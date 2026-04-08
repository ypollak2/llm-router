"""Benchmark-driven routing table reordering.

Loads weekly-updated benchmark data (from bundled ``data/benchmarks.json``,
copied to ``~/.llm-router/benchmarks.json`` on startup) and uses it to
reorder the static model chains from ``profiles.py`` so the best-performing
model for each task type is tried first.

Key design principles:
- **Offline-safe**: every public function returns a safe default on any failure.
  ``get_benchmark_data()`` returns ``None``, ``apply_benchmark_ordering()``
  returns the input chain unchanged, ``check_and_update_benchmarks()`` is a
  no-op. No exception ever propagates to the caller.
- **Zero runtime overhead**: benchmark data is loaded once and cached in a
  module-level variable; ``apply_benchmark_ordering()`` is a dict lookup, not
  a recomputation.
- **Internal feedback loop**: local failure rates from ``routing_decisions``
  penalize models that consistently fail, pushing them down the chain even if
  their benchmark score is high.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llm_router.types import RoutingProfile, TaskType

log = logging.getLogger("llm_router")

# Blended cost per 1K tokens (avg of input + output). Used by the quality-cost
# sort to prefer cheaper models when two options are within 5% quality of each other.
# Codex and Ollama are free ($0) — always prefer them over paid when quality allows.
_MODEL_COST_PER_1K: dict[str, float] = {
    # Anthropic
    "anthropic/claude-opus-4-6":           0.045,
    "anthropic/claude-sonnet-4-6":         0.009,
    "anthropic/claude-haiku-4-5-20251001": 0.00075,
    # OpenAI
    "openai/o3":                           0.025,
    "openai/gpt-4o":                       0.006,
    "openai/gpt-4o-mini":                  0.00038,
    # Google
    "gemini/gemini-2.5-pro":               0.003,
    "gemini/gemini-2.5-flash":             0.00019,
    "gemini/gemini-2.5-flash-lite":        0.000025,
    # DeepSeek
    "deepseek/deepseek-reasoner":          0.0014,
    "deepseek/deepseek-chat":              0.0007,
    # Groq
    "groq/llama-3.3-70b-versatile":        0.0007,
    # Perplexity
    "perplexity/sonar-pro":                0.010,
    "perplexity/sonar":                    0.003,
    # Mistral
    "mistral/mistral-large-latest":        0.004,
    "mistral/mistral-small-latest":        0.00020,
    # xAI / Cohere
    "xai/grok-3":                          0.009,
    "cohere/command-r-plus":               0.006,
    # Free models (Codex = OpenAI subscription, Ollama = local)
    "codex/gpt-5.4":                       0.0,
    "codex/o3":                            0.0,
    "codex/gpt-4o":                        0.0,
}

# Fallback cost for unknown models — use a mid-range estimate.
_DEFAULT_COST = 0.005

# Bundled benchmark file shipped inside the wheel.
_BUNDLED = Path(__file__).parent / "data" / "benchmarks.json"

# User-local copy updated on each pip upgrade + server restart.
_INSTALLED = Path.home() / ".llm-router" / "benchmarks.json"

# Module-level cache so the JSON is only parsed once per process.
_cache: dict[str, Any] | None = None
_cache_loaded: bool = False

# Cold-start latency defaults for models that have no routing history yet.
# Based on empirical observations: Codex CLI requires application launch time
# before the first call each session, leading to 60-90s P95 latency.
_COLD_START_LATENCY_MS: dict[str, float] = {
    "codex/gpt-5.4": 60_000.0,
    "codex/o3":      90_000.0,
    "codex/gpt-4o":  60_000.0,
}

# Latency stats cache: refreshed at most every 60 s to avoid repeated DB hits
# inside a single routing cycle where many models are evaluated in sequence.
_latency_cache: dict[str, dict] | None = None
_latency_cache_ts: float = 0.0
_LATENCY_CACHE_TTL: float = 60.0


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load and parse a JSON file, returning None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _benchmark_version(data: dict[str, Any] | None) -> int:
    """Extract the integer version from benchmark data, or 0 on failure."""
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("version", 0))
    except (TypeError, ValueError):
        return 0


def check_and_update_benchmarks() -> str | None:
    """Copy the bundled benchmarks file to ``~/.llm-router/`` if it is newer.

    Called at MCP server startup (``server.py``) so users receive updated
    routing data automatically after ``pip upgrade``, without reinstalling.

    Returns:
        A human-readable update message like
        ``"Updated benchmark data v1 → v3"``, or ``None`` if no update
        was needed or the bundled file is missing.
    """
    global _cache, _cache_loaded
    if not _BUNDLED.exists():
        return None
    try:
        bundled_data = _load_json(_BUNDLED)
        installed_data = _load_json(_INSTALLED)
        src_version = _benchmark_version(bundled_data)
        dst_version = _benchmark_version(installed_data)
        if src_version <= dst_version:
            return None
        _INSTALLED.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_BUNDLED, _INSTALLED)
        # Invalidate the module cache so the next call picks up the new file.
        _cache = None
        _cache_loaded = False
        return f"Updated benchmark data v{dst_version} → v{src_version}"
    except Exception as e:
        log.debug("benchmark update check failed: %s", e)
        return None


def get_benchmark_data() -> dict[str, Any] | None:
    """Return the cached benchmark data dict, loading it on first call.

    Load order: ``~/.llm-router/benchmarks.json`` (user-local, may be newer
    than bundled), falling back to ``data/benchmarks.json`` (bundled in wheel).

    Returns:
        The parsed benchmark dict, or ``None`` if neither file exists or
        can be parsed.
    """
    global _cache, _cache_loaded
    if _cache_loaded:
        return _cache
    _cache_loaded = True
    _cache = _load_json(_INSTALLED) or _load_json(_BUNDLED)
    return _cache


def get_model_failure_penalty(
    model: str,
    task_type: str,
    failure_rates: dict[str, float] | None = None,
) -> float:
    """Return a 0.0–1.0 penalty for a model based on its local failure rate.

    When ``failure_rates`` is provided (pre-fetched by the async routing layer),
    uses that dict directly — no DB access, no async/sync conflict.
    When not provided, falls back to a synchronous DB fetch (works only from
    non-async contexts; returns 0.0 otherwise to avoid event loop deadlock).

    Models with >20% failure rate receive a linear penalty that grows to 1.0
    at 100% failure rate, effectively pushing them to the bottom of the chain.

    Args:
        model: Model ID in ``provider/model`` format.
        task_type: Task type string (e.g. ``"code"``).
        failure_rates: Pre-fetched dict of ``{model: rate}``. Pass this from
            the async routing path to avoid the sync/async conflict.

    Returns:
        Penalty coefficient in [0.0, 1.0]. Zero means no penalty.
    """
    try:
        if failure_rates is not None:
            rate = failure_rates.get(model, 0.0)
        else:
            import asyncio
            from llm_router.cost import get_model_failure_rates

            async def _fetch() -> float:
                rates = await get_model_failure_rates(window_days=30)
                return rates.get(model, 0.0)

            try:
                asyncio.get_running_loop()
                # Inside async — skip to avoid deadlock; caller should pass failure_rates
                return 0.0
            except RuntimeError:
                rate = asyncio.run(_fetch())

        if rate > 0.20:
            return min(1.0, (rate - 0.20) * 2.0)
    except Exception:
        pass
    return 0.0


def get_model_latency_penalty(
    model: str,
    task_type: str,
    latency_stats: dict[str, dict] | None = None,
) -> float:
    """Return a 0.0–0.5 penalty for a model based on its observed P95 latency.

    Latency thresholds (P95 ms → penalty):
      < 5 000 ms  →  0.00  (fast cloud APIs, Gemini Flash)
      < 15 000 ms →  0.03  (normal cloud APIs, GPT-4o)
      < 60 000 ms →  0.10  (slower models, first-token latency)
      < 180 000 ms → 0.30  (Codex typical cold-start)
      ≥ 180 000 ms → 0.50  (Codex worst-case, effectively last resort)

    For models with no routing history, ``_COLD_START_LATENCY_MS`` provides
    pessimistic defaults so Codex is not unfairly promoted to first position
    before any data has been collected.

    When ``latency_stats`` is provided (pre-fetched by the async routing layer),
    uses that dict directly — no DB access, no async/sync conflict.
    When not provided, falls back to a synchronous DB fetch (works only from
    non-async contexts; returns 0.0 otherwise to avoid event loop deadlock).

    Args:
        model: Model ID in ``provider/model`` format.
        task_type: Task type string (unused today, reserved for per-task tuning).
        latency_stats: Pre-fetched dict of ``{model: {"p50": ms, "p95": ms, "count": n}}``.
            Pass this from the async routing path to avoid the sync/async conflict.

    Returns:
        Penalty coefficient in [0.0, 0.5]. Zero means no penalty.
    """
    import time

    global _latency_cache, _latency_cache_ts

    try:
        if latency_stats is not None:
            stats = latency_stats
        else:
            # Refresh cache if stale
            now = time.monotonic()
            if _latency_cache is None or (now - _latency_cache_ts) > _LATENCY_CACHE_TTL:
                import asyncio
                from llm_router.cost import get_model_latency_stats

                async def _fetch() -> dict[str, dict]:
                    return await get_model_latency_stats(window_days=7)

                try:
                    loop = asyncio.get_running_loop()
                    _ = loop  # running loop — skip sync fetch, use cached/default
                except RuntimeError:
                    # No running loop — safe to call asyncio.run()
                    _latency_cache = asyncio.run(_fetch())
                    _latency_cache_ts = now

            stats = _latency_cache or {}

        if model in stats:
            p95_ms = stats[model]["p95"]
        elif model in _COLD_START_LATENCY_MS:
            p95_ms = _COLD_START_LATENCY_MS[model]
        else:
            return 0.0

        if p95_ms < 5_000:
            return 0.00
        elif p95_ms < 15_000:
            return 0.03
        elif p95_ms < 60_000:
            return 0.10
        elif p95_ms < 180_000:
            return 0.30
        else:
            return 0.50

    except Exception:
        return 0.0


def get_model_acceptance_penalty(
    model: str,
    acceptance_scores: dict[str, float] | None = None,
) -> float:
    """Return a 0.0–0.4 penalty for models with below-average user acceptance.

    Models with no feedback data (None) receive no penalty — unknown is neutral.
    Acceptance thresholds:
      ≥ 70%  →  0.00  (good track record)
      ≥ 50%  →  0.20  (mixed quality — push down moderately)
      < 50%  →  0.40  (poor quality — push to back of chain)

    Args:
        model: Model ID in ``provider/model`` format.
        acceptance_scores: Pre-fetched dict of ``{model: rate}`` from
            ``cost.get_model_acceptance_scores()``.

    Returns:
        Penalty coefficient in [0.0, 0.4]. Zero means no penalty.
    """
    if not acceptance_scores:
        return 0.0
    rate = acceptance_scores.get(model)
    if rate is None:
        return 0.0  # no feedback yet — neutral
    if rate >= 0.70:
        return 0.00
    elif rate >= 0.50:
        return 0.20
    else:
        return 0.40


def apply_benchmark_ordering(
    chain: list[str],
    task_type: "TaskType",
    profile: "RoutingProfile",
    failure_rates: dict[str, float] | None = None,
    latency_stats: dict[str, dict] | None = None,
    acceptance_scores: dict[str, float] | None = None,
) -> list[str]:
    """Reorder a model chain using benchmark data for the given task/profile.

    Takes the static model chain from ``profiles.ROUTING_TABLE`` and reorders
    it so the benchmark-best models for this task type and routing profile come
    first, while preserving all models (no removals). Models not covered by
    benchmark data are appended at the end in their original relative order.

    The reordering incorporates three local feedback penalties:
    - **Failure-rate penalty**: models that frequently fail are pushed down.
    - **Latency penalty**: models with high P95 latency are pushed down.
    - **Acceptance penalty**: models rated poorly via ``llm_rate`` are pushed down.

    Args:
        chain: Ordered list of model IDs from the static routing table.
        task_type: The task type being routed.
        profile: The routing profile (BUDGET / BALANCED / PREMIUM).
        failure_rates: Pre-fetched dict of ``{model: rate}`` from cost module.
        latency_stats: Pre-fetched dict of ``{model: {"p50", "p95", "count"}}``.
        acceptance_scores: Pre-fetched dict of ``{model: acceptance_rate}``
            from ``cost.get_model_acceptance_scores()``. Models with a rate
            below 70% receive a penalty reducing their adjusted score.

    Returns:
        Reordered chain. Falls back to the input ``chain`` on any failure.
    """
    try:
        data = get_benchmark_data()
        if not data:
            return chain

        task_key = task_type.value  # e.g. "code"
        profile_key = profile.value  # e.g. "budget"

        # Look up the pre-sorted tier list for this (task, profile) pair.
        benchmark_tier: list[str] = (
            data.get("tiers", {})
            .get(task_key, {})
            .get(profile_key, [])
        )
        task_scores: dict[str, float] = data.get("task_scores", {}).get(task_key, {})

        if not benchmark_tier:
            return chain

        # Build two groups:
        # 1. Models in the chain that have quality scores in task_scores.
        # 2. Models in the chain with no score data (appended at end unchanged).
        scored: list[str] = [m for m in chain if m in task_scores]
        unscored: list[str] = [m for m in chain if m not in task_scores]

        # Apply local failure-rate, latency, and acceptance penalties to scored models.
        def adjusted_score(model: str) -> float:
            base = task_scores.get(model, 0.5)
            failure_pen = get_model_failure_penalty(model, task_key, failure_rates=failure_rates)
            latency_pen = get_model_latency_penalty(model, task_key, latency_stats=latency_stats)
            accept_pen = get_model_acceptance_penalty(model, acceptance_scores=acceptance_scores)
            return base * (1.0 - failure_pen) * (1.0 - latency_pen) * (1.0 - accept_pen)

        # Quality-cost sort: quality first, but within any 5% quality tier
        # sort by cost ascending (cheapest wins when models are near-equal).
        #
        # Algorithm:
        #   1. Sort by quality descending.
        #   2. Walk the sorted list, opening a new tier whenever the quality
        #      drop from the TIER LEADER exceeds 5% (relative). This avoids
        #      non-transitivity that arises from pairwise comparisons.
        #   3. Within each tier, sort by cost ascending (cheapest first).
        #
        # Example (BALANCED/CODE):
        #   DeepSeek:  quality=0.999 → tier 0 leader
        #   Haiku:     quality=0.821 → 18% below tier 0 leader → tier 1 leader
        #   Sonnet:    quality=0.803 → 2.2% below Haiku        → tier 1 (same tier!)
        #   GPT-4o:    quality=0.774 → 5.7% below Haiku leader → tier 2 leader
        #
        #   Tier 0: [DeepSeek] → only one → no cost comparison needed
        #   Tier 1: [Haiku $0.00075, Sonnet $0.009] → Haiku cheaper → Haiku first
        #   Tier 2: [GPT-4o, ...] → continues
        #
        # Result: DeepSeek → Haiku → Sonnet → GPT-4o → ...
        scores = {m: adjusted_score(m) for m in scored}

        # Step 1: sort by quality descending
        by_quality = sorted(scored, key=lambda m: scores.get(m, 0.0), reverse=True)

        # Step 2: assign quality tiers (5% drop from tier leader = new tier)
        tiers: list[list[str]] = []
        current_tier: list[str] = []
        tier_leader_q: float | None = None

        for model in by_quality:
            q = scores.get(model, 0.0)
            if tier_leader_q is None:
                current_tier = [model]
                tier_leader_q = q
            elif tier_leader_q > 0 and (tier_leader_q - q) / tier_leader_q > 0.05:
                tiers.append(current_tier)
                current_tier = [model]
                tier_leader_q = q
            else:
                current_tier.append(model)

        if current_tier:
            tiers.append(current_tier)

        # Step 3: within each tier, sort by cost ascending (cheapest first)
        reordered: list[str] = []
        for tier in tiers:
            tier.sort(key=lambda m: _MODEL_COST_PER_1K.get(m, _DEFAULT_COST))
            reordered.extend(tier)

        return reordered + unscored

    except Exception as e:
        log.debug("benchmark ordering failed, using static chain: %s", e)
        return chain
