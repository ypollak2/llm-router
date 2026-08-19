"""Routing profiles — maps (profile, task_type) to ordered model preferences.

This module defines the static routing tables that power the multi-provider
fallback chain. For every (RoutingProfile, TaskType) pair, there is an ordered
list of models to try. The router walks this list top-to-bottom, skipping
unhealthy providers, until one succeeds.

Four profile tiers exist:
  - **BUDGET**: cheapest models that still produce usable results. Prioritizes
    free/low-cost providers (Gemini Flash, Groq, DeepSeek).
  - **BALANCED**: quality/cost sweet spot. Uses mid-tier models from major
    providers (GPT-4o, Claude Sonnet, Gemini Pro).
  - **PREMIUM**: best available quality, cost secondary. Uses frontier models
    (o3, Claude Opus, Gemini Pro).
  - **REASONING**: dedicated extended-thinking chain for deep_reasoning
    complexity. Prioritises native reasoning models (DeepSeek-R1, o3) and
    activates extended-thinking flags on Claude Opus and Gemini 2.5 Pro.

Model IDs use LiteLLM's ``provider/model`` format for text models and the
same convention for media models (though media bypasses LiteLLM).
"""

from __future__ import annotations

from llm_router.logging import get_logger
from llm_router.types import Complexity, RoutingProfile, TaskType

log = get_logger("llm_router.profiles")

# Models treated as "cheap" under a Claude subscription — Haiku/Sonnet only.
# Opus ($15/1M) is NOT cheap, so it's NOT included. Only Haiku ($3/1M) and
# Sonnet ($3/1M) are reasonable fallbacks when quota is available.
_CLAUDE_CHEAP_MODELS: frozenset[str] = frozenset({
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5-20251001",
})

# Free external models (Codex uses OpenAI subscription, Gemini CLI uses Google One AI Pro).
_FREE_EXTERNAL_MODELS: frozenset[str] = frozenset({
    "codex/gpt-5.4",
    "codex/o3",
    "codex/gpt-4o",
    "gemini_cli/gemini-2.5-flash",
    "gemini_cli/gemini-2.0-flash",
    "gemini_cli/gemini-3-flash-preview",
})

# Cheap-but-not-free models (< $0.002/1K tokens blended).
# deepseek-v4-flash is $0.14/$0.28 per 1M — belongs in the cheap tier for
# pressure reordering. (v4-pro, the deepseek-reasoner successor, is mid-tier
# now at $1.74/$3.48, so it is intentionally NOT listed here.)
_CHEAP_MODELS: frozenset[str] = frozenset({
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.5-flash-lite",
    "groq/llama-3.3-70b-versatile",
    "deepseek/deepseek-v4-flash",   # was deepseek-chat/reasoner; aliases deprecate 2026-07-24
    "openai/gpt-4o-mini",
})

def _load_routing_table_from_policy() -> dict[tuple[RoutingProfile, TaskType], list[str]]:
    """Build the runtime ROUTING_TABLE by loading policies/standard.yaml.

    Plan 07 Phase 1b.2: standard.yaml is the canonical source of routing
    chains; this function transforms its nested chains structure (profile
    string -> task string -> ordered model list) into the dict-keyed-by-enum
    shape that the rest of the codebase already consumes.

    Raises:
        RuntimeError if standard.yaml is missing, malformed, or omits a
        (profile, task_type) combination that the runtime needs.
    """
    from llm_router.policy import PolicyManager

    try:
        policy = PolicyManager().load_policy("standard")
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(
            "Failed to load policies/standard.yaml — packaging error?"
        ) from exc

    if not policy.chains:
        raise RuntimeError(
            "policies/standard.yaml has no `chains` entries; ROUTING_TABLE would be empty."
        )

    table: dict[tuple[RoutingProfile, TaskType], list[str]] = {}
    for profile_key, tasks in policy.chains.items():
        try:
            profile_enum = RoutingProfile(profile_key)
        except ValueError as exc:
            raise RuntimeError(
                f"standard.yaml: unknown profile {profile_key!r}"
            ) from exc
        for task_key, chain in tasks.items():
            try:
                task_enum = TaskType(task_key)
            except ValueError as exc:
                raise RuntimeError(
                    f"standard.yaml: unknown task type {task_key!r} under profile {profile_key!r}"
                ) from exc
            table[(profile_enum, task_enum)] = list(chain)
    return table


