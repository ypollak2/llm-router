"""The single place repo knowledge is attached to a prompt, for ANY model.

OKF reached 2 of 7 execution paths. `router.py` (the MCP `llm()` door) and
`direct_executor.execute_chain` (text-in/text-out drafts) injected it; the tool
loop, the Codex agent, the Claude agent, the Gemini agent and `llm_local_task`
all ran with no repo knowledge at all.

That is not a policy anyone chose. It is what happens when each new execution
path is written next to the others and injection is something you have to
remember. The measurable cost, on one audited day: 101 drafts, zero used, and a
third of them asserting statuses they had no way to observe — a model asked
about a repository it could not see.

So injection stops being a thing each path remembers and becomes a thing each
path CROSSES. `tests/test_okf_choke_point.py` enumerates the execution modules
and fails when one of them does not import this. A new path that forgets is a
red test, not a silent regression discovered weeks later.

Provider-agnostic on purpose. A local 8B model needs the repo context more than
Claude does, not less, and Codex and Gemini are equally blind without it. The
knowledge is about the user's repository; it has nothing to do with who is
answering.
"""
from __future__ import annotations

import os

_DISABLE = ("0", "off", "false", "no")


def enabled() -> bool:
    """`LLM_ROUTER_CONTEXT_INJECTION=off` disables it everywhere at once."""
    return os.environ.get("LLM_ROUTER_CONTEXT_INJECTION", "on").strip().lower() not in _DISABLE


def inject(prompt: str, *, root: str | None = None, limit: int = 3,
           session_id: str | None = None, task_type: str | None = None,
           target_provider: str | None = None,
           session_tokens: int = 1200) -> str:
    """Return *prompt* with relevant repo knowledge prepended, or unchanged.

    Fail-open in every direction: no knowledge, no match, a broken store or an
    unreadable file all return the prompt untouched. Context is an improvement
    to a call, never a precondition for it — a retrieval failure must not turn
    into a routing failure.

    `root` scopes retrieval to a project. Passing it matters more than it looks:
    the MCP server's cwd is $HOME, which has no .git, so unscoped retrieval has
    previously pulled one project's documents into another project's prompt.

    `session_id` adds the conversation and the tool facts recorded for that
    session — what was said AND what was actually done. `target_provider` is
    passed through to the privacy gate, so naming the provider is what keeps
    session content from reaching an external paid API under `local` mode.
    """
    if not enabled() or not prompt:
        return prompt
    body = prompt
    try:
        from pathlib import Path

        from llm_router import okf
        scope = okf.project_root(Path(root)) if root else None
        concepts = (okf.find_relevant(prompt, limit=limit, root=scope)
                    if scope is not None else okf.find_relevant(prompt, limit=limit))
        if concepts:
            body = okf.inject_context(prompt, concepts)
    except Exception:                                        # noqa: BLE001
        body = prompt

    # OKF answers "what does this repo say". It cannot answer "what did we just
    # do", which is what a continuation points at. Both belong to every routed
    # model, not just the hook's draft path — measured 2026-09-14, router.py (the
    # MCP / Codex / Gemini path) made ZERO calls to build_session_context, so a
    # routed model got documents and no idea what happened in the session.
    #
    # build_session_context already enforces the privacy mode (it returns "" when
    # the target is an external paid provider under `local`), already budgets
    # itself with max_tokens, and already wraps its output in the sentinel that
    # stops injected context being re-recorded as new ground truth. So this is
    # wiring, not a new mechanism.
    if not session_id:
        return body
    try:
        from llm_router.session_store import build_session_context
        block = build_session_context(
            session_id, max_tokens=session_tokens, query=prompt,
            task_type=task_type, target_provider=target_provider,
        )
    except Exception:                                        # noqa: BLE001
        return body
    return f"{block}\n\n{body}" if block else body


def inject_system_prompt(system_prompt: str | None, objective: str,
                         *, root: str | None = None) -> str | None:
    """Attach knowledge to a SYSTEM prompt instead of a user prompt.

    The agent loop and the CLI agents carry the task in a system prompt and the
    conversation in messages, so injecting into the user turn would put the
    knowledge where the loop's own context pruning is most likely to evict it.
    Retrieval is still driven by the objective, because that is what describes
    the work.
    """
    if not enabled():
        return system_prompt
    try:
        enriched = inject(objective, root=root)
        if enriched == objective:
            return system_prompt
        block = enriched[: enriched.index(objective)].strip() if objective in enriched else enriched
        if not block:
            return system_prompt
        return f"{block}\n\n{system_prompt}" if system_prompt else block
    except Exception:                                        # noqa: BLE001
        return system_prompt


def render_block(concepts) -> str:
    """Format already-retrieved concepts as a knowledge block, with no prompt.

    The routing hook retrieves concepts to decide whether to open its
    context-dependent gate, then needs the same docs as a block to hand to the
    model. It has the concepts already, so a second retrieval would be waste —
    but the BLOCK FORMAT still has to have one owner, or the label that marks
    this material "retrieved and possibly irrelevant" drifts between call sites.

    Returns "" on any failure; the caller decides what an empty block means.
    """
    if not enabled() or not concepts:
        return ""
    try:
        from llm_router import okf
        return okf.inject_context("", concepts).rstrip()
    except Exception:                                        # noqa: BLE001
        return ""
