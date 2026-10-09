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


_SEED_PATH = None


def _session_seed_query(body: str, session_id: str | None,
                        root: str | None = None) -> str | None:
    """The retrieval query: the prompt plus the files the session just touched.

    I3b (2026-09-24). Retrieval is lexical — it needs an identifier or path in
    the query — and real prompts rarely carry one ("keep going"). Replayed over
    456 real prompts in sessions on this repo: code retrieved for 0.9% from the
    prompt alone, 98.9% with the session's recent file paths added. Only the
    retrieval query changes; the prompt sent to the model does not.
    """
    global _SEED_PATH
    if not session_id:
        return None
    try:
        import re
        from llm_router.session_store import load_events
        if _SEED_PATH is None:
            _SEED_PATH = re.compile(
                r"[\w./-]+\.(?:py|md|toml|json|ya?ml|sh|ts|tsx|js|go|rs|java|rb)\b")
        paths: list[str] = []
        for ev in load_events(session_id, limit=60, project_root=root):
            if ev.get("kind") == "tool_call":
                paths += _SEED_PATH.findall(str(ev.get("content", "")))
        recent = list(dict.fromkeys(reversed(paths)))[:8]
        return f"{body}\n" + " ".join(recent) if recent else None
    except Exception:                                        # noqa: BLE001
        return None  # seeding is an improvement to retrieval, never a precondition


#: Per-turn cap on the prior-turn text folded into a retrieval query. A
#: follow-up needs the NOUNS of the last exchange, not a transcript.
_PRIOR_TURN_CHARS = 600


def _prior_turn_text(session_id: str | None, prompt: str,
                     root: str | None = None) -> str | None:
    """The previous user prompt and the assistant's last reply, newest of each.

    Follow-ups ("do it", "fix that", "rerun it") carry no identifier of their
    own, so lexical retrieval returns nothing for them (REPORT.md item 3, 2026-10-02:
    15 of 40 real prompts). What they point at is the last exchange.

    Same session only: `load_events` reads exactly this session's log inside this
    project's bucket, so another session's turns are unreachable from here. The
    current prompt is skipped by value (a hook may have recorded it already).
    Bounded to two turns of `_PRIOR_TURN_CHARS`. Fail-open: `None` on any error.
    """
    if not session_id:
        return None
    try:
        from llm_router.session_store import load_events
        current = (prompt or "").strip()
        user: str | None = None
        reply: str | None = None
        for ev in reversed(load_events(session_id, limit=40, project_root=root)):
            content = str(ev.get("content") or "").strip()
            if not content or content == current:
                continue
            # The assistant's side is recorded under more than one kind
            # ("claude_answer" from the hook's transcript copy, "routed_qa",
            # "assistant"), so it is recognised by role, not by kind.
            if user is None and ev.get("kind") == "user_prompt":
                user = content[:_PRIOR_TURN_CHARS]
            elif reply is None and ev.get("role") == "assistant":
                reply = content[:_PRIOR_TURN_CHARS]
            if user is not None and reply is not None:
                break
        return "\n".join(p for p in (user, reply) if p) or None
    except Exception:                                        # noqa: BLE001
        return None


_NAMES_SOMETHING = None


def _names_something(prompt: str) -> bool:
    """True when the prompt carries its own retrieval anchor: a file path, a
    snake_case identifier or a CamelCase symbol. Such a prompt can be retrieved
    for as it stands, so it is not widened."""
    global _NAMES_SOMETHING
    if _NAMES_SOMETHING is None:
        import re
        _NAMES_SOMETHING = re.compile(
            r"[\w./-]+\.(?:py|pyi|md|toml|json|ya?ml|sh|ts|tsx|js|go|rs|java|rb)\b"
            r"|\b[A-Za-z]+_[A-Za-z0-9_]+\b"
            r"|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b")
    return bool(_NAMES_SOMETHING.search(prompt or ""))


def is_followup(prompt: str) -> bool:
    """A reply to the previous turn: reply-shaped and with no anchor of its
    own. The one gate every caller uses, so the proxy and the choke point can
    never disagree about which prompts get the prior turn."""
    try:
        from llm_router.context_signal import is_reply_shaped
        return is_reply_shaped(prompt) and not _names_something(prompt)
    except Exception:                                        # noqa: BLE001
        return False


def _retrieval_query(prompt: str, session_id: str | None,
                     root: str | None = None) -> str:
    """What retrieval searches with: the prompt, plus the session's recent files
    (I3b), plus - only for a context-dependent follow-up - the prior turn.

    The follow-up gate is `context_signal.is_reply_shaped` AND the prompt
    naming nothing retrievable itself, so a standalone prompt - or any prompt
    that already carries a path or identifier - keeps exactly the query it
    always had. Only the query changes; the prompt the model receives does not.
    """
    query = _session_seed_query(prompt, session_id, root) or prompt
    if not session_id:
        return query
    try:
        if is_followup(prompt):
            prior = _prior_turn_text(session_id, prompt, root)
            if prior:
                query = f"{query}\n{prior}"
    except Exception:                                        # noqa: BLE001
        pass
    return query


