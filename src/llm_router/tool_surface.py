"""Tool surface resolution — the ONE place that answers "what may we tell the
caller to call?".

Why this module exists (CHZ-SURF-01)
────────────────────────────────────
The routing hooks classify a prompt and then *name a tool* in the directive they
inject ("⚡ ROUTE: code/moderate → call llm_code"). That name is a promise: the
caller will type it verbatim. If the named tool is not registered on the running
MCP server, the caller gets ``Error: No such tool available``, silently gives up,
and does the work on the expensive model. The savings dashboard cannot see this
— an unroutable hint looks exactly like "the model chose not to route".

That is precisely what shipped: ``hooks/auto-route.py`` emitted the legacy tool
names (llm_query / llm_analyze / llm_code / llm_research / llm_generate) while
``LLM_ROUTER_SLIM`` defaulted to ``consolidated``, a tier under which *none of them
are registered* — they are collapsed behind the unified ``llm(task=…)`` door.

The breakage was never consolidated-only. Every slim tier registers a different
subset, and the emitters hardcoded one fixed vocabulary:

    core          4 of 7 route targets unregistered
    routing       1 of 7 unregistered
    consolidated  5 of 7 unregistered
    off           0 (everything registered)

So the fix is not "special-case consolidated". It is: no emitter may name a tool
directly. Every emitter asks :func:`resolve`, which is tier-aware and guarantees
a registered answer, and :func:`unregistered` proves that guarantee holds for
every (emitter name × tier) pair in CI and at server startup.

Deliberately dependency-free
────────────────────────────
Hooks are standalone scripts on the UserPromptSubmit critical path, and they are
not guaranteed to run under an interpreter that has ``llm_router`` importable (the
installer prefers ``sys.executable``, but falls back to bare ``python3``). This
module therefore imports NOTHING outside the stdlib, so a hook can load it by
path as a last resort and still get correct answers.

Keep it that way. Everything else depends on this module, never the reverse:
``llm_router.tool_tiers`` re-exports the tier sets from here, and
``llm_router.tools.consolidated`` re-exports :data:`DEPRECATED_TOOLS` from here.
Adding a ``llm_router.*`` import to this file re-breaks the standalone path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CORE_TOOLS",
    "ROUTING_TOOLS",
    "CONSOLIDATED_TOOLS",
    "DEPRECATED_TOOLS",
    "ToolCall",
    "active_slim",
    "registered_tools",
    "resolve",
    "resolve_name",
    "route_tool",
    "route_call",
    "route_call_with_complexity",
    "call_parts",
    "localize",
    "door_name",
    "KNOWN_TOOLS",
    "is_registered",
    "unregistered",
    "TASK_TOOL_MAP",
    "DEFAULT_TASK_TOOL",
    "tool_for_task",
    "implemented_tools",
    "phantom_tools",
    "EMITTABLE_TOOLS",
]

# ── Task type → tool (canonical home; RED8-06) ───────────────────────────────
# Three independently-maintained copies of this map existed — auto-route.py
# (8 keys), agent-route.py (5 keys) and service.py (5 keys) — with TWO DIFFERENT
# fallbacks for an unrecognised task type. The five shared keys agreed, so they
# looked consistent; the divergence was in what happened to everything else.
#
# auto-route fell back to llm_route, which can pick a tool. agent-route fell back
# to llm_analyze, a COMPLETION DOOR that cannot run tools. So the same ambiguous
# prompt reached a working router on one path and a structural dead-end on the
# other — the outcome NORTH_STAR lists first among its anti-goals.
#
# It lives here because this module is deliberately dependency-free and loadable
# by path: a hook running under a bare `python3` with no llm_router importable can
# still read it. That constraint is precisely why the copies existed.
TASK_TOOL_MAP: dict[str, str] = {
    "research": "llm_research",
    "generate": "llm_generate",
    "analyze": "llm_analyze",
    "code": "llm_code",
    "query": "llm_query",
    "image": "llm_image",
    # Coordination is cheap and wants an instant decision, not deep reasoning.
    "coordination": "llm_query",
    "auto": "llm_route",
}

#: Fallback for an unrecognised task type. MUST be a tool that can route to
#: others — a completion door here silently caps the task's capability.
DEFAULT_TASK_TOOL = "llm_route"


def tool_for_task(task_type: str) -> str:
    """Canonical task->tool lookup. Use this instead of a private dict."""
    return TASK_TOOL_MAP.get(task_type, DEFAULT_TASK_TOOL)


# ── Tier membership (canonical home; re-exported by llm_router.tool_tiers) ───────
CORE_TOOLS: frozenset[str] = frozenset({
    "llm_query",
    "llm_code",
    "llm_research",
    "llm_usage",
})
"""4-tool tier — essential tools only. Maximum token savings (~7,500 tokens saved)."""

ROUTING_TOOLS: frozenset[str] = CORE_TOOLS | frozenset({
    "llm_analyze",
    "llm_generate",
    "llm_classify",
    "llm_route",
    "llm_auto",
    "llm_check_usage",
    "llm_set_profile",
    "llm_health",
    "llm_session_spend",
    "llm_session_savings",  # v10.1.0 — tier-grouped savings dashboard
    "llm_savings",
    "llm_reroute",
    "llm_select_agent",
})
"""12-tool tier — routing + core admin tools. Recommended for most users (~5,000 tokens saved)."""

# North Star 1.0 cutover (staged): the CONSOLIDATED front-door surface. Opt into it
# with LLM_ROUTER_SLIM=consolidated to *run* the collapsed ~11-tool surface today (old
# tools hidden, not removed). This validates the doors cover every capability before
# the breaking 1.0 step that actually removes the 73 old tools.
CONSOLIDATED_TOOLS: frozenset[str] = frozenset({
    "llm",             # unified completion door (query/analyze/code/research/generate)
    "llm_act",         # agentic execution door (delegation)
    "llm_router_status",   # observability door (savings/usage/health/…)
    "llm_router_admin",    # config door (set_profile/clear_cache/…)
    "llm_router_session",  # agent-lifecycle door (list/check_budget/complete/lineage)
    "llm_route",       # auto-routing decision (no door alias yet)
    "llm_image",       # media (future: llm_media)
    "llm_audio",       # media (future: llm_media)
    "llm_edit",        # file ops (future: llm_fs)
    "llm_router_agent_start_session",  # rich session action (kept until llm_router_session covers it)
    "llm_router_agent_route",          # rich session action
})
"""~11-tool CONSOLIDATED front-door tier (North Star 1.0 direction)."""


# ── Legacy tool → consolidated front door ────────────────────────────────────
# Single source of truth for the 1.0 cutover. ``llm_router.tools.consolidated`` and
# ``hooks/enforce-route.py`` both consume this; neither keeps a private copy.
DEPRECATED_TOOLS: dict[str, str] = {
    # completion → llm
    "llm_query": "llm", "llm_analyze": "llm", "llm_code": "llm",
    "llm_research": "llm", "llm_generate": "llm",
    # RED1-21: llm_reason was in NONE of the three surface sets, so localize()
    # never rewrote it and the lint never saw it — while three generated rules
    # tables (install_hooks.py, cli.py x2) instructed models to call it. Emitted
    # but unknown to the resolver is the worst pairing available: taught to the
    # model, invisible to the guard.
    "llm_reason": "llm",
    # agentic → llm_act
    "llm_delegate": "llm_act",
    # observability → llm_router_status
    "llm_savings": "llm_router_status", "llm_session_savings": "llm_router_status",
    "llm_session_spend": "llm_router_status", "llm_usage": "llm_router_status",
    "llm_health": "llm_router_status", "llm_providers": "llm_router_status",
    "llm_gain": "llm_router_status", "llm_dashboard": "llm_router_status",
    "llm_check_usage": "llm_router_status",  # emitted by agent-route's stale-quota note
    # config → llm_router_admin
    "llm_set_profile": "llm_router_admin", "llm_import_profile": "llm_router_admin",
    "llm_cache_clear": "llm_router_admin", "llm_policy": "llm_router_admin",
    "llm_budget": "llm_router_admin",
    # agent lifecycle → llm_router_session
    "llm_router_agent_list": "llm_router_session", "llm_router_agent_check_budget": "llm_router_session",
    "llm_router_agent_complete_session": "llm_router_session", "llm_router_agent_lineage": "llm_router_session",
}

# The unified ``llm`` door needs a discriminator to preserve the specialization
# that the legacy tool name carried. Mapping llm_code → "llm" alone LOSES the
# routing decision; the caller must be told ``llm(task="code")``.
_DOOR_TASK_ARG: dict[str, str] = {
    "llm_query": "query",
    "llm_analyze": "analyze",
    "llm_code": "code",
    "llm_research": "research",
    "llm_generate": "generate",
    # Deep reasoning maps to the analyze specialization and deliberately pins NO
    # tier. Pinning tier="best" would look faithful to the old name and would be
    # North-Star-negative: it sends every reasoning call straight to the frontier
    # instead of letting the router escalate only when a cheaper tier misses the
    # quality bar. The task carries the specialization; the tier stays a routing
    # decision.
    "llm_reason": "analyze",
}

# Capability-ordered degradation used when neither the tool NOR its front door is
# registered in the active tier (e.g. llm_analyze under ``core``). Each chain is
# ordered most-faithful-first and every chain ends in a tool that is present in
# the narrowest tier (``core``), so resolution can never run out of options.
_FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    "llm_analyze": ("llm_code", "llm_query"),
    "llm_generate": ("llm_query", "llm_code"),
    "llm_research": ("llm_query",),
    "llm_code": ("llm_query",),
    "llm_query": ("llm_code",),
    "llm_route": ("llm_query", "llm_code"),
    "llm_image": ("llm_route", "llm_query"),
    "llm_audio": ("llm_route", "llm_query"),
    "llm_act": ("llm_delegate", "llm_code", "llm_query"),
    "llm_delegate": ("llm_act", "llm_code", "llm_query"),
    "llm_edit": ("llm_act", "llm_code", "llm_query"),
    "llm_classify": ("llm_route", "llm_query"),
    # Observability/admin: degrade to a sibling that still reports something
    # real, never to a completion tool (which would answer a spend question
    # with a hallucinated paragraph).
    "llm_savings": ("llm_usage", "llm_health"),
    "llm_usage": ("llm_savings", "llm_health"),
    "llm_health": ("llm_usage", "llm_savings"),
    "llm_check_usage": ("llm_usage", "llm_savings"),
}

# Last-resort target per tier. Guaranteed registered; used only if every chain
# entry somehow misses, so :func:`resolve` is total.
_TIER_FLOOR: dict[str, str] = {
    "core": "llm_query",
    "routing": "llm_query",
    "consolidated": "llm",
    "off": "llm_query",
}

# Every tool name this module has an opinion about. Anything outside it is a
# third-party or future tool and must pass through untouched.
KNOWN_TOOLS: frozenset[str] = (
    CORE_TOOLS | ROUTING_TOOLS | CONSOLIDATED_TOOLS
    | frozenset(DEPRECATED_TOOLS) | frozenset(_FALLBACK_CHAINS)
)

_TIERS: dict[str, frozenset[str] | None] = {
    "core": CORE_TOOLS,
    "routing": ROUTING_TOOLS,
    "consolidated": CONSOLIDATED_TOOLS,
    "off": None,  # None == every tool is registered
}

# Every tool name any emitter in the codebase is allowed to produce. The guard
# test walks this × every tier and asserts resolution lands on a registered tool.
# ADD A NAME HERE when a new emitter learns to suggest a new tool — the guard
# will immediately tell you which tiers cannot serve it.
EMITTABLE_TOOLS: frozenset[str] = frozenset({
    "llm_query", "llm_analyze", "llm_code", "llm_research", "llm_generate",
    "llm_route", "llm_image", "llm_audio", "llm_classify",
    "llm_delegate", "llm_act", "llm_edit",
    "llm_savings", "llm_usage", "llm_health", "llm_check_usage",
})


def active_slim() -> str:
    """Return the slim tier in force.

    Reads ``LLM_ROUTER_SLIM`` directly rather than importing ``llm_router.config`` so
    hooks stay light. The default MUST track ``Config.llm_router_slim`` — an unset
    env var means ``consolidated``, not ``off``. Getting this default wrong is
    the original bug (an emitter assuming the legacy surface).
    """
    return (os.environ.get("LLM_ROUTER_SLIM") or "consolidated").strip().lower()


def registered_tools(slim: str | None = None) -> frozenset[str] | None:
    """Tools registered under ``slim``; ``None`` means "all of them" (tier off).

    Unknown tier names mirror :func:`llm_router.tool_tiers.make_should_register` and
    resolve to "everything registered", so a typo degrades to permissive rather
    than to a wrong-name hint.
    """
    tier = (slim or active_slim()).strip().lower()
    return _TIERS.get(tier, None)


def is_registered(name: str, slim: str | None = None) -> bool:
    """True if ``name`` is actually callable on a server running ``slim``."""
    reg = registered_tools(slim)
    return True if reg is None else name in reg


@dataclass(frozen=True)
class ToolCall:
    """A tool the caller can actually invoke, plus any pinned arguments.

    ``name`` is safe to interpolate wherever a bare tool name is wanted;
    :meth:`render` produces the full invocation. Never build an invocation by
    concatenating ``str(call)`` with ``"(prompt=…)"`` — that yields
    ``llm(task="code")(prompt=…)``. Use :meth:`render` instead.
    """

    name: str
    pinned: tuple[tuple[str, str], ...] = field(default=())
    #: The logical tool the emitter asked for, before tier resolution.
    logical: str = ""
    #: True when the tier forced a capability downgrade (no faithful door).
    degraded: bool = False

    def render(self, *extra_args: str) -> str:
        """Full call form, e.g. ``llm(task="code", prompt=…)``."""
        args = [f'{k}="{v}"' for k, v in self.pinned]
        args.extend(a for a in extra_args if a)
        return f"{self.name}({', '.join(args)})" if args else f"{self.name}()"

    @property
    def display(self) -> str:
        """Bare-but-unambiguous form for prose, e.g. ``llm(task="code")``."""
        if not self.pinned:
            return self.name
        # Built outside the f-string: backslashes/nested quotes inside an
        # f-string expression are a SyntaxError before Python 3.12, and this
        # package supports >=3.10.
        args = ", ".join(f'{k}="{v}"' for k, v in self.pinned)
        return f"{self.name}({args})"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.display


def resolve(logical: str, slim: str | None = None) -> ToolCall:
    """Map a logical tool name onto something registered under ``slim``.

    This is total: it always returns a :class:`ToolCall` whose ``name`` is
    registered in the tier (verified by the guard test across every tier).

    Resolution order:
      1. ``logical`` itself, if registered.
      2. Its consolidated front door, carrying the ``task=`` discriminator so
         the specialization survives (llm_code → ``llm(task="code")``).
      3. Capability-ordered fallback chain (marked ``degraded``).
      4. The tier floor.
    """
    tier = (slim or active_slim()).strip().lower()
    reg = registered_tools(tier)

    # Tier "off" (or unknown): everything is registered, nothing to translate.
    if reg is None:
        return ToolCall(logical, logical=logical)

    if logical in reg:
        return ToolCall(logical, logical=logical)

    door = DEPRECATED_TOOLS.get(logical)
    if door and door in reg:
        task = _DOOR_TASK_ARG.get(logical)
        pinned = (("task", task),) if task and door == "llm" else ()
        return ToolCall(door, pinned, logical=logical)

    for candidate in _FALLBACK_CHAINS.get(logical, ()):
        if candidate in reg:
            return ToolCall(candidate, logical=logical, degraded=True)
        # A chain entry may itself only exist behind a door in this tier.
        cand_door = DEPRECATED_TOOLS.get(candidate)
        if cand_door and cand_door in reg:
            task = _DOOR_TASK_ARG.get(candidate)
            pinned = (("task", task),) if task and cand_door == "llm" else ()
            return ToolCall(cand_door, pinned, logical=logical, degraded=True)

    # A name we know nothing about (a third-party MCP tool, a future tool) is
    # returned UNCHANGED. Substituting the tier floor here would be a guess, and
    # this value is also compared against the tool the caller actually invoked —
    # rewriting an unknown name silently breaks that match.
    if logical not in KNOWN_TOOLS:
        return ToolCall(logical, logical=logical)

    floor = _TIER_FLOOR.get(tier, "llm_query")
    if floor not in reg:  # pragma: no cover - defensive
        floor = next(iter(sorted(reg)))
    return ToolCall(floor, logical=logical, degraded=True)


def door_name(logical: str, slim: str | None = None) -> str:
    """The registered name to MATCH against, never a guess.

    Distinct from :func:`resolve` on purpose. ``resolve`` may degrade a known tool
    to a capable substitute so a *hint* always names something callable. Matching
    must not do that: ``_door_for`` in enforce-route compares this against the tool
    the caller actually invoked, and substituting a different name there turns a
    correct call into a recorded violation. So: the explicit door if one exists and
    is registered, otherwise the name exactly as given.
    """
    reg = registered_tools(slim)
    if reg is None or logical in reg:
        return logical
    door = DEPRECATED_TOOLS.get(logical)
    return door if door and door in reg else logical


def resolve_name(logical: str, slim: str | None = None) -> str:
    """Convenience: the bare registered tool name (no pinned args)."""
    return resolve(logical, slim).name


def route_tool(logical: str, slim: str | None = None) -> str:
    """Display form of a tool the caller can invoke, e.g. ``llm(task="code")``.

    The convenience entry point for emitters: never interpolate a raw tool name
    into user-visible text, always pass it through here first.
    """
    return resolve(logical, slim).display


def route_call(logical: str, *args: str, slim: str | None = None) -> str:
    """Full invocation form, e.g. ``llm(task="code", prompt="…")``.

    Use this rather than appending arguments to :func:`route_tool`, which would
    produce the uncallable ``llm(task="code")(prompt="…")``.
    """
    return resolve(logical, slim).render(*args)


# The unified `llm` door takes tier=fast|balanced|best; the legacy completion
# tools take complexity=simple|moderate|complex. Inverse of
# `llm_router.tools.consolidated._TIER_TO_COMPLEXITY` — renaming the TOOL without
# also translating its ARGUMENTS just swaps a "no such tool" error for an
# "unexpected keyword argument" one.
_COMPLEXITY_TO_TIER: dict[str, str] = {
    "simple": "fast",
    "moderate": "balanced",
    "complex": "best",
}


def route_call_with_complexity(
    logical: str, complexity: str, *extra_args: str, slim: str | None = None
) -> str:
    """Invocation form carrying the complexity, spelled the way the target expects.

    ``llm_code`` + ``moderate`` renders as ``llm_code(complexity='moderate')`` on a
    legacy tier and ``llm(task="code", tier="balanced")`` on the consolidated one.
    """
    call = resolve(logical, slim)
    if call.name == "llm":
        tier = _COMPLEXITY_TO_TIER.get((complexity or "").lower(), "balanced")
        return call.render(f'tier="{tier}"', *extra_args)
    return call.render(f"complexity='{complexity}'", *extra_args)


def localize(text: str, slim: str | None = None) -> str:
    """Rewrite every legacy tool name inside a text blob to its registered form.

    For templates that are written to disk or handed to a model whole — generated
    agent files, rules files, quick-start guides, host config snippets. Those are
    the highest-leverage place to get names right: a rules file that says
    ``llm_code`` teaches the model to make a failing call for the entire life of
    the file, long after the session that produced it.

    Two passes, and the order matters:

    1. Whole CALL forms first — ``llm_code(complexity="complex")`` becomes
       ``llm(task="code", tier="best")``. Doing the name pass first would leave
       ``llm(task="code")(complexity="complex")``: an uncallable double call,
       carrying an argument the door does not even accept. That is the exact
       failure ``scripts/lint_tool_surface.py`` exists to catch, and a naive
       name-only ``localize`` reintroduced it in the shipped rules files.
    2. Bare names, longest-first so no name is rewritten as a prefix of a longer one.
    """
    import re as _re

    def _call(m: "_re.Match[str]") -> str:
        legacy, args = m.group(1), m.group(2)
        call = resolve(legacy, slim)
        # No translation needed on this tier → return the ORIGINAL text verbatim.
        # Re-rendering would only change quote style, and that cosmetic diff would
        # make every install/refresh report spurious content drift.
        if call.name == legacy:
            return m.group(0)
        if call.name == "llm":
            # The door takes tier=, the legacy tools take complexity=. Carrying the
            # old spelling across would swap "no such tool" for "unexpected keyword".
            args = _re.sub(
                r"complexity\s*=\s*([\"'])(\w+)\1",
                lambda a: 'tier="%s"' % _COMPLEXITY_TO_TIER.get(a.group(2).lower(), "balanced"),
                args,
            )
        pinned = ", ".join(f'{k}="{v}"' for k, v in call.pinned)
        inner = ", ".join(p for p in (pinned, args.strip()) if p)
        return f"{call.name}({inner})"

    # Whole call expressions, including multi-argument ones. [^()]* keeps this to a
    # single un-nested arg list, which is all the docs and rules files contain.
    text = _re.sub(
        r"\b(" + "|".join(sorted(DEPRECATED_TOOLS, key=len, reverse=True)) + r")\(([^()]*)\)",
        _call,
        text,
    )

    for legacy in sorted(DEPRECATED_TOOLS, key=len, reverse=True):
        if legacy in text:
            text = text.replace(legacy, resolve(legacy, slim).display)
    return text


def call_parts(logical: str, slim: str | None = None) -> tuple[str, list[str]]:
    """``(registered_name, pinned_args)`` for hand-formatted multi-line calls.

    Callers that lay a call out over several lines cannot use :func:`route_call`
    (it renders a single line) and must NOT use :func:`route_tool` as the head —
    that already includes the pinned args, so appending "(" yields the uncallable
    ``llm(task="code")(…``. Take the head and args separately from here.
    """
    call = resolve(logical, slim)
    return call.name, [f'{k}="{v}"' for k, v in call.pinned]


#: Prefixes a tool coroutine's name begins with. Kept beside the scanner so a
#: new naming convention has one place to be declared.
_TOOL_NAME_PREFIXES = ("llm", "llm_router_")


def implemented_tools() -> frozenset[str]:
    """Tool functions that actually EXIST, read from llm_router/tools/ by AST.

    This is the ground truth :func:`unregistered` does not have. That function
    checks names against ``_TIERS``, which IS the tier constants — rename a tool
    inside ``CORE_TOOLS`` and the "registered" set contains the new name, so it
    reports clean. Self-consistency, not validation. A bogus canonical tool name
    passed the whole suite and ``doctor`` on the strength of it (audit Q3(c)).

    Parsed rather than imported: importing the tool modules drags in the server
    stack, and a check that only runs when the world is healthy is a poor check
    for the case where it is not. Returns an empty set if the directory cannot be
    read, and callers must treat empty as UNKNOWN rather than as "nothing is
    implemented" — see :func:`phantom_tools`.
    """
    import ast

    tools_dir = Path(__file__).resolve().parent / "tools"
    names: set[str] = set()
    try:
        paths = sorted(tools_dir.glob("*.py"))
    except OSError:
        return frozenset()
    for path in paths:
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name.startswith(_TOOL_NAME_PREFIXES):
                    names.add(node.name)
    return frozenset(names)


def phantom_tools(slim: str | None = None) -> list[str]:
    """Tier entries that name a tool nothing implements. Must always be empty.

    Returns ``[]`` when ground truth is unavailable rather than reporting every
    tool as phantom — an unreadable tools/ directory is not evidence that the
    surface is broken, and a check that screams on its own failure gets muted.
    """
    implemented = implemented_tools()
    if not implemented:
        return []
    offered = registered_tools(slim)
    if offered is None:
        offered = KNOWN_TOOLS
    deprecated = frozenset(DEPRECATED_TOOLS)
    return sorted(n for n in offered if n not in implemented and n not in deprecated)


def unregistered(names=None, slim: str | None = None) -> list[str]:
    """Return the names that resolve to a NON-registered tool under ``slim``.

    The whole point of the module: this must always be empty. Used by the CI
    guard test and by the server's startup self-check, so a future emitter that
    invents an unroutable name fails loudly instead of silently costing money.
    """
    reg = registered_tools(slim)
    if reg is None:
        return []
    bad: list[str] = []
    for name in sorted(names if names is not None else EMITTABLE_TOOLS):
        if resolve(name, slim).name not in reg:
            bad.append(name)
    return bad