# Master routing table: maps (profile, task_type) -> ordered model chain.
# Each entry is a list of model IDs in LiteLLM's "provider/model" format.
# The router tries models in order, falling back to the next on failure or
# rate-limiting.
#
# Source of truth: src/llm_router/policies/standard.yaml. This dict is
# hydrated at module-import time (Plan 07 Phase 1b.2). Drift between the
# YAML and the in-memory dict is impossible because there is only the YAML.
# tests/test_standard_policy_mirror.py is the canonical guardrail.

ROUTING_TABLE: dict[tuple[RoutingProfile, TaskType], list[str]] = _load_routing_table_from_policy()

# Historical literal removed — see git history (commit 2faaa08) for the
# previous hardcoded chains. To inspect or edit chains, modify
# src/llm_router/policies/standard.yaml.


# ── Reordering profiles ──────────────────────────────────────────────────────
#
# Two profiles have NO chains of their own in standard.yaml. They are
# *reorderings* of another profile's chain, applied at a later stage:
#
#   QUOTA_BALANCED      reordered by quota_balance.reorder_chain_by_providers()
#                       in router._build_and_filter_chain()
#   SUBSCRIPTION_LOCAL  reordered by
#                       subscription_local_routing.reorder_for_subscription_local()
#
# A ROUTING_TABLE lookup keyed on one of them therefore misses, and every
# caller has to know to substitute the base profile first. Three callers did
# not, and each failed differently:
#
#   get_model_chain()             SUBSCRIPTION_LOCAL -> the `["anthropic/
#                                 claude-sonnet-4-6"]` default: a ONE-model
#                                 chain with no fallback, consisting solely of
#                                 the paid seat. The exact inverse of what a
#                                 "one paid seat + free local bucket" profile
#                                 is for.
#   chain_builder._static_chain() both profiles -> [], breaking that module's
#                                 documented "Never empty (falls back to
#                                 static)" guarantee on the two paths that
#                                 exist to provide it (discovery empty, or the
#                                 dynamic build raised).
#   memory/profiles.py            both profiles -> the raw tool name returned
#                                 in place of a model id.
#
# Measured before the fix, task_type=CODE:
#
#     get_model_chain    balanced 6 · quota_balanced 6 · subscription_local 1
#     _static_chain      balanced 7 · quota_balanced 0 · subscription_local 0
#
# So: one table, one mapping, and every lookup goes through it. Adding a third
# reordering profile without an entry here fails
# tests/test_reordering_profiles_resolve.py, which enumerates the enum rather
# than naming profiles, so it covers profiles that do not exist yet.
_REORDERING_PROFILE_BASE: dict[RoutingProfile, RoutingProfile] = {
    RoutingProfile.QUOTA_BALANCED: RoutingProfile.BALANCED,
    RoutingProfile.SUBSCRIPTION_LOCAL: RoutingProfile.BALANCED,
}


def base_lookup_profile(profile: RoutingProfile) -> RoutingProfile:
    """The profile whose chain table ``profile`` should be looked up under.

    Identity for every profile that owns its chains. Use this before ANY
    ``ROUTING_TABLE`` / policy-chains lookup that takes a caller-supplied
    profile — see ``_REORDERING_PROFILE_BASE`` above for what goes wrong
    without it.

    This deliberately does not apply the reordering itself; it only resolves
    the base chain to reorder. The reordering stays where it is.
    """
    return _REORDERING_PROFILE_BASE.get(profile, profile)


# ── Classifier model preferences (cheapest/fastest first) ────────────────────
# These models are used exclusively by the complexity classifier, NOT for
# user-facing responses. They are ordered cheapest-first because classification
# is a low-stakes, structured-output task that doesn't need frontier quality.
#
# IMPORTANT: Non-thinking models are strongly preferred here. Thinking models
# (e.g. gemini-2.5-flash, deepseek-reasoner) spend most of their output budget
# on internal chain-of-thought reasoning, which often causes the actual JSON
# response to be truncated — triggering the _parse_truncated_json fallback.
CLASSIFIER_MODELS: list[str] = [
    # Haiku is the preferred classifier — fast, cheap, accurate structured output.
    # Skipped automatically when no ANTHROPIC_API_KEY is configured (subscription mode).
    "anthropic/claude-haiku-4-5-20251001",
    "gemini/gemini-2.5-flash-lite",  # non-thinking, fastest, cheapest external
    "groq/llama-3.3-70b-versatile",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-v4-flash",   # was deepseek-chat; alias deprecates 2026-07-24
    "mistral/mistral-small-latest",
]
# Ollama models (local, free) are prepended by router.py when ollama_base_url
# is configured, so they are tried before any cloud model.

