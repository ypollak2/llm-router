"""Answer a tool call locally so its output never reaches Claude.

Measured on this machine over 5 days: 80.5% of all tool-output tokens were
images and 14.3% were shell output. Neither can be reduced after the fact — a
PostToolUse hook cannot replace a tool result (verified live: `updatedOutput` is
ignored, `additionalContext` only appends). The output arrives in full whatever
the hook says about it.

What DOES work is preventing the call. A PreToolUse `deny` whose
`permissionDecisionReason` carries the answer was verified in-session: the tool
never ran, the file was never loaded, and the substitute text reached the model.
That is the whole mechanism this module implements.

Two things the verification also showed, both encoded below:

  * the substitute arrives wrapped as an ERROR. Text that does not say "this is
    your result, continue" invites a retry, and a retry loop costs more than the
    interception saves.
  * the escape hatch has to be IN the message. The model is the only party that
    can tell the description was insufficient, and it cannot ask for the real
    thing unless it is told how.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import stat
import time
import urllib.request
from pathlib import Path

# Formats a vision model can actually take. A .svg is text and a .pdf is not an
# image to Ollama, so neither is intercepted — they would fail as a base64 blob.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

# Above this, reading the file to base64 it is itself expensive and the model is
# likely to choke. Falls through to Claude, which is the safe direction.
MAX_IMAGE_BYTES = 12 * 1024 * 1024

_DESCRIBE_PROMPT = (
    "Describe this screenshot for an engineer who cannot see it. Report, "
    "specifically and literally: every visible text label, field value, button "
    "caption, heading, status word, and number; the layout in reading order; and "
    "which element appears selected or active. Quote text exactly. If something "
    "is unreadable, say so rather than guessing."
)




def _env_override(name: str) -> str:
    """Literal env reads, so a scanner can see the names this module honours."""
    if name == ENV_IMAGE_INTERCEPT:
        return os.environ.get("LLM_ROUTER_IMAGE_INTERCEPT", "").strip().lower()
    if name == ENV_BASH_INTERCEPT:
        return os.environ.get("LLM_ROUTER_BASH_INTERCEPT", "").strip().lower()
    return os.environ.get(name, "").strip().lower()


def _flag(env_name: str, yaml_key: str) -> bool:
    """Resolve a boolean from the env, else ~/.llm-router/routing.yaml.

    Env alone is not enough to turn these on. A hook inherits the environment
    the HOST was launched with, so an export in a shell — or in the terminal
    running Claude Code — never reaches it, and `enforce_config` says as much:
    relying on a ~/.zshrc export produced inconsistent enforcement between
    sessions. File config is what survives a restart and a GUI launch.

    Parsed line-wise rather than with a yaml import, matching enforce_config,
    because this is on the PreToolUse critical path for every tool call.
    """
    raw = _env_override(env_name)
    if raw:
        return raw in ("1", "on", "true", "yes")
    try:
        base = os.environ.get("LLM_ROUTER_HOME", "").strip()
        root = Path(base).expanduser() if base else Path.home() / ".llm-router"
        for line in (root / "routing.yaml").read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(f"{yaml_key}:"):
                continue
            value = stripped.split(":", 1)[1].strip().strip("'\"").lower()
            return value in ("1", "on", "true", "yes")
    except Exception:
        pass
    return False




# Anthropic bills an image by its DIMENSIONS, about (width * height) / 750
# tokens, after downscaling so the longest side is at most 1568px. File size is
# not the measure: a 1280x720 screenshot costs ~1,228 tokens whatever it
# compresses to on disk.
#
# The first version of this estimated base64 length (bytes/3/4) and overstated
# three real screenshots by 10.7x — turning a measured 12% saving into a
# reported 92% one. That is the fourth projection-shaped error in this project,
# and the first to survive into a released measurement tool, so the formula is
# now the billed one and the fallback is conservative rather than flattering.
_MAX_IMAGE_EDGE = 1568
_PIXELS_PER_TOKEN = 750


def _image_token_cost(path: str) -> int:
    """Tokens this image would have cost Claude, or 0 if it cannot be measured.

    0, not a guess: an unmeasurable image contributes nothing to the numerator
    OR the denominator of a savings figure, which is the only honest way to
    leave it out. Reading dimensions needs no third-party library — PNG and JPEG
    headers are parsed directly, since Pillow is optional in this tree and its
    absence already caused one silent failure (vision_registry's probe).
    """
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return 0
    width = height = 0
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            import struct
            width, height = struct.unpack(">II", blob[16:24])
        elif blob[:2] == b"\xff\xd8":
            i = 2
            while i < len(blob) - 9:
                if blob[i] != 0xFF:
                    i += 1
                    continue
                marker = blob[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    import struct
                    height, width = struct.unpack(">HH", blob[i + 5:i + 9])
                    break
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                import struct
                i += 2 + struct.unpack(">H", blob[i + 2:i + 4])[0]
    except Exception:
        return 0
    if width <= 0 or height <= 0:
        return 0
    scale = min(1.0, _MAX_IMAGE_EDGE / max(width, height))
    return int(width * scale) * int(height * scale) // _PIXELS_PER_TOKEN


def _log_intercept(kind: str, detail: str, before_tokens: int,
                   after_tokens: int) -> None:
    """Append one JSONL record per interception. Never raises.

    Without this there is nothing to audit: an interception leaves no trace in
    the transcript beyond a denial, and "it seems faster" is not a measurement.
    Each record carries what the call WOULD have cost and what it did cost, so
    the saving is computed from observations rather than from the benchmark's
    projection — which is the distinction that made the earlier 9.6% figure
    wrong.
    """
    try:
        base = os.environ.get("LLM_ROUTER_HOME", "").strip()
        root = Path(base).expanduser() if base else Path.home() / ".llm-router"
        root.mkdir(parents=True, exist_ok=True)
        # C-04: `detail` is a raw shell command. This file had zero scrubber
        # references and was found at mode 644 with 177 live rows and no TTL --
        # nothing would have stopped a `curl -H "Authorization: Bearer ..."` from
        # landing world-readable and staying there. Scrub BEFORE truncating, so a
        # secret cannot be cut mid-token past the pattern that matches it.
        try:
            from llm_router.secret_scrubber import scrub_text

            safe_detail = scrub_text(detail)[:200]
        except Exception:                                    # noqa: BLE001
            safe_detail = "[SCRUB-FAILED: command withheld]"
        record = {
            "at": time.time(),
            "kind": kind,
            "detail": safe_detail,
            "before_tokens": int(before_tokens),
            "after_tokens": int(after_tokens),
            "saved_tokens": int(before_tokens) - int(after_tokens),
        }
        # C-04: create at 0600 rather than inheriting the umask (0644).
        try:
            from llm_router.paths import private_opener
        except Exception:                                    # noqa: BLE001
            private_opener = None
        target = root / "intercepts.jsonl"
        if private_opener is not None:
            with open(target, "a", encoding="utf-8", opener=private_opener) as handle:
                handle.write(json.dumps(record) + "\n")
        else:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        # repair an already-existing file created by an older version at 0644
        try:
            if stat.S_IMODE(target.stat().st_mode) != 0o600:
                os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        pass


# Named as literals so the env_registry scanner can see them: it greps for
# os.environ.get("NAME"), and passing the name through _flag as a parameter made
# both vars look undeclared-but-read — "phantom" entries the registry test
# correctly refused.
ENV_IMAGE_INTERCEPT = "LLM_ROUTER_IMAGE_INTERCEPT"
ENV_BASH_INTERCEPT = "LLM_ROUTER_BASH_INTERCEPT"


def image_intercept_enabled() -> bool:
    """Default OFF.

    A description is not a substitute for looking. "Does this button say
    Continue" is answered perfectly by one (8/8 measured); "does this UI look
    right" is not, and the loss would be invisible to both parties — the model
    receives a confident description and has no way to know what it missed.
    Turning this on is a decision about what the images are FOR, which belongs
    to the person who took them.
    """
    return _flag(ENV_IMAGE_INTERCEPT, "image_intercept")


def is_image(path: str) -> bool:
    try:
        return Path(path).suffix.lower() in IMAGE_SUFFIXES
    except Exception:
        return False


def describe_image(path: str, model: str, timeout: int = 120) -> str | None:
    """Local model's description, or None if anything at all goes wrong.

    None always means "let Claude read it": an unreadable file, an oversized
    one, a model that will not load, a timeout. Falling back costs tokens;
    failing closed with a wrong description costs correctness.
    """
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    if not blob or len(blob) > MAX_IMAGE_BYTES:
        return None

    body = json.dumps({
        "model": model,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1},
        "messages": [{
            "role": "user",
            "content": _DESCRIBE_PROMPT,
            "images": [base64.b64encode(blob).decode()],
        }],
    }).encode()
    try:
        from llm_router.hooks.agent_loop import _get_ollama_url
        request = urllib.request.Request(
            f"{_get_ollama_url().rstrip('/')}/api/chat",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = json.loads(response.read()).get("message", {}).get("content", "")
    except Exception:
        return None
    text = (text or "").strip()
    # A two-word reply is a failure wearing a success's clothes.
    return text if len(text) >= 40 else None


def substitute_message(path: str, model: str, description: str) -> str:
    """The text the model receives INSTEAD of the image.

    Every part of this is load-bearing. It says the call did not fail, names the
    source so the description is never mistaken for Claude's own observation,
    and carries the escape hatch — because the model is the only party that can
    judge the description insufficient, and it cannot ask for the image unless
    it is told how.
    """
    return (
        f"[llm-router] The image was NOT loaded into your context. A local "
        f"vision model ({model}) described it instead, which is why this cost "
        f"no image tokens. Treat the description below as the result of the "
        f"Read and continue — this is not an error.\n\n"
        f"FILE: {path}\n"
        f"DESCRIPTION (from {model}, not from you — do not present it as your "
        f"own observation):\n{description}\n\n"
        f"If this description is not enough for the task — for example a visual "
        f"design judgement rather than a factual lookup — say so to the user and "
        f"ask them to re-run with LLM_ROUTER_IMAGE_INTERCEPT=off, which loads "
        f"the real image."
    )


def try_intercept_read(hook_input: dict) -> str | None:
    """The substitute message for a Read of an image, or None to let it proceed.

    None is returned for every uncertainty: feature off, not an image, no model
    has PROVEN it can see, or the description failed. Each of those routes the
    read to Claude, which is correct but expensive — and the expensive right
    answer beats the cheap wrong one.
    """
    if not image_intercept_enabled():
        return None
    if hook_input.get("tool_name") != "Read":
        return None
    path = str((hook_input.get("tool_input") or {}).get("file_path", ""))
    if not path or not is_image(path):
        return None

    try:
        from llm_router.vision_registry import best_vision_model
        model = best_vision_model(allow_probe=False)
    except Exception:
        return None
    if not model:
        return None

    description = describe_image(path, model)
    if not description:
        return None
    message = substitute_message(path, model, description)
    try:
        _log_intercept("image", path, _image_token_cost(path), len(message) // 4)
    except Exception:
        pass
    return message


def deny_payload(reason: str) -> dict:
    """The PreToolUse shape that stops the call and delivers the text."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ── Bash ────────────────────────────────────────────────────────────────────
#
# Shell output is 14.3% of tokens. The compressor reduces it by 9.6% over 994
# real outputs, but that saving was never realised: a PostToolUse hook cannot
# replace the output, so the full text arrived regardless. Running the command
# HERE and denying the call is what makes the number real — the uncompressed
# output never exists in the context at all.
#
# This is a bigger step than the image case and is treated as such. Running a
# command from a PreToolUse hook means the hook is now the executor: the
# working directory, the environment, the timeout and the exit code all have to
# be right, and a mistake is not a bad description but a command that ran
# differently than the model believed. Hence OFF by default, an explicit
# allowlist of commands whose output is bulky and side-effect-free, and a hard
# refusal to touch anything that writes.

# Commands worth intercepting: verbose output, no side effects. Anything that
# mutates state is excluded on principle — the saving is not worth owning the
# semantics of someone else's `git commit`.
_INTERCEPT_VERBS = frozenset({
    "git status", "git log", "git diff", "git show", "git branch",
    "ls", "cat", "head", "tail", "wc", "find", "grep", "rg", "tree", "du",
    # Read-only text utilities. Measured on 261 real Bash calls: the four
    # single largest outputs in the session were `sed -n '1,90p' <file>` —
    # semantically a Read, which this module already intercepts, but `sed` was
    # not on the list. Adding this group moves coverage from 0.3% to 23.3% of
    # tool-output bytes, and every verb here is side-effect-free BY DEFAULT —
    # the flags that make them otherwise are refused below.
    "sed", "awk", "nl", "jq", "diff", "sort", "uniq", "cut", "comm", "paste",
    "column", "stat", "file", "basename", "dirname", "echo", "printf", "date",
    "which", "pwd", "fd", "realpath", "readlink", "md5", "shasum", "cksum",
})

# Flags that turn an allowlisted reader into a writer or an arbitrary executor.
# `sed -i` edits in place; `find -delete` and `find -exec` were reachable BEFORE
# this change, because `find` was already allowlisted — closing that is a fix,
# not a new restriction.
_BLOCKED_FLAGS = {
    "sed": ("-i", "--in-place"),
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
             "-fprintf", "-fls"),
    "awk": ("-i", "--in-place"),
    "jq": ("-i", "--in-place"),
    "git": ("-c",),          # `git -c alias.x=!sh` runs a shell
    "sort": ("-o", "--output"),
    "shasum": ("-c", "--check"),
}

