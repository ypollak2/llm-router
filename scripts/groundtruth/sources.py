"""Where real user prompts actually live, and what has to be thrown away.

Measured 2026-09-20, this machine:

  ~/.llm-router/routing_quality.jsonl   22,356 rec  routing metadata, NO prompt text
  ~/.llm-router/auto-route-debug.log     8,252 inv  `prompt_len=` only, NO prompt text
  ~/.llm-router/model_tracking.jsonl               NO prompt text
  ~/.llm-router/transcript_*.jsonl      170 files   {role, content} — HAS text
  ~/.claude/projects/*/*.jsonl          378 files   type=user records — HAS text

The two stores that hold prompt text carry no route_id, ts or task_type, and
the two that carry routing metadata hold no text. There is no join key. That
is why `PromptRecord.task_type` is None for every historical record and is
only populated by the live capture path (`prompt_capture.py`), which writes
both halves together.

Everything here is read-only and defensive: these files are live logs written
by other processes, so a truncated final line is normal, not an error.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

def llm_router_home() -> Path:
    """Router state dir, resolved on every call.

    M-04: this was a module-level constant reading the environment once, at
    import. That honoured LLM_ROUTER_HOME only if it was already set when the
    module first loaded, so a caller that set it afterwards — every test, and
    any process switching profiles — silently read the operator's real state.
    """
    return Path(os.environ.get("LLM_ROUTER_HOME", "").strip() or Path.home() / ".llm-router")
def claude_projects_dir() -> Path:
    """Claude Code's transcript directory, resolved on every call (M-04)."""
    return Path(os.environ.get("CLAUDE_PROJECTS_DIR", "").strip()
                or Path.home() / ".claude" / "projects")

# ── Exclusion reasons. Every dropped record carries one, so the manifest can
# account for the full funnel rather than just reporting survivors. ──────────
DROP_UNPARSEABLE = "unparseable-json"
DROP_NOT_USER = "not-a-user-turn"
DROP_NO_TEXT = "no-text-content"
DROP_EMPTY = "empty"
DROP_SYSTEM_NOISE = "system-noise"
DROP_TOOL_ECHO = "tool-or-command-echo"
DROP_TEST_SESSION = "synthetic-test-session"
DROP_TOO_SHORT = "degenerate-too-short"
DROP_DUPLICATE = "duplicate"
DROP_SANDBOX_PROJECT = "benchmark-sandbox-workspace"
DROP_HARNESS_ARTEFACT = "harness-artefact"
DROP_BARE_ATTACHMENT = "bare-attachment"
DROP_PASTED_TOOL_OUTPUT = "pasted-tool-output"

# ── Artefacts that reach the transcript as "user" turns but contain no user
# instruction. Found 2026-09-20 in the frozen seed set: 18 of 137 rows, which
# `_NOISE_PREFIX` missed because it keyed on the `<tag>` shape and these use
# other shapes entirely. Five of them announce it in their own text — the
# literal string "not an instruction to follow". ────────────────────────────

# Context the harness injects ahead of a turn, plus loaded skill bodies and raw
# tool output. None of these is a thing a person typed.
_HARNESS_ARTEFACT = re.compile(
    r"^\s*(?:"
    r"\[Background context from this session"
    r"|\[Recent conversation context\]"
    r"|<bash-std(?:out|err)"
    r"|Base directory for this skill:"
    r"|<function_results"
    r"|\[Tool output\]"
    r")",
    re.I,
)

# An attachment with no words of the user's own. `[Image: source: …]` alone is
# the file being attached; `[Image #2] - use the amount in green` is a real
# instruction ABOUT an image and must survive, which is why this anchors on the
# `source:` form and checks that nothing else follows.
_BARE_ATTACHMENT = re.compile(r"^\s*\[(?:Image|File|Attachment):\s*source:[^\]]*\]\s*$", re.I)

# Terminal/CI output pasted with no question attached. A paste that a person
# wrapped in their own words is a legitimate prompt; these are the raw dump
# alone, so the rule anchors at the START of the text. A paste preceded by a
# question is therefore kept, which is the conservative direction.
_PASTED_TOOL_OUTPUT = re.compile(
    r"^\s*(?:"
    r"npm (?:notice|error|WARN)\b"
    r"|[\w.-]+@[\w.-]+\s+\w+\s*%\s"          # shell prompt line: user@host dir %
    r"|(?:macos|ubuntu|windows)-[\d.]+\s+\(best effort\)"
    r"|Run\s+\w[\w-]*/[\w-]+@[0-9a-f]{40}"   # GitHub Actions step echo
    r"|\+\s*\w+\s+\w+.*\n\+\s"                # set -x trace
    r")",
    re.I,
)