# ── Complexity -> Profile mapping ─────────────────────────────────────────────
# Maps classifier output to routing profile. The rationale is straightforward:
# simple tasks don't need expensive models (budget), moderate tasks benefit
# from mid-tier quality (balanced), and complex tasks warrant frontier models
# (premium). This mapping is the bridge between the classifier and the
# routing table.
# ── Model family matching (version-agnostic) ─────────────────────────────────
# A "family" is a model id with its version/date suffix removed, e.g.
#   anthropic/claude-opus-4-8        -> anthropic/claude-opus
#   anthropic/claude-haiku-4-5-20251001 -> anthropic/claude-haiku
# Constraints below are written as FAMILY prefixes so a new version (Opus 4.9,
# Opus 5, …) is governed automatically without editing this file — it can never
# silently break a chain or slip past a guard.

# Single source of truth lives in model_aliases (no llm_router deps -> no import
# cycle). Re-exported here for backwards compatibility with existing callers.
from llm_router.model_aliases import (  # noqa: E402, F401  (re-exported for back-compat)
    LATEST_CLAUDE,
    model_family,
    model_matches,
    resolve_model_alias,
)


# ── Model-Profile Constraints ───────────────────────────────────────────────
# SAFEGUARD #3: Explicit data structures defining which models are allowed
# per profile. Used by _validate_chain_invariants() to catch policy violations.
# Entries are FAMILY prefixes (version-agnostic) — see model_matches().
#
# These constraints are the SOURCE OF TRUTH for policy enforcement:
# - BUDGET: Never include Opus or even Sonnet (use Haiku only as last resort)
# - BALANCED: Never include Opus (use Sonnet/Haiku as fallback only)
# - PREMIUM: Can include Opus, but it must be first (best quality)
MODELS_PER_PROFILE: dict[RoutingProfile, dict[str, list[str]]] = {
    RoutingProfile.BUDGET: {
        "forbidden": ["anthropic/claude-opus"],  # any Opus version forbidden in BUDGET
        "discouraged": [
            "anthropic/claude-sonnet",  # Sonnet discouraged (use only Haiku)
        ],
        "allowed_claude": ["anthropic/claude-haiku"],  # Haiku only as last resort
    },
    RoutingProfile.BALANCED: {
        "forbidden": ["anthropic/claude-opus"],  # any Opus version forbidden in BALANCED
        "discouraged": [],
        "allowed_claude": [
            "anthropic/claude-sonnet",
            "anthropic/claude-haiku",
        ],
    },
    RoutingProfile.PREMIUM: {
        "forbidden": [],  # No models forbidden in PREMIUM
        "discouraged": [],
        "allowed_claude": [
            "anthropic/claude-opus",  # any Opus version allowed, should be first
            "anthropic/claude-sonnet",
            "anthropic/claude-haiku",
        ],
    },
    RoutingProfile.REASONING: {
        "forbidden": [],  # Reasoning chain allows all models (reasoning specialists first)
        "discouraged": ["anthropic/claude-haiku"],  # Haiku lacks extended thinking
        "allowed_claude": [
            "anthropic/claude-opus",   # Primary Claude pick — extended thinking supported
            "anthropic/claude-sonnet",  # Fallback — extended thinking supported on Sonnet 4+
            "anthropic/claude-fable",  # Last-resort escalation — most sophisticated / most expensive
        ],
    },
}


