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
import threading
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


# ── Local Model Quality Registry ─────────────────────────────────────────────
# Static quality estimates for local model families that never appear on CI
# benchmarks (ollama/*, lm_studio/*, vllm/*). Keyed by benchmark_alias defined
# in discover.py's _OLLAMA_MODEL_REGISTRY.
#
# Scores are conservative estimates calibrated from community reports and
# Aider/EvalPlus leaderboards for quantized local variants. Local models are
# penalised ~15–25% vs the cloud models they are based on (quantization loss).
#
# Format: {benchmark_alias: {task_type: quality_score [0.0–1.0]}}
_LOCAL_QUALITY_SCORES: dict[str, dict[str, float]] = {
    "qwen3-coder":    {"code": 0.76, "analyze": 0.72, "query": 0.65, "generate": 0.62, "research": 0.55},
    "qwen3":          {"code": 0.70, "analyze": 0.68, "query": 0.66, "generate": 0.64, "research": 0.52},
    "qwen2.5-coder":  {"code": 0.72, "analyze": 0.68, "query": 0.60, "generate": 0.58, "research": 0.48},
    "qwen2.5":        {"code": 0.65, "analyze": 0.64, "query": 0.62, "generate": 0.60, "research": 0.50},
    "codestral":      {"code": 0.73, "analyze": 0.65, "query": 0.55, "generate": 0.58, "research": 0.45},
    "deepseek-coder": {"code": 0.74, "analyze": 0.68, "query": 0.58, "generate": 0.56, "research": 0.46},
    "deepseek-v3":    {"code": 0.70, "analyze": 0.70, "query": 0.67, "generate": 0.65, "research": 0.54},
    "llama-3":        {"code": 0.62, "analyze": 0.63, "query": 0.64, "generate": 0.62, "research": 0.52},
    "llama":          {"code": 0.55, "analyze": 0.57, "query": 0.58, "generate": 0.56, "research": 0.46},
    "gemma":          {"code": 0.60, "analyze": 0.63, "query": 0.62, "generate": 0.59, "research": 0.48},
    "mistral":        {"code": 0.60, "analyze": 0.60, "query": 0.59, "generate": 0.60, "research": 0.50},
    "phi-4":          {"code": 0.62, "analyze": 0.61, "query": 0.60, "generate": 0.59, "research": 0.48},
    "phi":            {"code": 0.50, "analyze": 0.50, "query": 0.50, "generate": 0.50, "research": 0.40},
    "granite":        {"code": 0.65, "analyze": 0.62, "query": 0.58, "generate": 0.55, "research": 0.45},
    "command-r":      {"code": 0.55, "analyze": 0.60, "query": 0.62, "generate": 0.65, "research": 0.58},
}

# Default score for models not in any registry.
_DEFAULT_QUALITY = 0.50

# Background refresh state: only one refresh runs at a time.
_refresh_lock = threading.Lock()
_refresh_in_progress = False


def _resolve_local_alias(model_id: str) -> str | None:
    """Resolve a local model ID to a benchmark alias for quality lookup.

    Strips the provider prefix (``ollama/``, ``lm_studio/``) and matches the
    model name against the alias table used in ``discover.py``.  This allows
    ``ollama/qwen3:32b`` to resolve to the ``qwen3`` alias and look up quality
    scores from ``_LOCAL_QUALITY_SCORES``.

    Args:
        model_id: Full model ID, e.g. ``"ollama/qwen3:32b"``.

    Returns:
        Alias string (e.g. ``"qwen3"``) or ``None`` if not matched.
    """
    _LOCAL_PREFIXES = ("ollama/", "lm_studio/", "vllm/", "llamacpp/")
    base = model_id
    for prefix in _LOCAL_PREFIXES:
        if model_id.startswith(prefix):
            base = model_id[len(prefix):]
            break
    else:
        return None  # not a local model

    # Strip :<tag> suffix (e.g. "qwen3:32b" → "qwen3")
    name_lower = base.split(":")[0].lower()

    # Import the registry lazily to avoid circular imports at module load.
    # Ordered longest-prefix-first so "qwen3-coder" matches before "qwen3".
    _ALIAS_PREFIXES: list[tuple[str, str]] = [
        ("qwen3-coder",    "qwen3-coder"),
        ("qwen3",          "qwen3"),
        ("qwen2.5-coder",  "qwen2.5-coder"),
        ("qwen2.5",        "qwen2.5"),
        ("codestral",      "codestral"),
        ("deepseek-coder", "deepseek-coder"),
        ("deepseek",       "deepseek-v3"),
        ("llama3",         "llama-3"),
        ("llama",          "llama"),
        ("gemma4",         "gemma"),
        ("gemma",          "gemma"),
        ("mistral",        "mistral"),
        ("phi4",           "phi-4"),
        ("phi",            "phi"),
        ("granite",        "granite"),
        ("command",        "command-r"),
    ]
    for prefix, alias in _ALIAS_PREFIXES:
        if name_lower.startswith(prefix):
            return alias
    return None


