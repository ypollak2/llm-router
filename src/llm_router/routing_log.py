"""The one parser for the routing hook's debug log.

Moved here from scripts/routing_rate.py (which now imports it) so the installed
`llm-router routing-health` and the script cannot disagree — two ad-hoc parsers
did, on 2026-09-13, and produced two wrong answers about "routing stopped".
Dependency-free on purpose: the script runs under a bare `python3`.
"""
from __future__ import annotations

import re
from pathlib import Path

MIN_SAMPLE = 50   # below this, report "too few to tell" rather than a number

_START = re.compile(r"\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION START\] ID=([\d.]+)")
_LINE = re.compile(r"\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION ([\d.]+)\] (.*)")


def default_log() -> Path:
    from llm_router import paths
    return paths.state_path("auto-route-debug.log")


def load(path: Path) -> dict[str, dict]:
    inv: dict[str, dict] = {}
    for line in path.read_text(errors="replace").splitlines():
        m = _LINE.match(line)
        if not m:
            m2 = _START.match(line)
            if m2:
                inv.setdefault(m2.group(2), {"day": m2.group(1), "msgs": []})
            continue
        day, iid, rest = m.groups()
        rec = inv.setdefault(iid, {"day": day, "msgs": []})
        rec["msgs"].append(rest)
        sid = re.search(r"session_id=(\S+)", rest)
        if sid:
            rec["sid"] = sid.group(1)
    return inv


# A real session id is what the host actually emits: Claude Code and Codex both
# log the first 8 characters of a UUID, so it is exactly 8 lowercase hex digits.
# Anything else is a fixture.
_REAL_SESSION = re.compile(r"^[0-9a-f]{8}$")


def is_real(rec: dict) -> bool:
    """A prompt from a real session — the only thing a rate may be computed over.

    Excluding only `session_id=unknown` is NOT enough, and getting that wrong is
    the third denominator error in this investigation — this time in the script
    written to prevent the first two. Test fixtures use readable ids, and they
    were counted as real sessions: `sess-fai` (496 prompts), `sess-xyz` (251),
    `sess-abc` (248), `sess-qa` (248), `sess-emp` (248) — 1,508 in total, which
    moved the overall rate by several points.

    So the test is positive, not negative: a session id must LOOK like one the
    host emits, rather than merely not look like one particular fixture. A new
    fixture name cannot silently rejoin the denominator.
    """
    return (any(m.startswith("prompt_len=") for m in rec["msgs"])
            and bool(_REAL_SESSION.match(rec.get("sid") or "")))


def is_human(rec: dict) -> bool:
    """A real prompt a PERSON typed — what a reach or use rate is about.

    A real session id is not enough. Replaying 10 days (2026-09-24) showed the
    hook also sees benchmark sessions run in /tmp sandboxes (real-looking ids),
    sub-agent reports and background-task notifications. The hook tags the
    first two on its `prompt_len=` line (`sandbox=1`, `kind=agent-report`) and
    bypasses notifications with a logged reason. Untagged lines written before
    2026-09-24 cannot be told apart and count as human.
    """
    if not is_real(rec):
        return False
    head = next((m for m in rec["msgs"] if m.startswith("prompt_len=")), "")
    if " sandbox=1" in head or " kind=agent-report" in head:
        return False
    return not any("SYSTEM_NOTIFICATION_BYPASS" in m for m in rec["msgs"])


def outcome(rec: dict) -> str:
    for m in rec["msgs"]:
        if m.startswith("DIRECT:"):
            return "reached"
        if "DIRECT SKIP:" in m:
            return "skip: " + m.split("DIRECT SKIP:")[1].strip()[:34]
        if "BYPASS" in m:
            return "bypass"
        if "CONTINUATION" in m:
            return "continuation"
    return "UNLOGGED — a prompt whose fate was never recorded"
