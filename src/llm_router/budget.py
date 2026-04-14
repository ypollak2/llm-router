"""Budget Oracle — real-time budget pressure for every provider type.

Normalizes provider budget state into a single ``BudgetState`` value object
with a ``pressure`` field (0.0 = fully available, 1.0 = exhausted).

Pressure sources by provider type:

  Local (Ollama, vLLM, LM Studio)
      Always 0.0 — zero monetary cost, no quota.

  Claude subscription (anthropic)
      Reads ``~/.llm-router/usage.json`` (written by the session-start hook).
      Pressure = max(session_pct, weekly_pct) / 100.

  Generic API-key providers (openai, gemini, groq, deepseek, …)
      Reads per-provider monthly spend from the SQLite usage DB (cost.py).
      Pressure = spend / cap  (0.0 when no cap configured).

All results are cached for ``_CACHE_TTL`` seconds to avoid hitting the DB or
filesystem on every routing call.  Failures are silent — a missing DB or stale
usage.json returns pressure=0.0 (optimistic; keeps routing working).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from llm_router.config import get_config
from llm_router.types import BudgetState, LOCAL_PROVIDERS

# ── Cache ─────────────────────────────────────────────────────────────────────
# Per-provider cache: {provider: (BudgetState, cached_at)}
_cache: dict[str, tuple[BudgetState, float]] = {}
_CACHE_TTL = 60.0  # seconds

_USAGE_JSON = Path.home() / ".llm-router" / "usage.json"
_ROUTER_DIR = Path.home() / ".llm-router"


# ── Public API ────────────────────────────────────────────────────────────────


async def get_budget_state(provider: str) -> BudgetState:
    """Return the current budget state for *provider*.

    Results are cached for 60 seconds.  All failures return a neutral
    ``BudgetState`` with ``pressure=0.0`` (optimistic fallback).

    Args:
        provider: Provider name (e.g. ``"anthropic"``, ``"groq"``, ``"ollama"``).

    Returns:
        :class:`~llm_router.types.BudgetState` for the provider.
    """
    # Cache hit
    if provider in _cache:
        state, cached_at = _cache[provider]
        if time.monotonic() - cached_at < _CACHE_TTL:
            return state

    state = await _compute_budget_state(provider)
    _cache[provider] = (state, time.monotonic())
    return state


async def get_all_budget_states() -> dict[str, BudgetState]:
    """Return budget states for all configured providers in parallel.

    Returns:
        Dict mapping provider name → :class:`~llm_router.types.BudgetState`.
    """
    cfg = get_config()
    providers = list(cfg.available_providers) + list(LOCAL_PROVIDERS)
    results = await asyncio.gather(
        *[get_budget_state(p) for p in providers],
        return_exceptions=True,
    )
    return {
        p: r if isinstance(r, BudgetState) else _neutral(p)
        for p, r in zip(providers, results)
    }


def invalidate_cache(provider: str | None = None) -> None:
    """Invalidate the budget cache for one provider or all providers.

    Args:
        provider: Provider to invalidate. Pass ``None`` to clear all.
    """
    if provider is None:
        _cache.clear()
    else:
        _cache.pop(provider, None)


# ── Internal ──────────────────────────────────────────────────────────────────


def _neutral(provider: str) -> BudgetState:
    """Return a zero-pressure (fully available) state for *provider*."""
    return BudgetState(provider=provider, pressure=0.0)


async def _compute_budget_state(provider: str) -> BudgetState:
    """Compute fresh budget state for *provider* (no cache)."""
    # Local providers are always free — no I/O needed.
    if provider in LOCAL_PROVIDERS:
        return _neutral(provider)

    # Claude subscription — read from usage.json snapshot
    if provider == "anthropic":
        return _claude_subscription_state()

    # Generic API-key providers — check per-provider spend vs. configured cap
    return await _api_provider_state(provider)


def _claude_subscription_state() -> BudgetState:
    """Compute pressure from the cached Claude quota snapshot.

    Uses all three quota dimensions — session (5h window), weekly (all models),
    and weekly Sonnet — so pressure reflects whichever limit is closest to
    exhaustion. The pre-computed ``highest_pressure`` field is used when present
    (written by the session-start hook); individual fields are the fallback.
    """
    try:
        data = json.loads(_USAGE_JSON.read_text())
        # highest_pressure is the authoritative field (pre-computed by the hook)
        if "highest_pressure" in data:
            pressure = min(float(data["highest_pressure"]), 1.0)
        else:
            # Fallback: compute from individual quota dimensions
            session_pct = float(data.get("session_pct", 0.0)) / 100.0
            weekly_pct = float(data.get("weekly_pct", 0.0)) / 100.0
            sonnet_pct = float(data.get("sonnet_pct", 0.0)) / 100.0
            pressure = min(max(session_pct, weekly_pct, sonnet_pct), 1.0)
        return BudgetState(
            provider="anthropic",
            pressure=pressure,
            quota_pct=pressure,
        )
    except (OSError, json.JSONDecodeError, ValueError, KeyError):
        return _neutral("anthropic")


async def _api_provider_state(provider: str) -> BudgetState:
    """Compute pressure from monthly spend vs. per-provider cap.

    Spend sources:
      1. Local SQLite usage DB (always tracked)
      2. Helicone pull (when LLM_ROUTER_HELICONE_PULL=true)
      3. LiteLLM Proxy DB (when LLM_ROUTER_LITELLM_BUDGET_DB is set)

    Aggregation strategy is controlled by ``llm_router_spend_aggregation``:
      - ``"max"`` (default): conservative dedup when multiple trackers likely
        observe the same traffic.
      - ``"sum"``: additive when trackers represent independent traffic channels.
    """
    cfg = get_config()
    cap = _get_cap(provider, cfg)

    # Gather spend from all configured sources concurrently
    spend_sources = [_get_provider_monthly_spend(provider)]

    try:
        from llm_router.integrations.helicone import get_helicone_spend
        spend_sources.append(_extract_provider(get_helicone_spend(), provider))
    except Exception:
        pass

    try:
        from llm_router.integrations.litellm_budget import (
            get_litellm_spend, is_litellm_budget_enabled,
        )
        if is_litellm_budget_enabled():
            spend_sources.append(_extract_provider(get_litellm_spend(provider), provider))
    except Exception:
        pass

    results = await asyncio.gather(*spend_sources, return_exceptions=True)
    spend_values = [
        float(r) if isinstance(r, (int, float)) else 0.0
        for r in results
    ]
    if cfg.llm_router_spend_aggregation == "sum":
        spend = sum(spend_values)
    else:
        spend = max(spend_values, default=0.0)

    if cap <= 0.0:
        # No cap configured — provider is available, track spend only.
        return BudgetState(provider=provider, pressure=0.0, spend_usd=spend, cap_usd=0.0)

    pressure = min(spend / cap, 1.0)
    return BudgetState(
        provider=provider,
        pressure=pressure,
        spend_usd=spend,
        cap_usd=cap,
    )


async def _extract_provider(coro, provider: str) -> float:
    """Await a coroutine returning dict[str, float] and extract *provider*'s value."""
    try:
        result = await coro
        return float(result.get(provider, 0.0))
    except Exception:
        return 0.0


