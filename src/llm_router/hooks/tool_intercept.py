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


def image_intercept_enabled() -> bool:
    """Default OFF.

    A description is not a substitute for looking. "Does this button say
    Continue" is answered perfectly by one (8/8 measured); "does this UI look
    right" is not, and the loss would be invisible to both parties — the model
    receives a confident description and has no way to know what it missed.
    Turning this on is a decision about what the images are FOR, which belongs
    to the person who took them.
    """
    return os.environ.get("LLM_ROUTER_IMAGE_INTERCEPT", "").strip().lower() in (
        "1", "on", "true", "yes",
    )


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
    return substitute_message(path, model, description)


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
})

_BASH_TIMEOUT_S = 30
_MIN_LINES_TO_BOTHER = 12


def bash_intercept_enabled() -> bool:
    """Default OFF. Running the command here makes this hook the executor, and
    a wrong environment is worse than an uncompressed result."""
    return os.environ.get("LLM_ROUTER_BASH_INTERCEPT", "").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _interceptable(command: str) -> bool:
    """Only a single, side-effect-free, allowlisted command.

    A compound command is refused outright rather than parsed: `git status &&
    rm -rf build` starts with an allowlisted verb, and deciding which half is
    safe is exactly the kind of judgement that should not live in a cost
    optimisation.
    """
    from llm_router.compression.rtk_adapter import effective_command

    if any(sep in command for sep in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
        return False
    effective = effective_command(command).strip()
    if "\n" in effective:
        return False
    parts = effective.split()
    if not parts:
        return False
    one = parts[0]
    two = f"{parts[0]} {parts[1]}" if len(parts) > 1 else ""
    return two in _INTERCEPT_VERBS or one in _INTERCEPT_VERBS


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

    import shlex
    import subprocess

    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    cwd = hook_input.get("cwd") or os.getcwd()
    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=_BASH_TIMEOUT_S, cwd=cwd)
    except Exception:
        return None
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
