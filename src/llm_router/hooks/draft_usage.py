"""Did the draft get USED, or only produced?

`DIRECT SUCCESS` records that a local model returned a draft and the hook
injected it. It records nothing about what happened next, and what happens next
is the whole question: Claude reads the draft as an unverified hint and is told
to discard it whenever the answer depends on anything the draft model could not
see. That is the common case in a repo, so the injected draft is frequently
thrown away and Claude does the work anyway — at full cost.

A routing rate built on DIRECT SUCCESS therefore counts drafts PRODUCED and
reports them as work routed. This is not a small correction: a session whose log
showed successful routing throughout drove subscription quota from 49% to 79%,
because every draft in it was discarded.

Usage is observable. A relayed draft must open with the `🎯 LLM Router routed →`
line the hook asks for by name, so the next assistant turn in the transcript
says which happened. The check runs one invocation late — at the next prompt,
when the turn it is judging is complete and the transcript is already open.

Absence of the marker is treated as UNUSED. It can undercount (Claude used the
draft's substance without the marker), and that direction is deliberate: the
counter exists because the old one flattered itself, and a replacement that
guesses in the same direction would be no better than the number it replaces.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

RELAY_MARKER = "🎯 LLM Router routed"

# The FIRST non-empty line, exactly as the hook instructs ("begin your reply with
# this exact line then a blank line"). Not a few lines of slack, and certainly not
# a whole-body search: Claude quoting the marker while explaining that it
# DISCARDED the draft is not a relay, and this session produced that text
# repeatedly. A three-line window scored one of those as a successful relay.

USED = "used"
UNUSED = "unused"

# A pending record older than this is stale — the session was abandoned, or the
# user never replied. Judging it against whatever turn appears next would be
# worse than not judging it.
_PENDING_TTL_S = 3600.0


def _pending_dir() -> Path:
    """Resolved per call. A module-level Path.home() freezes $HOME at import,
    which is the defect class that has bitten this tree four times."""
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "pending_drafts"


def _pending_path(session_id: str) -> Path:
    # Session ids come from the host; keep only path-safe characters so a hostile
    # or malformed id cannot select a file outside the directory.
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:128]
    return _pending_dir() / f"{safe or 'unknown'}.json"


def record_draft(session_id: str, invocation_id: float, model: str) -> bool:
    """Note that a draft was injected, to be judged at the next prompt.

    Returns True if recorded. Never raises: this runs inside the hook, and a
    measurement that breaks the prompt is worse than no measurement.
    """
    if not session_id:
        return False
    try:
        path = _pending_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "invocation_id": invocation_id,
            "model": model,
            "at": time.time(),
        }), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def take_pending(session_id: str) -> dict | None:
    """Read and clear the pending record. Returns None when absent or stale."""
    if not session_id:
        return None
    path = _pending_path(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent, unreadable or malformed
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    try:
        if time.time() - float(data.get("at", 0)) > _PENDING_TTL_S:
            return None
    except (TypeError, ValueError):
        return None
    return data


def draft_was_relayed(assistant_text: str) -> bool:
    """Does this reply open with the relay marker the hook asked for?"""
    if not assistant_text:
        return False
    for line in assistant_text.splitlines():
        if not line.strip():
            continue
        return RELAY_MARKER in line
    return False


def audit(session_id: str, last_assistant_text: str) -> tuple[str, dict] | None:
    """Judge the previous draft against the turn that followed it.

    Returns ``(USED|UNUSED, pending_record)``, or None when there was no draft
    to judge.
    """
    pending = take_pending(session_id)
    if pending is None:
        return None
    return (USED if draft_was_relayed(last_assistant_text) else UNUSED), pending
