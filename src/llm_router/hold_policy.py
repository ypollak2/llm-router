"""Which prompts may hold tools, and what releases a hold (BIND W2).

The defect. The PreToolUse hold used to tell the model "call ANY llm_* tool (even a
trivial llm(task="query"))" to clear it. The model did: it sent "Reply OK" probes whose
only purpose was to release the lock. Over 2026-10-04..2026-11-03 (BIND-R2, the
coordinator's read of the 158 ``routed_mcp`` units) 76 of the 141 unused MCP units were
such probes, 0 of 133 router calls forwarded the owner's prompt, and the toll calls and
the blocked attempts before them cost 104 Claude turns. A probe was never routing: the
answer was thrown away and the hold was a toll.

Two rules replace the escape valve, one per hook, kept here so each is defined once.

1. NO HOLD FOR A PROMPT THAT IS NOT A REQUEST. :func:`hold_exempt_reason` names the
   prompts that never create a hold. ``auto-route.py`` applies it in ``smart`` mode (the
   default, and the mode the audit measured); ``hard`` and ``strict``, the explicit
   opt-in to hold everything, keep every hold, as does a user-forced ``local:`` /
   ``codex:`` prompt, and the modes that never block have no hold to take away:

   * ``short_prompt`` - ten words or fewer. Evidence (own replication of the audit, the
     organic sessions of the 30-day window, 652 counted prompts): 40 unique BLOCKED
     tool results; the prompt before each had 3 words (9 times), 4 (3), 5 (4), 6 (8),
     7 (2), 9 (1), 10 (2) - 29 of 40 at ten words or fewer - then nothing at 11, 12 or
     13 words, then 14, 18, 24, 33, 55, 55, 59. The toll probes show the same gap: of
     95 probe calls (BIND-R2 appendix A, class e) 70 followed a prompt of ten words or
     fewer, none followed one of 11 or 12. "keep going", "continue the plan" and "deploy
     main now" are acknowledgements, continuations and asks about the session: a cheaper model has nothing to do with them, so holding a
     tool for them only invites a toll. Ten is the largest value before the observed
     gap. It is a line through one machine's gap, not a judgement of the prompt: a
     self-contained ten-word question is not held in smart mode either
     (docs/bugs/BIND-HOLD-1.md, Known limits).
   * ``session_continuation`` - the "This session is being continued from a previous
     conversation" summary Claude Code injects after a compaction. 4 of the 40 holds
     and 9 of the 95 probes followed one (1,863 to 2,804 words each). It is machine
     text, not a request.

2. A HOLD IS RELEASED BY ROUTING THE OWNER'S REQUEST. :func:`forwards_owner_prompt`
   decides whether a routing-door call carries the owner's prompt. A call that does
   not (a trivial probe, a status view, an unrelated question) leaves the hold in
   place; :func:`is_route_door` names the tools that can carry a request at all.

The explicit bypass for the user is the existing ``claude:`` prefix (docs/proxy.md,
"Routing prefixes"): the prompt hook writes no hold for it and drops a stale one.

Stdlib only: the PreToolUse hook imports this and must stay light.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = [
    "REASON_SESSION_CONTINUATION",
    "REASON_SHORT_PROMPT",
    "SHORT_PROMPT_MAX_WORDS",
    "ROUTE_DOORS",
    "content_words",
    "forwards_owner_prompt",
    "hold_exempt_reason",
    "is_route_door",
    "words_in",
]

#: A prompt of at most this many whitespace-separated words never holds a tool.
SHORT_PROMPT_MAX_WORDS = 10

REASON_SHORT_PROMPT = "short_prompt"
REASON_SESSION_CONTINUATION = "session_continuation"

_CONTINUATION_RE = re.compile(
    r"^\s*This session is being continued from a previous conversation", re.IGNORECASE
)


def words_in(text: str | None) -> int:
    """Whitespace-separated words, the unit the short-prompt rule is stated in."""
    return len((text or "").split())


def hold_exempt_reason(prompt: str | None) -> str | None:
    """Why *prompt* must not create (or keep) a hold, or None when it may.

    An empty or missing prompt is NOT exempt: it carries no evidence either way, and a
    pending file written by an older hook without ``original_prompt`` keeps today's
    behaviour.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if _CONTINUATION_RE.match(prompt):
        return REASON_SESSION_CONTINUATION
    if words_in(prompt) <= SHORT_PROMPT_MAX_WORDS:
        return REASON_SHORT_PROMPT
    return None


# ── what releases a hold ─────────────────────────────────────────────────────

