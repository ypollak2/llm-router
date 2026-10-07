"""Which thread a proxy call belongs to: a join of the proxy ledger's ``msg_id`` to the Claude Code
transcripts (M0.3b).

A proxy row's ``msg_id`` is the API message id of its response. The same id is ``message.id`` on
the transcript's assistant entry. The transcript says what the call was:

``turn``          the FIRST assistant message of a main-thread turn that a typed prompt started
                  (the only kind of call O3 counts as a human turn);
``continuation``  a later assistant message of a main-thread turn (after a tool result);
``meta``          the first assistant message of a turn that no typed prompt started: a slash
                  command, a sub-agent hand-back or peer message, other ``isMeta`` input;
``sidechain``     a sub-agent call. Claude Code writes a sub-agent's work in
                  ``<projects>/<project>/<session>/subagents/**/agent-*.jsonl`` (every entry
                  ``isSidechain: true``; workflow agents sit one level deeper, under
                  ``subagents/workflows/wf_*``); older layouts mark ``isSidechain`` inside the main
                  file;
``orphan``        the session HAS a transcript and no assistant message in it has this id: the call
                  was never part of the conversation (a permission classifier, a prompt suggestion,
                  a title or summary side query). On the one measured session, 548 of 813 "turns"
                  were such calls.

``None`` (from :func:`thread_lookup`) means the session has no transcript at all, so nothing can
be said about the call.

"Typed prompt" has one definition, shared with ``o3_integrity.py``: a ``user`` entry that is not a
sidechain, not ``isMeta``, has non-blank text (a tool result has none) and does not start with
``<command-``. Reads message ids and entry kinds only: no prompt text leaves a transcript.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from llm_router import northstar as _ns

ROLE_TURN = "turn"
ROLE_CONTINUATION = "continuation"
ROLE_META = "meta"
ROLE_SIDECHAIN = "sidechain"
ROLE_ORPHAN = "orphan"


def typed_text(content: Any) -> str | None:
    """The text of a user message, or None when it holds none (a tool_result-only message)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def is_typed_prompt(entry: dict) -> bool:
    """A ``user`` transcript entry the owner's definition counts as a typed human prompt."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    msg = entry.get("message")
    text = typed_text(msg.get("content") if isinstance(msg, dict) else None)
    return bool(text and text.strip() and not text.lstrip().startswith("<command-"))


def _scan_main(path: Path, out: dict[str, str]) -> None:
    """File order is time order. ``state`` is what the next assistant message would be the first
    answer to: ``human`` (typed prompt), ``meta`` (slash command, peer message), ``tool`` (a tool
    result) or ``after`` (an assistant message was just seen)."""
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    state = "after"
    with fh:
        for line in fh:
            if '"user"' not in line and '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            kind = d.get("type")
            if kind == "assistant":
                msg = d.get("message")
                mid = msg.get("id") if isinstance(msg, dict) else None
                if not (isinstance(mid, str) and mid):
                    continue
                if d.get("isSidechain") is True:
                    out[mid] = ROLE_SIDECHAIN
                elif mid not in out:   # one message is written as several entries: the first decides
                    out[mid] = {"human": ROLE_TURN, "meta": ROLE_META}.get(state, ROLE_CONTINUATION)
                    state = "after"
            elif kind == "user" and not d.get("isSidechain"):
                msg = d.get("message")
                text = typed_text(msg.get("content") if isinstance(msg, dict) else None)
                if text is None or not text.strip():
                    state = "tool"
                elif d.get("isMeta") or text.lstrip().startswith("<command-"):
                    # injected input never demotes a typed prompt or a tool result it follows
                    if state not in ("human", "tool"):
                        state = "meta"
                else:
                    state = "human"


def _scan_sidechain(path: Path, out: dict[str, str]) -> None:
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if '"assistant"' not in line:   # cheap pre-filter: only assistant entries carry the id
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") != "assistant":
                continue
            msg = d.get("message")
            mid = msg.get("id") if isinstance(msg, dict) else None
            if isinstance(mid, str) and mid:
                out[mid] = ROLE_SIDECHAIN


def thread_index(session_id: str, *, projects_dir: Path | None = None) -> dict[str, str] | None:
    """``{msg_id: role}`` for every assistant message of one session, or None when the session has
    no transcript file at all. An id absent from a returned dict is an ``orphan``."""
    if not isinstance(session_id, str) or not session_id or "/" in session_id or session_id.startswith("."):
        return None
    root = projects_dir if projects_dir is not None else _ns.claude_projects_dir()
    out: dict[str, str] = {}
    found = False
    try:
        for main in root.glob(f"*/{session_id}.jsonl"):
            found = True
            _scan_main(main, out)
        # `**` also reaches workflow agents: <sid>/subagents/workflows/wf_*/agent-*.jsonl
        for sub in root.glob(f"*/{session_id}/subagents/**/*.jsonl"):
            found = True
            _scan_sidechain(sub, out)
    except OSError:
        return None
    return out if found else None


def thread_lookup(*, projects_dir: Path | None = None) -> Callable[[Any, Any], str | None]:
    """``thread_of(session_id, msg_id)`` for ``offload_share.build_units``. Reads each session's
    transcripts once, on first use. Returns a role, ``orphan`` for an id the session's transcript
    does not hold, and None when the session has no transcript (or the row has no ids)."""
    cache: dict[str, dict[str, str] | None] = {}

    def of(sid: Any, mid: Any) -> str | None:
        if not (isinstance(sid, str) and sid and isinstance(mid, str) and mid):
            return None
        if sid not in cache:
            try:
                cache[sid] = thread_index(sid, projects_dir=projects_dir)
            except Exception:  # noqa: BLE001 -- a transcript problem leaves rows unjoined
                cache[sid] = None
        idx = cache[sid]
        return None if idx is None else idx.get(mid, ROLE_ORPHAN)

    return of
