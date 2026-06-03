"""Routing profiles — maps (profile, task_type) to ordered model preferences.

This module defines the static routing tables that power the multi-provider
fallback chain. For every (RoutingProfile, TaskType) pair, there is an ordered
list of models to try. The router walks this list top-to-bottom, skipping
unhealthy providers, until one succeeds.

Three profile tiers exist:
  - **BUDGET**: cheapest models that still produce usable results. Prioritizes
    free/low-cost providers (Gemini Flash, Groq, DeepSeek).
  - **BALANCED**: quality/cost sweet spot. Uses mid-tier models from major
    providers (GPT-4o, Claude Sonnet, Gemini Pro).
  - **PREMIUM**: best available quality, cost secondary. Uses frontier models
    (o3, Claude Opus, Gemini Pro).

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
# deepseek-reasoner is $0.0014 — cheaper than Gemini Pro ($0.003) and outperforms
# it on every benchmark, so it belongs in the cheap tier for pressure reordering.
_CHEAP_MODELS: frozenset[str] = frozenset({
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.5-flash-lite",
    "groq/llama-3.3-70b-versatile",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
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
    "deepseek/deepseek-chat",
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
# ── Model-Profile Constraints ───────────────────────────────────────────────
# SAFEGUARD #3: Explicit data structures defining which models are allowed
# per profile. Used by _validate_chain_invariants() to catch policy violations.
#
# These constraints are the SOURCE OF TRUTH for policy enforcement:
# - BUDGET: Never include Opus or even Sonnet (use Haiku only as last resort)
# - BALANCED: Never include Opus (use Sonnet/Haiku as fallback only)
# - PREMIUM: Can include Opus, but it must be first (best quality)
MODELS_PER_PROFILE: dict[RoutingProfile, dict[str, list[str]]] = {
    RoutingProfile.BUDGET: {
        "forbidden": ["anthropic/claude-opus-4-6"],  # Opus forbidden in BUDGET
        "discouraged": [
            "anthropic/claude-sonnet-4-6",  # Sonnet discouraged (use only Haiku)
        ],
        "allowed_claude": ["anthropic/claude-haiku-4-5-20251001"],  # Haiku only as last resort
    },
    RoutingProfile.BALANCED: {
        "forbidden": ["anthropic/claude-opus-4-6"],  # Opus forbidden in BALANCED
        "discouraged": [],
        "allowed_claude": [
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-haiku-4-5-20251001",
        ],
    },
    RoutingProfile.PREMIUM: {
        "forbidden": [],  # No models forbidden in PREMIUM
        "discouraged": [],
        "allowed_claude": [
            "anthropic/claude-opus-4-6",  # Opus allowed, should be first
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-haiku-4-5-20251001",
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
    else:
        profile_for_check = profile

    constraints = MODELS_PER_PROFILE.get(profile_for_check)
    if not constraints:
        return  # No constraints defined, skip validation

    forbidden = constraints.get("forbidden", [])
    discouraged = constraints.get("discouraged", [])

    # SAFEGUARD #1: Invariant assertions — these MUST never happen
    for forbidden_model in forbidden:
        if forbidden_model in chain:
            error_msg = (
                f"POLICY VIOLATION: {forbidden_model} appears in {profile.name} profile chain. "
                f"Context: {context}. Chain: {chain}"
            )
            log.error(error_msg)  # SAFEGUARD #2: Log the violation
            raise AssertionError(error_msg)

    # SAFEGUARD #2: Logging on discouraged matches
    for discouraged_model in discouraged:
        if discouraged_model in chain:
            # Check if it's at the front (bad) vs. end (acceptable fallback)
            is_first = chain[0] == discouraged_model
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
    Complexity.DEEP_REASONING: RoutingProfile.PREMIUM,  # Extended thinking — same chain as PREMIUM
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
    # QUOTA_BALANCED uses BALANCED as base chain — reordering happens in router.py
    profile_for_lookup = RoutingProfile.BALANCED if profile == RoutingProfile.QUOTA_BALANCED else profile

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