# Claude Code names a project directory after its working directory, with
# separators flattened to "-". A session whose cwd was under /tmp or
# /private/tmp was a benchmark sandbox, not work: `bench_backend_quality.py`
# defaults to BENCH_SANDBOX=/tmp/bq_<backend>, which is why
# `-private-tmp-bq-claude` and friends exist. Measured 2026-09-20: 12 such
# directories, and they put six verbatim bench fixture prompts into the corpus
# ("What is the current value of MAX_VALUE? Answer with just the number.").
#
# This is the same denominator error the project CLAUDE.md records for
# `session_id=unknown` in auto-route-debug.log — benchmark traffic that looks
# like usage. Exclude by workspace, not by prompt text, so it keeps working
# when someone adds a fixture.
_SANDBOX_PROJECT = re.compile(r"^-(private-)?(tmp|var-folders)-", re.I)

# Harness-injected blocks. These are not things a human typed.
_NOISE_PREFIX = re.compile(
    r"^\s*<(task-notification|system-reminder|local-command|command-name"
    r"|command-message|command-args|local-command-stdout|agent-message"
    r"|function_results|user-prompt-submit-hook)",
    re.I,
)
_CAVEAT = re.compile(r"^\s*Caveat: The messages below were generated", re.I)
_INTERRUPT = re.compile(r"^\s*\[Request interrupted", re.I)

# Session ids that are obviously fixtures. `aabbccdd`, `beef1234`, `sess-xyz`
# and friends appear throughout auto-route-debug.log; they are benchmark runs,
# not usage. `unknown` is excluded too — it cannot be attributed, and on
# 2026-09-13 it was 54% of one day's routing log.
_TEST_SESSION = re.compile(
    r"^(unknown|sess-[a-z0-9-]+|[0-9a-f]{0,4}(1234|abcd|beef|cafe|f00d))$",
    re.I,
)

# Words that only appear in a session id somebody typed by hand.
_FIXTURE_WORD = re.compile(
    r"(test|demo|mode|trace|probe|smoke|bench|fixture|sample|example|dummy"
    r"|fake|mock|debug|scratch|tmp|temp|foo|bar|baz)",
    re.I,
)


# Hex placeholder words, as prefixes so truncated forms are caught too.
_HEX_FIXTURE_STEMS = (
    "deadbee", "deadbea", "cafebab", "feedfac", "badc0de", "decafba",
    "0ddba11", "abadcaf", "8badf00", "facefee", "baaaaaa", "beefbee",
)


def is_synthetic_session(session_id: str | None) -> bool:
    """True when a session id was authored rather than generated.

    Real ids from this harness are UUIDs or 8-hex-character prefixes of one.
    Fixtures are hand-typed and give themselves away two ways: they contain a
    word ("modetest", "tracedemo"), or they have almost no entropy
    ("a1a1a1a1", "deadbee2").

    Found on 2026-09-20 *after* the first filter passed them: the handful of
    corpus prompts that looked most gradable — "what is the capital of
    Portugal?", "Write a detailed 900-word architecture document" — all came
    from these sessions. They are prompts written to exercise the router, so
    treating them as usage would have made the dataset measure its own test
    fixtures.
    """
    if not session_id:
        return False
    sid = session_id.strip().lower()
    if _TEST_SESSION.match(sid):
        return True
    if _FIXTURE_WORD.search(sid):
        return True
    core = sid.replace("-", "")
    # Hex words people type as placeholders, including truncated forms such as
    # "deadbee2" — which has five distinct characters and so survives the
    # entropy test below.
    if any(core.startswith(stem) for stem in _HEX_FIXTURE_STEMS):
        return True
    # A hex id with four or fewer distinct characters is not random.
    if len(core) >= 8 and all(c in "0123456789abcdef" for c in core):
        if len(set(core)) <= 4:
            return True
    return False

# Free-standing slash commands and shell escapes: real input, but not a task.
_COMMAND_ONLY = re.compile(r"^\s*[/!][A-Za-z0-9_-]+\s*$")


@dataclass
class PromptRecord:
    """One human-authored prompt, with enough provenance to re-find it."""

    text: str
    source_kind: str             # "llm-router-transcript" | "claude-code" | "captured"
    source_file: str             # path, relative to home where possible
    source_index: int            # line number within that file (1-based)
    session_id: str | None = None
    ts: float | None = None
    task_type: str | None = None  # only ever set by the live capture path
    route_id: str | None = None   # only ever set by the live capture path
    workspace_is_sandbox: bool = False
    content_sha: str = ""
    scrub_counts: dict[str, int] = field(default_factory=dict)
    residual_flags: list[str] = field(default_factory=list)

    def finalise(self) -> "PromptRecord":
        self.content_sha = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]
        return self

    def to_json(self) -> dict:
        return asdict(self)


def _rel_home(p: str | Path) -> str:
    try:
        return "~/" + str(Path(p).resolve().relative_to(Path.home()))
    except ValueError:
        return str(p)


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict | None]]:
    """Yield (line_no, obj). obj is None for a line that will not parse.

    These are live logs; the last line can be a partial write.
    """
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for i, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except (json.JSONDecodeError, ValueError):
                yield i, None


