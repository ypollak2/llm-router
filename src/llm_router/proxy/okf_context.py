"""Repo knowledge for the proxy's local tier.

The proxy serves a Claude Code continuation step from a local model. That model
receives the trimmed request only (``backends._trim_fast``: a 366-char system
prompt, four tools, the first ask and the last exchange) and so knows nothing
about the repository it is acting on. ``okf_context_diagnosis.md`` (2026-10-01)
found this was one of three local-serving paths that attached no OKF at all.

This module is the proxy's crossing of the single injection choke point
(``llm_router.context_injection``, enforced by ``tests/test_okf_choke_point.py``).
It adds one block to the SYSTEM prompt of the request about to go to the local
backend, and nowhere else: the Claude pass-through path is untouched, and the
client's own request is never mutated.

Four rules, each a way this could cost a call instead of helping one:

* **Scope is the session's project, or nothing.** The cwd comes from the
  request's own environment block (what Claude Code sends), else from the
  session-cwd pointer the hook wrote. A cwd that is unknown, not a directory or
  not inside a git repo gets NO injection: the proxy process's own cwd means
  nothing, and ``$HOME`` is a bucket shared by every project on the machine.
* **Budget.** The local-agent compaction target is ~5k prompt tokens
  (``local_agent.DEFAULT_MAX_PROMPT_TOKENS``). The block may only use what the
  trimmed request leaves of that, and never more than ``MAX_BLOCK_TOKENS``; it
  is retried with fewer documents before it is dropped. It is never truncated
  mid-document.
* **Fail open.** Any error returns the request unchanged with a reason. Context
  is an improvement to a step, never a precondition for serving it.
* **Off the event loop.** Retrieval reads the store and the repo-state line runs
  git, so ``attach`` is synchronous and the server calls it in a thread.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from llm_router.local_agent import DEFAULT_MAX_PROMPT_TOKENS
from llm_router.local_agent.compact import estimate_tokens, session_cwd
from llm_router.proxy.steps import session_id_of, tier_text
from llm_router.session_store import SENTINEL_OPEN as _SESSION_TAG

#: Hard cap on the injected block, whatever room the request leaves.
MAX_BLOCK_TOKENS = 1500
#: Below this much room the block is not worth the churn.
MIN_ROOM_TOKENS = 200
#: Documents tried, most to fewest, until the block fits the budget.
_LIMITS = (3, 2, 1)

#: The sentinel okf.inject_context wraps retrieved documents in.
_KNOWLEDGE_TAG = "<knowledge_context>"

#: Session text a follow-up may carry into the local request. Small on purpose:
#: the whole block shares ``MAX_BLOCK_TOKENS`` with the retrieved documents.
SESSION_BLOCK_TOKENS = 300

REASON_INJECTED = "injected"
REASON_DISABLED = "disabled"
REASON_NO_SCOPE = "no_scope"
REASON_NO_QUERY = "no_query"
REASON_NO_MATCH = "no_match"
REASON_NO_ROOM = "no_room"
REASON_OVER_BUDGET = "over_budget"
REASON_ERROR = "error"


def _tokens(text: str) -> int:
    return len(text) // 4  # the estimate local_agent.compact budgets with


def session_scope(body: dict) -> str | None:
    """The project root this request's session works in, or None.

    None when the cwd is unknown, is not a directory, or sits outside any git
    repo. ``semantic.scope.resolve_scope`` would answer for such a cwd with the
    directory itself; that is right for a store that must live somewhere and
    wrong here, where "no known project" has to mean "inject nothing".
    """
    from llm_router.semantic.scope import resolve_scope
    from llm_router.session_store import read_session_cwd

    cwd = session_cwd(body) or read_session_cwd(session_id_of(body))
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    if not path.is_dir():
        return None
    root = resolve_scope(path)
    return str(root) if (root / ".git").exists() else None


def _with_block(send: dict, block: str) -> dict:
    """*send* with *block* first in its system prompt (a shallow copy)."""
    system = send.get("system")
    if isinstance(system, list):
        merged = [{"type": "text", "text": block}, *system]
    elif isinstance(system, str) and system:
        merged = f"{block}\n\n{system}"
    else:
        merged = block
    return dict(send, system=merged)


def _request_text(send: dict) -> str:
    """The text of *send*'s messages (not its system prompt): text blocks, tool inputs and tool results."""
    parts: list[str] = []

    def add(value) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            kind = value.get("type")
            if kind == "tool_use":
                parts.append(json.dumps(value.get("input"), ensure_ascii=False, default=str))
            elif kind == "tool_result":
                add(value.get("content"))
            else:
                add(value.get("text"))

    for message in send.get("messages") or []:
        if isinstance(message, dict):
            add(message.get("content"))
    return "\n".join(parts)