# ── Profile-Model Invariant Validation ───────────────────────────────────────
# SAFEGUARD #1 & #2: Runtime assertions and logging on policy mismatch.
#
# These functions catch Opus in wrong profiles at runtime (invariant assertions)
# and log violations with immediate alerts (logging on policy mismatch).
def _validate_chain_invariants(
    chain: list[str],
    profile: RoutingProfile,
    context: str = "unknown",
) -> None:
    """Validate that a model chain follows profile-model constraints.

    This is SAFEGUARD #1 — profile-model invariant assertions that catch Opus
    in wrong profiles at runtime.

    Raises:
        AssertionError if Opus appears in BUDGET or BALANCED profiles.

    SAFEGUARD #2: Logs warnings on policy mismatches (constraints that don't
    raise but should be noted).

    Args:
        chain: The model chain to validate.
        profile: The routing profile it's used for.
        context: String describing where the chain came from (e.g.,
            "get_model_chain(BALANCED, CODE)", "reorder_for_pressure(BALANCED)").
    """
    if profile == RoutingProfile.QUOTA_BALANCED:
        # QUOTA_BALANCED uses BALANCED constraints as its base
        profile_for_check = RoutingProfile.BALANCED
    elif profile == RoutingProfile.SUBSCRIPTION_LOCAL:
        # SUBSCRIPTION_LOCAL has no dedicated constraints — skip validation
        return
    else:
        profile_for_check = profile

    constraints = MODELS_PER_PROFILE.get(profile_for_check)
    if not constraints:
        return  # No constraints defined, skip validation

    forbidden = constraints.get("forbidden", [])
    discouraged = constraints.get("discouraged", [])

    # SAFEGUARD #1: Invariant assertions — these MUST never happen.
    # Matching is FAMILY-AWARE (see model_matches): a pattern like
    # "anthropic/claude-opus" matches "anthropic/claude-opus-4-8", "...-5", etc.
    # so a new model version never silently slips past a guard or breaks a chain.
    for forbidden_model in forbidden:
        hit = next((m for m in chain if model_matches(m, forbidden_model)), None)
        if hit:
            error_msg = (
                f"POLICY VIOLATION: {hit} (matches forbidden family {forbidden_model!r}) "
                f"appears in {profile.name} profile chain. Context: {context}. Chain: {chain}"
            )
            log.error(error_msg)  # SAFEGUARD #2: Log the violation
            raise AssertionError(error_msg)

    # SAFEGUARD #2: Logging on discouraged matches
    for discouraged_model in discouraged:
        match = next((m for m in chain if model_matches(m, discouraged_model)), None)
        if match:
            # Check if it's at the front (bad) vs. end (acceptable fallback)
            is_first = model_matches(chain[0], discouraged_model)
            if is_first:
                log.warning(
                    "POLICY MISMATCH: %s appears first in %s chain (should be fallback). "
                    "Context: %s. Chain: %s",
                    discouraged_model,
                    profile.name,
                    context,
                    chain,
                )


COMPLEXITY_TO_PROFILE: dict[Complexity, RoutingProfile] = {
    Complexity.SIMPLE: RoutingProfile.BUDGET,
    Complexity.MODERATE: RoutingProfile.BALANCED,
    Complexity.COMPLEX: RoutingProfile.PREMIUM,
    Complexity.DEEP_REASONING: RoutingProfile.REASONING,  # Dedicated reasoning chain (R1/o3/thinking)
}