#: The ``llm_*`` tools that take a request and answer or act on it. Every other tool
#: under the ``llm_`` prefix (status, admin, session, budget and usage views) reports
#: or configures and can never carry the owner's prompt, so calling one never
#: releases a hold. Names are the bare tool names, without the ``mcp__<server>__``
#: prefix.
ROUTE_DOORS = frozenset({
    "llm", "llm_route", "llm_act", "llm_delegate", "llm_edit", "llm_local_task",
    "llm_text_job", "llm_query", "llm_analyze", "llm_code", "llm_research",
    "llm_generate", "llm_reason", "llm_classify", "llm_auto", "llm_image", "llm_audio",
    "llm_router_agent_route",
})


def is_route_door(tool_name: str | None) -> bool:
    """True when *tool_name* (bare or ``mcp__<server>__<tool>``) is a routing door."""
    bare = (tool_name or "").split("__")[-1]
    return bare in ROUTE_DOORS


#: Keys of a door call's input that carry the request. ``context`` and
#: ``system_prompt`` are deliberately absent: they are material handed along, and a
#: probe that drags a large unrelated context behind it must not read as a forward.
_INSTRUCTION_KEYS = (
    "prompt", "objective", "query", "question", "input_text", "instruction",
    "message", "description", "text",
)
#: ``task`` is the request for llm_act / llm_edit but only a category word (query, code,
#: auto) for llm and llm_route, which also take ``prompt``. It counts as the request only
#: when no other instruction key is present.
_TASK_KEY = "task"
_MATERIAL_KEYS = frozenset({
    "context", "system_prompt", "files", "paths", "workdir", "acceptance_check",
    "model", "session_id", "task_type", "tier", "complexity", "complexity_override",
})
_MAX_CALL_CHARS = 20_000

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = frozenset((
    "the and for that this with from have has had are was were been being will would "
    "should could can may might shall not but all any each every some such than then "
    "them they their there these those what when where which while who whom whose why "
    "how into onto over under about above after before again also just only very more "
    "most much many few other another own same too out off per via you your yours our "
    "ours his her its him she him let get got make made use used using please thanks "
    "thank yes yeah okay now new one two want need like see look give take put run set "
    "try keep going ahead here still even ever never always already else rather quite "
    "really maybe perhaps"
).split())

#: A forward is judged on shared content words. Below this many in the owner's prompt
#: there is nothing to compare, and the call is given the benefit of the doubt.
MIN_OWNER_CONTENT_WORDS = 3
#: The call restates at least this share of the owner's content words ...
RECALL_MIN = 0.5
#: ... or it is mostly made of the owner's words (a quoted excerpt of a long prompt),
#: with at least this many shared.
PRECISION_MIN = 0.6
MIN_SHARED_FOR_PRECISION = 4
MIN_SHARED_FOR_RECALL = 3


def content_words(text: str | None) -> frozenset[str]:
    """Lower-cased words of three letters or more that are not function words."""
    return frozenset(
        w for w in _WORD_RE.findall((text or "").lower())
        if len(w) >= 3 and w not in _STOP and not w.isdigit()
    )


def _strings(value: Any, depth: int = 0) -> list[str]:
    if isinstance(value, str):
        return [value]
    if depth >= 2:
        return []
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _strings(item, depth + 1)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item, depth + 1)]
    return []


def call_text(tool_input: Any) -> str:
    """The part of a door call's input that states the request (bounded)."""
    if isinstance(tool_input, str):
        return tool_input[:_MAX_CALL_CHARS]
    if not isinstance(tool_input, dict):
        return ""
    parts: list[str] = []
    present = [key for key in _INSTRUCTION_KEYS if key in tool_input]
    if not present and _TASK_KEY in tool_input:
        present = [_TASK_KEY]
    for key in present:
        parts.extend(_strings(tool_input[key]))
    if not present:  # a door with a schema this module does not know: everything but the material
        for key, value in tool_input.items():
            if key not in _MATERIAL_KEYS:
                parts.extend(_strings(value))
    return "\n".join(parts)[:_MAX_CALL_CHARS]


def forwards_owner_prompt(owner_prompt: str | None, tool_input: Any) -> bool | None:
    """Does a routing-door call carry the owner's prompt?

    True: the call restates at least half of the owner's content words (and three of
    them), or is made mostly (60%, at least four shared) of the owner's words, as a
    quoted excerpt of a long prompt is. False: it does not, so it is a probe. None: the
    owner's prompt has fewer than three content words, so there is nothing to compare;
    the caller treats that as a release (the old behaviour) rather than hold forever.

    BIND-R2 measured the other side: the 4-gram overlap of all 133 router calls with the
    owner's preceding message was under 20%.
    """
    owner = content_words(owner_prompt)
    if len(owner) < MIN_OWNER_CONTENT_WORDS:
        return None
    call = content_words(call_text(tool_input))
    if not call:
        return False
    shared = len(owner & call)
    if shared >= MIN_SHARED_FOR_RECALL and shared / len(owner) >= RECALL_MIN:
        return True
    if shared >= MIN_SHARED_FOR_PRECISION and shared / len(call) >= PRECISION_MIN:
        return True
    return False