_BASH_TIMEOUT_S = 30
_MIN_LINES_TO_BOTHER = 12


def bash_intercept_enabled() -> bool:
    """Default OFF. Running the command here makes this hook the executor, and
    a wrong environment is worse than an uncompressed result."""
    return _flag(ENV_BASH_INTERCEPT, "bash_intercept")


# Everything below is refused before any parsing happens. `|` and the
# redirections need a shell to mean anything, and handing this module a shell is
# how a cost optimisation turns into an arbitrary-execution surface. `||` and
# `;` are excluded for a subtler reason than danger: they run the right-hand
# side after the left-hand side FAILED, or regardless of it, so the output a
# caller gets back depends on a control-flow decision this module would have to
# model correctly. `&&` has no such ambiguity — every segment ran, in order, and
# each one succeeded.
_SHELL_METACHARS = ("||", ";", ">", "<", "`", "$(", "&>", "${")


def _single_interceptable(segment: str) -> bool:
    """One bare command, no operators, whose verb is allowlisted and whose
    flags do not turn it into a writer."""
    parts = segment.split()
    if not parts:
        return False
    one = parts[0]
    two = f"{parts[0]} {parts[1]}" if len(parts) > 1 else ""
    if not (two in _INTERCEPT_VERBS or one in _INTERCEPT_VERBS):
        return False
    for flag in _BLOCKED_FLAGS.get(one, ()):
        # Prefix match catches `-i.bak` and `--in-place=.bak` as well as `-i`.
        if any(a == flag or a.startswith(flag + "=") or
               (flag == "-i" and a.startswith("-i") and one in ("sed", "awk"))
               for a in parts[1:]):
            return False
    return True