def reorder_for_pressure(
    chain: list[str],
    pressure: float,
    profile: "RoutingProfile",
    is_subscription_mode: bool = False,
) -> list[str]:
    """Reorder the model chain based on Claude subscription pressure.

    Called for BALANCED and PREMIUM profiles only — BUDGET is excluded because
    Ollama (injected by the router) already handles the free-first rule for
    simple tasks.

    Strategy:
    - **Below 85%**: Claude models move to the front — they're effectively
      free under a Pro/Max subscription. EXCEPTION: In is_subscription_mode=True,
      we leave the chain in its natural order (Ollama/Codex first) to preserve
      quota even when pressure is low.
    - **85–98%**: Claude moves to the end; free models (Codex) first, then
      cheap, then paid externals. Claude stays as a last-resort fallback.
    - **≥ 99% (hard cap)**: Claude is removed entirely from the chain to
      guarantee the weekly/session limit is never crossed.

    RESEARCH chains are excluded (caller's responsibility) because Perplexity
    must stay first regardless of quota state.

    Args:
        chain: Ordered list of model IDs from the routing table.
        pressure: Current Claude ``highest_pressure`` (raw max of session/weekly,
            0.0–1.0). Use the raw value, not ``effective_pressure``, so the
            99% hard cap is enforced regardless of imminent resets.
        profile: Routing profile — BUDGET is a no-op (pass-through).
        is_subscription_mode: If True, do not prepend Claude models at low pressure.

    Returns:
        Reordered list, possibly with Claude models removed at ≥ 99%.
    """
    claude_cheap_models = [m for m in chain if m in _CLAUDE_CHEAP_MODELS]
    other_models = [m for m in chain if m not in _CLAUDE_CHEAP_MODELS]

    if pressure >= 0.99:
        # Hard cap: remove ALL Claude models (including Opus) to protect quota.
        # Return only non-Claude models: Codex (free) → cheap → paid.
        non_claude_models = [m for m in chain if m not in _CLAUDE_CHEAP_MODELS and not m.startswith("anthropic/")]
        try:
            from llm_router.codex_agent import is_codex_available
            codex_available = is_codex_available()
        except Exception:
            codex_available = False

        def _hard_cap_priority(m: str) -> int:
            if m in _FREE_EXTERNAL_MODELS and codex_available:
                return 0
            if m in _CHEAP_MODELS:
                return 1
            return 2

        non_claude_models.sort(key=_hard_cap_priority)
        return non_claude_models

    if pressure < 0.85:
        # If in subscription mode, we DON'T want to push Claude to the front.
        # we want to save the quota for later. Leave the chain in its natural
        # order (usually favors Ollama/Codex/External).
        if is_subscription_mode:
            return chain

        # Quota available: cheap Claude models (Haiku/Sonnet) first, then external, then expensive
        return claude_cheap_models + other_models

    # 85–98%: quota tightening — externals first, Claude as last resort
    try:
        from llm_router.codex_agent import is_codex_available
        codex_available = is_codex_available()
    except Exception:
        codex_available = False

    def _priority(m: str) -> int:
        if m in _FREE_EXTERNAL_MODELS and codex_available:
            return 0   # Codex: free via OpenAI subscription
        if m in _CHEAP_MODELS:
            return 1   # Gemini Flash, Groq, DeepSeek, etc.
        return 2       # paid: GPT-4o, Gemini Pro, o3, etc.

    other_models.sort(key=_priority)
    result = other_models + claude_cheap_models

    # SAFEGUARD #1 & #2: Validate reordered chain against constraints
    try:
        _validate_chain_invariants(
            result, profile, context=f"reorder_for_pressure({profile.name}, pressure={pressure:.2f})"
        )
    except AssertionError:
        raise  # Policy violations are critical

    return result


def complexity_to_profile(complexity: Complexity) -> RoutingProfile:
    """Map a complexity level to the appropriate routing profile.

    Args:
        complexity: The classified complexity tier.

    Returns:
        The routing profile that best matches the complexity level.
    """
    return COMPLEXITY_TO_PROFILE[complexity]


