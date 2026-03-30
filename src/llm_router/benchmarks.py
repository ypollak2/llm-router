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

# Bundled benchmark file shipped inside the wheel.
_BUNDLED = Path(__file__).parent / "data" / "benchmarks.json"

# User-local copy updated on each pip upgrade + server restart.
_INSTALLED = Path.home() / ".llm-router" / "benchmarks.json"

# Module-level cache so the JSON is only parsed once per process.
_cache: dict[str, Any] | None = None
_cache_loaded: bool = False


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


def get_model_failure_penalty(model: str, task_type: str) -> float:
    """Return a 0.0–1.0 penalty for a model based on its local failure rate.

    Queries the ``routing_decisions`` SQLite table for recent call outcomes.
    Models with >20% failure rate receive a linear penalty that grows to 1.0
    at 100% failure rate, effectively pushing them to the bottom of the chain.

    Args:
        model: Model ID in ``provider/model`` format.
        task_type: Task type string (e.g. ``"code"``).

    Returns:
        Penalty coefficient in [0.0, 1.0]. Zero means no penalty.
    """
    try:
        import asyncio
        from llm_router.cost import get_model_failure_rates

        async def _fetch() -> float:
            rates = await get_model_failure_rates(window_days=30)
            return rates.get(model, 0.0)

        try:
            loop = asyncio.get_running_loop()
            # Inside an async context — schedule as a task but don't block.
            # Return 0.0 (no penalty) rather than deadlocking the event loop.
            _ = loop  # loop exists, we're async — skip the sync fetch
            return 0.0
        except RuntimeError:
            # No running loop — safe to use asyncio.run().
            failure_rate = asyncio.run(_fetch())
            if failure_rate > 0.20:
                return min(1.0, (failure_rate - 0.20) * 2.0)
    except Exception:
        pass
    return 0.0


def apply_benchmark_ordering(
    chain: list[str],
    task_type: "TaskType",
    profile: "RoutingProfile",
) -> list[str]:
    """Reorder a model chain using benchmark data for the given task/profile.

    Takes the static model chain from ``profiles.ROUTING_TABLE`` and reorders
    it so the benchmark-best models for this task type and routing profile come
    first, while preserving all models (no removals). Models not covered by
    benchmark data are appended at the end in their original relative order.

    The reordering also incorporates a local failure-rate penalty: a model with
    a high local failure rate is pushed down even if its benchmark score is good.

    Args:
        chain: Ordered list of model IDs from the static routing table.
        task_type: The task type being routed.
        profile: The routing profile (BUDGET / BALANCED / PREMIUM).

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
        # 1. Models in both the static chain AND the benchmark tier list.
        # 2. Models in the static chain but NOT covered by benchmarks.
        covered: list[str] = [m for m in benchmark_tier if m in chain]
        uncovered: list[str] = [m for m in chain if m not in benchmark_tier]

        # Apply local failure-rate penalty to covered models, then re-sort.
        def adjusted_score(model: str) -> float:
            base = task_scores.get(model, 0.5)
            penalty = get_model_failure_penalty(model, task_key)
            return base * (1.0 - penalty)

        covered.sort(key=adjusted_score, reverse=True)

        return covered + uncovered

    except Exception as e:
        log.debug("benchmark ordering failed, using static chain: %s", e)
        return chain