def _get_cap(provider: str, cfg) -> float:
    """Return the configured monthly budget cap for *provider* in USD.

    Priority: budget_store (set via CLI/dashboard) > env-var config fields.
    Returns 0.0 when no cap is set (unlimited).
    """
    from llm_router.budget_store import get_cap as _store_cap
    stored = _store_cap(provider)
    if stored > 0.0:
        return stored

    cap_map = {
        "openai": cfg.llm_router_budget_openai,
        "gemini": cfg.llm_router_budget_gemini,
        "groq": cfg.llm_router_budget_groq,
        "deepseek": cfg.llm_router_budget_deepseek,
        "together": cfg.llm_router_budget_together,
        "perplexity": cfg.llm_router_budget_perplexity,
        "mistral": cfg.llm_router_budget_mistral,
    }
    return cap_map.get(provider, 0.0)


async def _get_provider_monthly_spend(provider: str) -> float:
    """Query the SQLite usage DB for *provider*'s spend this calendar month.

    Returns 0.0 if the DB doesn't exist or the query fails.
    """
    try:
        import aiosqlite
        db_path = get_config().llm_router_db_path
        if not Path(db_path).exists():
            return 0.0
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
                "WHERE provider = ? AND timestamp >= datetime('now', 'start of month')",
                (provider,),
            )
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


# ── Convenience: pressure scalar for a model ─────────────────────────────────


async def get_model_pressure(model_id: str) -> float:
    """Return the budget pressure (0.0–1.0) for the provider of *model_id*.

    Used by the scorer to compute ``budget_availability = 1 - pressure``.

    Args:
        model_id: Full model identifier (e.g. ``"ollama/qwen3:32b"``,
            ``"openai/gpt-4o"``).

    Returns:
        Pressure float 0.0 (free) to 1.0 (exhausted).
    """
    provider = model_id.split("/")[0] if "/" in model_id else model_id
    state = await get_budget_state(provider)
    return state.pressure


def format_budget_summary(states: dict[str, BudgetState]) -> str:
    """Format a human-readable budget summary for the dashboard.

    Args:
        states: Dict from :func:`get_all_budget_states`.

    Returns:
        Multi-line markdown string showing pressure bars per provider.
    """
    lines = ["**Budget Oracle**\n"]
    for provider, state in sorted(states.items()):
        bar_len = 10
        filled = round(state.pressure * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        pct = f"{state.pressure:.0%}"
        if state.cap_usd > 0:
            spend_info = f"${state.spend_usd:.2f} / ${state.cap_usd:.2f}"
        elif provider in LOCAL_PROVIDERS:
            spend_info = "free (local)"
        else:
            spend_info = f"${state.spend_usd:.4f} (no cap)"
        lines.append(f"  {provider:<14} [{bar}] {pct:>4}  {spend_info}")
    return "\n".join(lines)
