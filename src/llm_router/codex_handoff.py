"""Bookkeeping for the binding Codex agent-route handoff: void detector, session breaker, re-issue guard.

A delegated unit is a PreToolUse[Agent] decision: ``agent-route.py`` runs the sub-agent's task on Codex and
DENIES the Agent call, so the Codex text reaches Claude as the tool result and no Claude sub-agent is
spawned. The deny is a request to the host, not an effect. For 317 of 317 delegations in the window
2026-10-04..2026-11-03 the host applied none of them (docs/BUGS.md BIND-1, BIND-4): the
hook's stdout was not one JSON object, Claude Code read it as plain text, and Claude's own sub-agent ran as
well. Nothing recorded that, because a delegated row says "Codex ran", not "the host honoured the deny".

Three facts of the host make the detection possible without an extra model call:

* PostToolUse[Agent] fires only for an Agent call that RAN. A call whose deny was honoured never reaches it.
* Both hooks see the same ``tool_use_id``.
* So a ``tool_use_id`` that the router delivered (denied with a result) and that then reaches PostToolUse[Agent]
  was void: the router paid for a Codex run the host threw away.

Three voids in one session open a breaker (``breaker_open``): the router stops spending Codex time in that
session. ``was_delivered`` is the other direction: the same prompt coming back after an honoured deny means the
model judged the result insufficient, and the escape hatch in the delivered text (call the Agent tool again with
the same prompt) must lead to Claude's own sub-agent, not to a second Codex run.

State lives in the per-session depth file the Agent hooks already share (``agent_depth_<session>.json``, removed
by the SessionEnd hook), under its own key, read and written inside the same ``.lock`` file those hooks use.
Everything fails open: unreadable or unwritable state means "no breaker, not void", is counted through
``failopen`` and never raises, because these run inside hooks whose failure must not cost the user a turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from llm_router import failopen, paths, state_files
from llm_router.file_lock import exclusive_lock

#: Voids in one session before the router stops delegating to Codex there.
VOID_LIMIT = 3
#: How long a delivered prompt (and an unresolved delivered id) is remembered.
DELIVERED_TTL_S = 3600.0
_MAX_DELIVERED = 100
_KEY = "codex_handoff"
_LOCK_WAIT_S = 0.25  # per acquisition; a hook must not stall a spawn on a lock


def prompt_fingerprint(prompt: str) -> str:
    """Stable id of a prompt for the re-issue guard. Not a secret and never stored with the prompt itself."""
    return hashlib.sha256((prompt or "").strip().encode("utf-8", "replace")).hexdigest()[:16]


def _depth_file(session_id: str) -> Path:
    # Same naming as agent-route.py / agent-depth-release.py / subagent-start.py _depth_file().
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "") or "unknown"
    return paths.state_path(f"agent_depth_{safe}.json")


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def _section(data: dict) -> dict:
    raw = data.get(_KEY)
    return raw if isinstance(raw, dict) else {}


def _live(entries, now: float) -> list[list]:
    """Delivered entries ``[tool_use_id, ts, prompt_hash]`` younger than the TTL; malformed ones are dropped."""
    out: list[list] = []
    if not isinstance(entries, list):
        return out
    for e in entries:
        if (isinstance(e, list) and len(e) >= 3 and isinstance(e[1], (int, float))
                and now - float(e[1]) < DELIVERED_TTL_S and str(e[0]) and str(e[2])):
            out.append([str(e[0]), float(e[1]), str(e[2])])
    return out


def _update(session_id: str, mutate, code: str):
    """Locked read-modify-write of this module's section; returns ``mutate``'s result, or None on failure."""
    path = _depth_file(session_id)
    try:
        state_files.private_dir(path.parent)
        with exclusive_lock(Path(f"{path}.lock"), timeout=_LOCK_WAIT_S):
            try:
                data = _read(path)
            except (OSError, ValueError):
                data = {}  # unreadable or corrupt: start this section afresh rather than fail every time
            section = dict(_section(data))
            result = mutate(section)
            data[_KEY] = section
            data.setdefault("session_id", session_id)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
            try:
                with state_files.open_private(tmp, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(data))
                os.replace(tmp, path)
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            return result
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must never cost the user a turn
        failopen.record(code, exc)
        return None


def note_delivered(session_id: str, tool_use_id: str, prompt_hash: str, *, now: float | None = None) -> None:
    """The router is about to deny ``tool_use_id`` with a Codex result. Remember it (and the prompt)."""
    now = time.time() if now is None else now

    def _m(section: dict) -> None:
        kept = _live(section.get("delivered"), now)
        kept.append([tool_use_id or f"untracked-{time.monotonic_ns()}", now, prompt_hash])
        section["delivered"] = kept[-_MAX_DELIVERED:]

    _update(session_id, _m, "CHZ-FO-CODEX-HANDOFF-NOTE")


def note_agent_launched(session_id: str, tool_use_id: str, *, now: float | None = None) -> int | None:
    """PostToolUse[Agent] fired for ``tool_use_id``. If the router had delivered it, the deny was void: count it.

    Returns the session's void count after this one, or None when the call was an ordinary Agent call (or the
    state is unreadable). The delivered entry is removed, so one id is counted once.
    """
    if not tool_use_id:
        return None
    now = time.time() if now is None else now

    # PostToolUse[Agent] fires for every Agent call; almost none were delivered. Look first, without the lock and
    # without creating state, so an ordinary call writes nothing (a session that never delegated has no depth file
    # for the release hook to leave behind).
    try:
        peek = _live(_section(_read(_depth_file(session_id))).get("delivered"), now)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-HANDOFF-READ", exc)
        return None
    if not any(e[0] == tool_use_id for e in peek):
        return None

    def _m(section: dict) -> int | None:
        kept = _live(section.get("delivered"), now)
        hit = [e for e in kept if e[0] == tool_use_id]
        if not hit:
            return None
        section["delivered"] = [e for e in kept if e[0] != tool_use_id]
        prior = section.get("void")
        section["void"] = (prior if isinstance(prior, int) and prior > 0 else 0) + 1
        return section["void"]

    return _update(session_id, _m, "CHZ-FO-CODEX-HANDOFF-VOID")


def void_count(session_id: str) -> int:
    try:
        raw = _section(_read(_depth_file(session_id))).get("void")
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-HANDOFF-READ", exc)
        return 0
    return raw if isinstance(raw, int) and raw > 0 else 0


def breaker_open(session_id: str) -> bool:
    """True once ``VOID_LIMIT`` delegations in this session were discarded by the host."""
    return void_count(session_id) >= VOID_LIMIT


def was_delivered(session_id: str, prompt_hash: str, *, now: float | None = None) -> bool:
    """True when this prompt already got a Codex result in this session within the TTL."""
    now = time.time() if now is None else now
    try:
        entries = _live(_section(_read(_depth_file(session_id))).get("delivered"), now)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-HANDOFF-READ", exc)
        return False
    return any(e[2] == prompt_hash for e in entries)


def record_void(session_id: str, tool_use_id: str, void_n: int) -> None:
    """Append the ``delegation_void`` ledger row (same ledger and row shape as agent-route's own rows)."""
    try:
        try:
            from llm_router import session_kind

            kind = session_kind.kind_of(session_id)
        except Exception:  # noqa: BLE001 -- an untagged session is not an error
            kind = None
        row = {
            "ts": time.time(), "lever": "agent_route_codex", "model": "", "outcome": "delegation_void",
            "session_kind": kind, "session_id": session_id, "tool_use_id": tool_use_id,
            "reason": "the Agent call launched after the router denied it with a Codex result; the host ignored the deny",
            "reason_code": "agent_launched_after_deny", "void_count": void_n,
        }
        target = paths.state_path("north_star_units.jsonl")
        state_files.private_dir(target.parent)
        with state_files.open_private(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-HANDOFF-LEDGER", exc)