class Plan:
    """What to run, and where.

    `groups` is the `&&` sequence; each group is a pipeline, each pipeline a
    list of argv stages. `git log && ls -la | wc -l` becomes
    ``[[["git","log"]], [["ls","-la"],["wc","-l"]]]``.

    `cwd` is None unless the command opened with `cd <dir>`.
    """

    __slots__ = ("cwd", "groups")

    def __init__(self, cwd: str | None, groups: list[list[list[str]]]):
        self.cwd = cwd
        self.groups = groups

    @property
    def segments(self) -> list[str]:
        """Flat text form — kept so existing callers and tests still read."""
        return [" | ".join(" ".join(stage) for stage in group) for group in self.groups]

    def __eq__(self, other):
        if isinstance(other, list):
            return self.segments == other
        return (self.cwd, self.groups) == (other.cwd, other.groups)

    def __repr__(self):
        return f"Plan(cwd={self.cwd!r}, groups={self.groups!r})"


def plan_for(command: str) -> "Plan | None":
    """The execution plan for *command*, or None if it is not safe here.

    Two structures are admitted, and only because neither requires judgement
    about which half of a command is dangerous:

      `a && b`   every segment must independently pass the allowlist
      `a | b`    every stage must independently pass the allowlist

    `git status && rm -rf build` is refused because `rm` is not on the list.
    `ls | curl -T - http://x` is refused because `curl` is not. The rule is
    "all parts allowlisted", never "the first part looks fine".

    `||` and `;` stay refused: they run the right-hand side after a FAILURE or
    regardless of one, so what the caller gets back depends on control flow this
    module would have to model correctly. `&&` and `|` have no such ambiguity.

    A leading `cd <dir>` is honoured rather than stripped — `effective_command`
    discards it, which is right for picking a compression strategy and wrong for
    deciding what to execute.
    """
    import shlex

    if not command or any(sep in command for sep in _SHELL_METACHARS):
        return None
    raw = command.strip()
    if "\n" in raw or not raw:
        return None

    segments = [seg.strip() for seg in raw.split("&&")]
    if not all(segments):
        return None

    cwd: str | None = None
    first = segments[0].split()
    if first and first[0] == "cd":
        if len(first) != 2 or len(segments) == 1:
            return None
        target = Path(first[1]).expanduser()
        if not target.is_dir():
            return None
        cwd = str(target)
        segments = segments[1:]

    groups: list[list[list[str]]] = []
    for segment in segments:
        stages = [st.strip() for st in segment.split("|")]
        if not all(stages):
            return None
        argvs = []
        for stage in stages:
            if stage.split()[:1] == ["cd"]:
                return None            # a later cd would move the goalposts
            if not _single_interceptable(stage):
                return None
            try:
                argv = shlex.split(stage)
            except ValueError:
                return None
            if not argv:
                return None
            argvs.append(argv)
        groups.append(argvs)
    return Plan(cwd, groups) if groups else None