def inject(prompt: str, *, root: str | None = None, limit: int = 3,
           session_id: str | None = None, task_type: str | None = None,
           target_provider: str | None = None,
           session_tokens: int = 1200,
           retrieval_sid: str | None = None,
           session_root: str | None = None,
           reuse_repo_state: bool = False) -> str:
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

    `retrieval_sid` lets a caller that does not know its target provider
    yet (the MCP `llm()` door chooses the model after this runs) use the session
    for the RETRIEVAL QUERY only. No session text is added to the prompt, so
    nothing session-derived can reach an external API through it.

    `reuse_repo_state` lets `repo_facts.render` reuse a recent read (see that
    module); only the context pack sets it, every other caller reads git fresh.

    `session_root` names the project whose session log to read. Left unset the
    log is looked up under this process's cwd, which is right for a hook running
    in the project and wrong for the MCP server (cwd=$HOME) and the proxy.
    """
    if not enabled() or not prompt:
        return prompt
    query = _retrieval_query(prompt, session_id or retrieval_sid, session_root)
    body = prompt
    try:
        from pathlib import Path

        from llm_router import okf
        from llm_router.semantic.scope import resolve_scope_or_none

        # OKF-SCOPE-05 (okf_context_diagnosis.md §3, root cause #5). An explicit
        # root always answers (a caller that named a project meant it). Without
        # one, this used to fall through to `find_relevant(prompt, limit=limit)`
        # with no root at all — which resolves scope via `okf.project_root()`,
        # i.e. `resolve_scope()`, which ALWAYS answers and falls back to the cwd
        # itself when no `.git` is found. For the MCP server that cwd is `$HOME`,
        # a bucket shared by every project on the machine, not "no project" — so
        # an unscoped call did not inject nothing, it injected whatever had been
        # written to that shared bucket by an unrelated repo.
        #
        # `resolve_scope_or_none()` makes the same walk but answers None when
        # nothing actually identifies a project, so a caller with no real scope
        # gets NO documents instead of the wrong ones.
        scope = okf.project_root(Path(root)) if root else resolve_scope_or_none()
        concepts = okf.find_relevant(query, limit=limit, root=scope) if scope is not None else []
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
    # Observed repository state — what is true right now, which neither OKF (what
    # is written) nor the session (what was said) can answer. A continuation like
    # "merge once CI is green" needs the branch; without it a model invents one.
    # Read from git, never from model output, and overwritten every call, so it
    # cannot compound the way a remembered fabrication does.
    try:
        from llm_router.repo_facts import render as _repo_render
        state = _repo_render(root, reuse=reuse_repo_state)
    except Exception:                                        # noqa: BLE001
        state = ""
    if state:
        body = f"{state}\n\n{body}"

    # The semantic layer attaches here or nowhere. It is off by default and its
    # shadow mode is byte-identical to off, so this call changes nothing until
    # someone selects an arm — but it has to EXIST, or "ships dark" quietly
    # means "is not wired in", and those are different claims. The choke point
    # is the only place that may attach context; a second attach path is the
    # defect this module was created to close.
    try:
        from llm_router.semantic import modes as _semantic_modes

        # X3 (2026-09-25): retrieval searches with the user's QUESTION (plus the
        # session's recent files), never with `body` — by now `body` carries the
        # OKF knowledge block and repo-state, and every name in them became a
        # seed, crowding out the function the user actually asked about.
        applied = _semantic_modes.apply(
            body, root=root,
            seed_query=query)
        body = applied.prompt
    except Exception:                                        # noqa: BLE001
        pass  # retrieval is an improvement to a call, never a precondition

    if not session_id:
        return body
    try:
        from llm_router.session_store import build_session_context
        block = build_session_context(
            session_id, max_tokens=session_tokens, query=prompt,
            task_type=task_type, target_provider=target_provider,
            project_root=session_root,
        )
    except Exception:                                        # noqa: BLE001
        return body
    return f"{block}\n\n{body}" if block else body


def inject_system_prompt(system_prompt: str | None, objective: str,
                         *, root: str | None = None,
                         session_id: str | None = None,
                         limit: int = 3,
                         target_provider: str | None = None,
                         session_tokens: int = 1200,
                         session_root: str | None = None) -> str | None:
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
        enriched = inject(objective, root=root, session_id=session_id, limit=limit,
                          target_provider=target_provider,
                          session_tokens=session_tokens,
                          session_root=session_root)
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