def get_quality_score(model_id: str, task_type: str) -> float:
    """Return the quality score [0.0–1.0] for *model_id* on *task_type*.

    Lookup order:
      1. **Benchmark data** (``~/.llm-router/benchmarks.json``) — API models
         that appear on Arena Hard / Aider / HuggingFace leaderboards.
      2. **Local alias registry** (``_LOCAL_QUALITY_SCORES``) — Ollama / local
         models resolved via ``_resolve_local_alias()``.
      3. **Default** — 0.5 (neutral; model is treated as average quality).

    All failures return 0.5 silently (same offline-safe contract as the rest
    of this module).

    Args:
        model_id: Full model ID, e.g. ``"ollama/qwen3:32b"`` or
            ``"openai/gpt-4o"``.
        task_type: Task type string (``"code"``, ``"analyze"``, ``"query"``,
            ``"generate"``, ``"research"``).

    Returns:
        Quality score in [0.0, 1.0]. Higher is better.
    """
    try:
        # 1. Try benchmark data (API models)
        data = get_benchmark_data()
        if data:
            task_scores: dict[str, float] = data.get("task_scores", {}).get(task_type, {})
            if model_id in task_scores:
                return float(task_scores[model_id])

        # 2. Try local alias registry (Ollama / local providers)
        alias = _resolve_local_alias(model_id)
        if alias and alias in _LOCAL_QUALITY_SCORES:
            return float(_LOCAL_QUALITY_SCORES[alias].get(task_type, _DEFAULT_QUALITY))

    except Exception:
        pass

    return _DEFAULT_QUALITY


def maybe_refresh_benchmarks_background(ttl_days: int = 7) -> bool:
    """Trigger a background benchmark refresh if the local file is stale.

    Checks whether ``~/.llm-router/benchmarks.json`` was last fetched more
    than *ttl_days* days ago.  If stale (or missing), launches a background
    thread that re-fetches all sources and writes a new file.

    Designed to be called once per session start (cheap — just a file-mtime
    check plus a thread spawn).  The refresh itself is best-effort: any failure
    is logged at DEBUG level and silently ignored so routing is never blocked.

    Args:
        ttl_days: Days before the benchmark file is considered stale.
            Reads ``llm_router_benchmark_ttl_days`` from config when available.

    Returns:
        ``True`` if a background refresh was started, ``False`` otherwise.
    """
    global _refresh_in_progress

    try:
        from llm_router.config import get_config
        ttl_days = get_config().llm_router_benchmark_ttl_days
    except Exception:
        pass

    # Already refreshing in this process — skip.
    if _refresh_in_progress:
        return False

    # Check staleness.
    stale = True
    if _INSTALLED.exists():
        try:
            data = _load_json(_INSTALLED)
            generated_at_str = (data or {}).get("generated_at", "")
            if generated_at_str:
                from datetime import datetime, timezone
                generated_at = datetime.fromisoformat(generated_at_str)
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - generated_at).days
                stale = age_days >= ttl_days
        except Exception:
            stale = True  # parse failed → refresh

    if not stale:
        return False

    # Acquire lock and launch refresh thread. Capture the exact lock instance
    # we acquired so tests or hot reloads swapping the module global can't
    # cause the worker to release a different, unlocked lock later on.
    refresh_lock = _refresh_lock
    if not refresh_lock.acquire(blocking=False):
        return False  # another thread just started

    _refresh_in_progress = True

    def _refresh_worker() -> None:
        global _refresh_in_progress, _cache, _cache_loaded
        try:
            from llm_router.benchmark_fetcher import generate_benchmarks_json
            generate_benchmarks_json(output_path=_INSTALLED)
            # Invalidate cache so next get_benchmark_data() picks up the new file.
            _cache = None
            _cache_loaded = False
            log.info("Background benchmark refresh completed → %s", _INSTALLED)
        except Exception as e:
            log.debug("Background benchmark refresh failed: %s", e)
        finally:
            _refresh_in_progress = False
            if refresh_lock.locked():
                refresh_lock.release()

    thread = threading.Thread(target=_refresh_worker, name="benchmark-refresh", daemon=True)
    thread.start()
    log.debug("Background benchmark refresh started (benchmarks stale by ≥%d days)", ttl_days)
    return True


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