def classify_drop(text: str, session_id: str | None,
                  workspace_is_sandbox: bool = False) -> str | None:
    """Return the exclusion reason, or None if the record survives.

    Order matters: the most specific reason should win so the funnel in the
    manifest is interpretable.
    """
    if workspace_is_sandbox:
        return DROP_SANDBOX_PROJECT
    if is_synthetic_session(session_id):
        return DROP_TEST_SESSION
    s = (text or "").strip()
    if not s:
        return DROP_EMPTY
    # The specific artefact rules run BEFORE the generic ones. `<bash-stdout>`
    # is caught by the catch-all "starts with < and ends with >" rule too, but
    # it would then be filed as generic system-noise, and the funnel in the
    # manifest is only interpretable if the narrowest reason wins.
    if _HARNESS_ARTEFACT.match(s):
        return DROP_HARNESS_ARTEFACT
    if _BARE_ATTACHMENT.match(s):
        return DROP_BARE_ATTACHMENT
    if _PASTED_TOOL_OUTPUT.match(s):
        return DROP_PASTED_TOOL_OUTPUT
    if _NOISE_PREFIX.match(s) or _CAVEAT.match(s) or _INTERRUPT.match(s):
        return DROP_SYSTEM_NOISE
    if s.startswith("<") and s.endswith(">") and len(s) < 200:
        return DROP_SYSTEM_NOISE
    if _COMMAND_ONLY.match(s):
        return DROP_TOOL_ECHO
    # Two words or fewer cannot state a task under any reading.
    if len(s.split()) < 3:
        return DROP_TOO_SHORT
    return None


# ── Readers ──────────────────────────────────────────────────────────────────

def read_llm_router_transcripts(root: Path | None = None) -> Iterator[PromptRecord]:
    """~/.llm-router/transcript_<session>.jsonl — {"role","content"} per line.

    The session id is in the filename and nowhere else in the record.
    """
    root = root if root is not None else llm_router_home()
    for path in sorted(root.glob("transcript_*.jsonl")):
        session = path.stem.replace("transcript_", "")
        for line_no, obj in _iter_jsonl(path):
            if not isinstance(obj, dict) or obj.get("role") != "user":
                continue
            content = obj.get("content")
            if not isinstance(content, str):
                continue
            yield PromptRecord(
                text=content.strip(),
                source_kind="llm-router-transcript",
                source_file=_rel_home(path),
                source_index=line_no,
                session_id=session,
            )


def read_claude_code_transcripts(root: Path | None = None) -> Iterator[PromptRecord]:
    """~/.claude/projects/<project>/<session>.jsonl.

    A user turn is `{"type": "user", "message": {"content": ...}}` where
    content is either a string or a list of blocks; only `type == "text"`
    blocks are human-authored (tool_result blocks are machine output).
    """
    root = root if root is not None else claude_projects_dir()
    for path in sorted(Path(p) for p in glob.glob(str(root / "*" / "*.jsonl"))):
        sandbox = bool(_SANDBOX_PROJECT.match(path.parent.name))
        for line_no, obj in _iter_jsonl(path):
            if not isinstance(obj, dict) or obj.get("type") != "user":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                if not parts:
                    continue
                text = "\n".join(parts)
            else:
                continue
            ts = obj.get("timestamp")
            if isinstance(ts, str):
                try:
                    import datetime as _dt
                    ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    ts = None
            yield PromptRecord(
                text=text.strip(),
                source_kind="claude-code",
                source_file=_rel_home(path),
                source_index=line_no,
                session_id=obj.get("sessionId") or path.stem,
                ts=ts if isinstance(ts, (int, float)) else None,
                workspace_is_sandbox=sandbox,
            )


def read_captured(root: Path | None = None) -> Iterator[PromptRecord]:
    """~/.llm-router/prompt_capture.jsonl — written by prompt_capture.py.

    This is the only source with prompt text AND routing metadata in the same
    record, because it is written at the routing choke point. Historical data
    has no equivalent; this file starts empty and fills from the day capture
    is switched on.
    """
    root = root if root is not None else llm_router_home()
    path = root / "prompt_capture.jsonl"
    for line_no, obj in _iter_jsonl(path):
        if not isinstance(obj, dict):
            continue
        text = obj.get("prompt")
        if not isinstance(text, str):
            continue
        yield PromptRecord(
            text=text.strip(),
            source_kind="captured",
            source_file=_rel_home(path),
            source_index=line_no,
            session_id=obj.get("session_id"),
            ts=obj.get("ts"),
            task_type=obj.get("task_type"),
            route_id=obj.get("route_id"),
            scrub_counts=obj.get("scrub_counts") or {},
        )


READERS = {
    "llm-router-transcript": read_llm_router_transcripts,
    "claude-code": read_claude_code_transcripts,
    "captured": read_captured,
}