def get_model_chain(
    profile: RoutingProfile,
    task_type: TaskType,
    failure_rates: dict[str, float] | None = None,
    latency_stats: "dict[str, dict] | None" = None,
    acceptance_scores: "dict[str, float] | None" = None,
    is_subscription_mode: bool = False,
) -> list[str]:
    """Get the ordered model preference chain for a profile + task type.

    Falls back to ``["anthropic/claude-sonnet-4-6"]`` if no entry exists.

    Applies two dynamic reorderings in sequence:
    1. Benchmark ordering — surface models with better benchmark scores,
       incorporating failure-rate and latency penalties when pre-fetched dicts
       are provided (avoids the sync/async conflict in penalty functions).
    2. Pressure reordering — when Claude quota is ≥ 85%, demote Claude
       models and promote free/cheap alternatives (see ``reorder_for_pressure``).

    RESEARCH chains use web-grounded alternatives (Claude/Gemini/OpenAI) for
    research tasks since web search is required.

    QUOTA_BALANCED uses BALANCED as its base chain; the final reordering is
    applied in _build_and_filter_chain() by quota_balance.reorder_chain_by_providers().

    Args:
        profile: The routing profile (budget/balanced/premium/quota_balanced).
        task_type: The task type.
        failure_rates: Pre-fetched dict of ``{model: failure_rate}`` from
            ``cost.get_model_failure_rates()``. Passed into benchmark ordering
            to enable penalty scoring without a sync DB call.
        latency_stats: Pre-fetched dict of ``{model: {"p50", "p95", "count"}}``
            from ``cost.get_model_latency_stats()``. Same purpose.
        acceptance_scores: Pre-fetched dict of ``{model: acceptance_rate}``
            from ``cost.get_model_acceptance_scores()``. Models with low user
            acceptance are penalised in benchmark ordering.
        is_subscription_mode: If True, do not prepend Claude models at low pressure.

    Returns:
        Ordered list of model IDs to try, best-fit first.
    """
    # Reordering profiles (QUOTA_BALANCED, SUBSCRIPTION_LOCAL) have no chains
    # of their own; resolve to the base they reorder. The reordering itself
    # still happens downstream — router.py for QUOTA_BALANCED,
    # chain_builder.build_chain for SUBSCRIPTION_LOCAL.
    profile_for_lookup = base_lookup_profile(profile)

    # Plan 06 Step 1 — consult the active policy's chains first so non-standard
    # policies (cost_aggressive, user-defined custom) actually take effect at the routing
    # layer. ROUTING_TABLE remains the policy-of-last-resort and matches
    # standard.yaml byte-for-byte, so the standard case is unchanged.
    static_chain: list[str] | None = None
    try:
        from llm_router.policy import get_active_policy
        active = get_active_policy()
        chains = getattr(active, "chains", None) or {}
        profile_chains = chains.get(profile_for_lookup.value, {})
        active_chain = profile_chains.get(task_type.value)
        if active_chain:
            static_chain = list(active_chain)
    except Exception:
        # Defensive — never let a policy mishap break routing. Fall through
        # to ROUTING_TABLE which is always valid.
        static_chain = None

    if static_chain is None:
        static_chain = ROUTING_TABLE.get(
            (profile_for_lookup, task_type), ["anthropic/claude-sonnet-4-6"],
        )

    # Media tasks: no benchmark data, no pressure reordering — use static order.
    if task_type in {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}:
        return static_chain

    try:
        from llm_router.claude_usage import get_claude_pressure
        pressure = get_claude_pressure()
    except Exception:
        pressure = 0.0

    # Research: Apply standard reordering (no special Perplexity handling)
    if task_type == TaskType.RESEARCH:
        # Use standard pressure reordering for research tasks
        try:
            chain = reorder_for_pressure(static_chain, pressure, profile, is_subscription_mode)
        except Exception as _e:
            log.warning("Pressure reordering failed for RESEARCH — using static order: %s", _e)
            chain = static_chain
        return chain

    # BUDGET: skip benchmark reordering — static chain already ordered correctly
    # (Haiku first for CODE, cheap-first for others). Ollama is prepended by the
    # router when configured; when it's not, Haiku must lead for CODE tasks.
    chain = static_chain
    if profile != RoutingProfile.BUDGET:
        try:
            from llm_router.benchmarks import apply_benchmark_ordering
            chain = apply_benchmark_ordering(
                chain, task_type, profile,
                failure_rates=failure_rates,
                latency_stats=latency_stats,
                acceptance_scores=acceptance_scores,
            )
        except Exception as _e:
            log.warning("Benchmark ordering failed — using static chain: %s", _e)

    try:
        chain = reorder_for_pressure(chain, pressure, profile, is_subscription_mode)
    except Exception as _e:
        log.warning("Pressure reordering failed — using static chain order: %s", _e)

    # SAFEGUARD #1 & #2: Validate chain against profile-model constraints
    # This catches Opus in BALANCED/BUDGET at runtime with an AssertionError
    try:
        _validate_chain_invariants(
            chain, profile, context=f"get_model_chain({profile.name}, {task_type.name})"
        )
    except AssertionError:
        raise  # Policy violations are critical — let them propagate

    # Filter Ollama entries to only models actually installed — prevents
    # 50-second LiteLLM hangs when the chain names a model that isn't present.
    try:
        from llm_router.discover import filter_ollama_by_installed
        chain = filter_ollama_by_installed(chain)
    except Exception:
        pass  # Never let filter failures break routing

    # Warm-path: if Ollama responded successfully within the last 60 seconds,
    # ensure it leads the chain for simple/budget tasks (skip classifier overhead).
    # Only applies when an Ollama model is already in the chain.
    try:
        from llm_router.discover import is_ollama_warm
        if (
            is_ollama_warm()
            and profile == RoutingProfile.BUDGET
            and task_type not in {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}
        ):
            ollama_models = [m for m in chain if m.startswith("ollama/")]
            rest = [m for m in chain if not m.startswith("ollama/")]
            if ollama_models:
                chain = ollama_models + rest
    except Exception:
        pass  # Never let warm-path failures break routing

    return chain


def provider_from_model(model: str) -> str:
    """Extract the provider name from a ``provider/model`` string.

    Args:
        model: Model identifier (e.g. ``"openai/gpt-4o"``).

    Returns:
        Provider name (e.g. ``"openai"``), or ``"unknown"`` if the string
        has no ``/`` separator.
    """
    return model.split("/")[0] if "/" in model else "unknown"