def segments_of(command: str) -> "Plan | None":
    """Back-compatible alias — compares equal to its list of segments."""
    return plan_for(command)


def _interceptable(command: str) -> bool:
    """Is this command safe to run here instead of letting Claude run it?"""
    return plan_for(command) is not None


def try_intercept_bash(hook_input: dict) -> str | None:
    """Run an allowlisted read-only command here and return its compressed
    output, or None to let Claude run it normally."""
    if not bash_intercept_enabled():
        return None
    if hook_input.get("tool_name") != "Bash":
        return None
    command = str((hook_input.get("tool_input") or {}).get("command", "")).strip()
    if not command or not _interceptable(command):
        return None


    plan = plan_for(command)
    if plan is None:
        return None
    cwd = plan.cwd or hook_input.get("cwd") or os.getcwd()

    # Each group runs in order; each group's stages are wired stdout->stdin.
    # No shell anywhere, so nothing in the text can expand, glob or chain.
    # `&&` semantics are reproduced here rather than delegated: stop at the
    # first non-zero exit and hand the whole command back to Claude, because a
    # partial result presented as a complete one is worse than not
    # intercepting. A pipeline's status is its LAST stage's, as in the shell —
    # an upstream `head` closing the pipe is normal, not a failure.
    stdout_parts: list[str] = []
    deadline_s = float(_BASH_TIMEOUT_S)
    for group in plan.groups:
        t0 = time.monotonic()
        try:
            procs = []
            prev_stdout = None
            for i, argv in enumerate(group):
                proc = subprocess.Popen(
                    argv, cwd=cwd,
                    stdin=prev_stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True)
                if prev_stdout is not None:
                    prev_stdout.close()   # let upstream see SIGPIPE
                prev_stdout = proc.stdout
                procs.append(proc)
            out, _ = procs[-1].communicate(timeout=max(1, int(deadline_s)))
            for proc in procs[:-1]:
                proc.wait(timeout=5)
        except Exception:
            for proc in locals().get("procs", []):
                try:
                    proc.kill()
                except Exception:
                    pass
            return None
        if procs[-1].returncode != 0:
            return None
        stdout_parts.append(out or "")
        deadline_s -= time.monotonic() - t0
        if deadline_s <= 0:
            return None

    class _Joined:
        """Stand-in for the single CompletedProcess the rest expects."""
        returncode = 0
        stdout = "".join(stdout_parts)

    completed = _Joined()
    # A non-zero exit is information the model needs and the reason text is a
    # poor place to convey a failure, so hand those back to Claude untouched.
    if completed.returncode != 0:
        return None
    output = completed.stdout or ""
    if output.count("\n") < _MIN_LINES_TO_BOTHER:
        return None

    try:
        from llm_router.compression.rtk_adapter import RTKAdapter
        result = RTKAdapter(enable=True).compress(command, output)
        compressed, strategy = result.output, result.strategy
    except Exception:
        return None
    if len(compressed) >= len(output):
        return None

    saved = (len(output) - len(compressed)) // 4
    _log_intercept("bash", command, len(output) // 4, len(compressed) // 4)
    return (
        f"[llm-router] This command was run locally by the router and its output "
        f"compressed, so the full output never entered your context "
        f"(~{saved:,} tokens saved, filter {strategy}). Treat the output below "
        f"as the result and continue — this is not an error.\n\n"
        f"$ {command}\n{compressed}\n\n"
        f"If you need the uncompressed output, re-run with "
        f"LLM_ROUTER_BASH_INTERCEPT=off."
    )


def try_intercept(hook_input: dict) -> str | None:
    """Either intercept, in tool order, or None."""
    return try_intercept_read(hook_input) or try_intercept_bash(hook_input)