def attach(send: dict, original: dict, *,
           ceiling_tokens: int = DEFAULT_MAX_PROMPT_TOKENS,
           fits: Callable[[dict], bool] | None = None,
           dedupe_session: bool = False) -> tuple[dict, dict]:
    """``(request_for_the_local_backend, info)``.

    *send* is the already-trimmed (or compacted) request; *original* is the
    client's request, the only place the cwd and the user's ask are reliably
    present. The returned request is a shallow copy with the block added to its
    system prompt, or *send* itself when nothing was added. *info* always says
    why, for the ledger.

    *dedupe_session* (default False: the block is built exactly as before) leaves out of the prior-turn
    session block every event whose text *send* already carries verbatim. The hook records the same tool
    calls and results the request holds, and the three newest events are by construction the ones in its
    verbatim tail, so beside MR summaries the block would repeat the tail. Events the request lost to
    summarisation are not covered, so they stay.
    """
    from llm_router import context_injection, failopen

    info: dict = {"injected": False, "reason": REASON_DISABLED}
    try:
        if not context_injection.enabled():
            return send, info
        root = session_scope(original)
        if root is None:
            info["reason"] = REASON_NO_SCOPE
            return send, info
        query = tier_text(original)
        if not query:
            info["reason"] = REASON_NO_QUERY
            return send, info
        room = min(MAX_BLOCK_TOKENS, ceiling_tokens - estimate_tokens(send))
        if room < MIN_ROOM_TOKENS:
            info.update(reason=REASON_NO_ROOM, room_tokens=room)
            return send, info

        # A follow-up ("do it", "fix that") names nothing, so on its own it
        # retrieves nothing. Only for those, hand the choke point this session
        # (same id, same project root: it cannot read another session's log) so
        # it can search with the prior turn and add a small prior-turn block.
        # The target is the local tier, which is what the privacy gate asks.
        # A standalone prompt takes the exact path it always took.
        sid = None
        if context_injection.is_followup(query):
            sid = session_id_of(original)

        covered = _request_text(send) if sid and dedupe_session else None
        info["reason"] = REASON_NO_MATCH
        block = None
        for limit in _LIMITS:
            if sid:
                block = context_injection.inject_system_prompt(
                    None, query, root=root, limit=limit, session_id=sid,
                    target_provider="local", session_tokens=SESSION_BLOCK_TOKENS,
                    session_root=root, session_covered_text=covered)
            else:
                block = context_injection.inject_system_prompt(
                    None, query, root=root, limit=limit)
            if not block or (_KNOWLEDGE_TAG not in block
                             and not (sid and _SESSION_TAG in block)):
                # Nothing retrieved. (inject also attaches the repo-state line
                # for any git repo; a project with no matching knowledge gets
                # nothing from this module, not just that line.)
                block = None
                break  # fewer documents cannot change that
            # *fits* (optional) judges the finished request with the caller's own estimators.
            if _tokens(block) <= room and (fits is None or fits(_with_block(send, block))):
                break
            info["reason"] = REASON_OVER_BUDGET
            block = None
        if not block:
            info["room_tokens"] = room
            return send, info

        info.update(injected=True, reason=REASON_INJECTED, tokens=_tokens(block), room_tokens=room)
        return _with_block(send, block), info
    except Exception as exc:  # noqa: BLE001 - fail-open: a step is served with or without context
        failopen.record("LR-FO-PROXY-OKF-ATTACH", exc)
        info.update(injected=False, reason=REASON_ERROR)
        return send, info
