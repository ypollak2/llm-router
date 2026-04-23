"""Centralized quota tracking for Claude subscription and external LLM providers.

This module provides a singleton QuotaTracker that:
1. Maintains a local cache of quota state (usage.json)
2. Validates cache freshness against configurable TTL (default 5 min)
3. Auto-refreshes stale cache via OAuth (with 3-retry backoff)
4. Falls back to conservative defaults (50% pressure) on OAuth failure
5. Reads OpenAI/Gemini spend from local usage table (last 24h)

All quota reads go through get_snapshot() or force_refresh().
No code outside this module directly reads usage.json or makes OAuth calls.

Architecture:
- Router (async) imports QuotaTracker and calls get_snapshot() / force_refresh()
- Hooks (stdlib-only) contain inline replicas of TTL-check logic (no imports)
- Both use same conservative fallback logic on cache miss

Design rationale:
- Singleton ensures one "source of truth" for quota state
- TTL-based validation removes implicit session_pct >= 70% gate
- Retry logic with exponential backoff ensures reliability at session start
- Conservative fallback prevents overoptimistic routing decisions under failure
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiosqlite

from llm_router.config import get_config


@dataclass(frozen=True)
class QuotaSnapshot:
    """Immutable snapshot of quota state at a moment in time.
    
    Attributes:
        claude_session_pct: Claude session usage (0.0–1.0)
        claude_weekly_pct: Claude weekly usage (0.0–1.0)
        claude_sonnet_pct: Claude Sonnet usage (0.0–1.0)
        openai_spent_usd: OpenAI spend (last 24h, from usage table)
        gemini_spent_usd: Gemini spend (last 24h, from usage table)
        ollama_available: True if Ollama is configured and reachable
        cache_age_seconds: Age of cached usage.json in seconds
        is_fresh: True if cache_age < TTL (cache is current)
        refreshed_at: Unix timestamp of last refresh attempt
    """
    claude_session_pct: float
    claude_weekly_pct: float
    claude_sonnet_pct: float
    openai_spent_usd: float
    gemini_spent_usd: float
    ollama_available: bool
    cache_age_seconds: float
    is_fresh: bool
    refreshed_at: float


class QuotaTracker:
    """Singleton that manages quota state with TTL-based caching and OAuth refresh.
    
    Config (all overridable via environment variables):
    - TTL_SECONDS: Cache validity window (default 300s)
    - MAX_RETRY: Number of OAuth refresh attempts (default 3)
    - RETRY_DELAY_SEC: Delay between retries (default 2.0s)
    - USAGE_JSON: Path to cached usage.json (default ~/.llm-router/usage.json)
    - DB_PATH: Path to router SQLite DB for provider spend lookups
    
    Thread-safety: All methods are async; use in async context only.
    Fallback: If all OAuth attempts fail, returns _last_good snapshot or conservative defaults.
    """

    # Configuration (overridable via env vars)
    TTL_SECONDS: int = int(os.environ.get("LLM_ROUTER_QUOTA_TTL", "300"))
    MAX_RETRY: int = int(os.environ.get("LLM_ROUTER_QUOTA_RETRY", "3"))
    RETRY_DELAY_SEC: float = float(os.environ.get("LLM_ROUTER_QUOTA_DELAY", "2.0"))
    USAGE_JSON: Path = Path.home() / ".llm-router" / "usage.json"

    # Instance state
    _last_good: Optional[QuotaSnapshot] = None

    def __init__(self):
        """Initialize tracker. Call get_snapshot() to access the singleton."""
        pass

    async def get_snapshot(self) -> QuotaSnapshot:
        """Get current quota snapshot. Always safe to call. Never raises.
        
        Logic:
        1. If cache fresh (age < TTL): return from disk
        2. If cache stale: attempt refresh via OAuth (with retries)
           - On success: update _last_good, return fresh snapshot
           - On failure: return _last_good (marked is_fresh=False)
        3. If no cache ever: return conservative defaults (50% all pressures)
        
        Returns:
            QuotaSnapshot with current or fallback quota state
        """
        # Try to load from disk first
        snapshot = await self._load_from_disk()
        if snapshot and snapshot.is_fresh:
            return snapshot

        # Cache is stale or missing — attempt refresh
        try:
            snapshot = await self.force_refresh()
            return snapshot
        except Exception:
            # OAuth failed — return last good or conservative defaults
            if self._last_good:
                return QuotaSnapshot(
                    claude_session_pct=self._last_good.claude_session_pct,
                    claude_weekly_pct=self._last_good.claude_weekly_pct,
                    claude_sonnet_pct=self._last_good.claude_sonnet_pct,
                    openai_spent_usd=self._last_good.openai_spent_usd,
                    gemini_spent_usd=self._last_good.gemini_spent_usd,
                    ollama_available=self._last_good.ollama_available,
                    cache_age_seconds=time.time() - self._last_good.refreshed_at,
                    is_fresh=False,
                    refreshed_at=time.time(),
                )
            else:
                # No cache ever — return conservative defaults
                return QuotaSnapshot(
                    claude_session_pct=0.5,
                    claude_weekly_pct=0.5,
                    claude_sonnet_pct=0.5,
                    openai_spent_usd=0.0,
                    gemini_spent_usd=0.0,
                    ollama_available=await self._is_ollama_available(),
                    cache_age_seconds=float("inf"),
                    is_fresh=False,
                    refreshed_at=time.time(),
                )

    async def force_refresh(self) -> QuotaSnapshot:
        """Force OAuth refresh of Claude quota. Retries up to MAX_RETRY times.
        
        Raises:
            RuntimeError if all retries fail
        
        Returns:
            Fresh QuotaSnapshot with is_fresh=True
        """
        last_err = None
        for attempt in range(self.MAX_RETRY):
            try:
                # Fetch fresh quota from OAuth
                session_pct, weekly_pct, sonnet_pct = (
                    await self._fetch_claude_oauth_data()
                )

                # Load provider spend from DB
                openai_spend, gemini_spend = await self._load_provider_spend()

                # Create and cache snapshot
                snapshot = QuotaSnapshot(
                    claude_session_pct=session_pct,
                    claude_weekly_pct=weekly_pct,
                    claude_sonnet_pct=sonnet_pct,
                    openai_spent_usd=openai_spend,
                    gemini_spent_usd=gemini_spend,
                    ollama_available=await self._is_ollama_available(),
                    cache_age_seconds=0.0,
                    is_fresh=True,
                    refreshed_at=time.time(),
                )

                # Write to disk and update fallback
                await self._write_to_disk(snapshot)
                self._last_good = snapshot
                return snapshot

            except Exception as err:
                last_err = err
                if attempt < self.MAX_RETRY - 1:
                    await asyncio.sleep(self.RETRY_DELAY_SEC)

        # All retries exhausted
        raise RuntimeError(
            f"QuotaTracker: OAuth refresh failed ({self.MAX_RETRY} attempts): {last_err}"
        )

    async def _load_from_disk(self) -> Optional[QuotaSnapshot]:
        """Load quota snapshot from cached usage.json. Returns None if file missing or invalid."""
        if not self.USAGE_JSON.exists():
            return None

        try:
            data = json.loads(self.USAGE_JSON.read_text())
            updated_at = data.get("updated_at", 0)
            age = time.time() - updated_at

            # Load provider spend
            openai_spend, gemini_spend = await self._load_provider_spend()

            snapshot = QuotaSnapshot(
                claude_session_pct=float(data.get("session_pct", 0.5)),
                claude_weekly_pct=float(data.get("weekly_pct", 0.5)),
                claude_sonnet_pct=float(data.get("sonnet_pct", 0.5)),
                openai_spent_usd=openai_spend,
                gemini_spent_usd=gemini_spend,
                ollama_available=await self._is_ollama_available(),
                cache_age_seconds=age,
                is_fresh=age < self.TTL_SECONDS,
                refreshed_at=updated_at,
            )
            return snapshot
        except Exception:
            return None

    async def _fetch_claude_oauth_data(self) -> tuple[float, float, float]:
        """Fetch fresh Claude quota data via OAuth. Raises on failure.
        
        Returns:
            (session_pct, weekly_pct, sonnet_pct) as floats 0.0–1.0
        """
        # This is a placeholder — real implementation would call Anthropic OAuth endpoint
        # For now, we use the pattern from session-start.py's _fetch_claude_oauth_data
        # The router will receive this data from the hook which refreshes it
        # This method is mainly for force_refresh() to trigger a new fetch

        # Import here to avoid circular dependency
        from llm_router.claude_usage import refresh_claude_usage

        data = await refresh_claude_usage()
        return (
            data.get("session_pct", 0.5),
            data.get("weekly_pct", 0.5),
            data.get("sonnet_pct", 0.5),
        )

    async def _write_to_disk(self, snapshot: QuotaSnapshot) -> None:
        """Write quota snapshot to usage.json for hook consumption."""
        self.USAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "session_pct": snapshot.claude_session_pct,
            "weekly_pct": snapshot.claude_weekly_pct,
            "sonnet_pct": snapshot.claude_sonnet_pct,
            "updated_at": snapshot.refreshed_at,
            "is_fresh": snapshot.is_fresh,
        }
        self.USAGE_JSON.write_text(json.dumps(data))

    async def _load_provider_spend(self) -> tuple[float, float]:
        """Load OpenAI and Gemini spend from local usage table (last 24h).
        
        Returns:
            (openai_usd, gemini_usd) tuple
        """
        db_path = Path(
            os.environ.get(
                "LLM_ROUTER_DB_PATH",
                Path.home() / ".llm-router" / "usage.db",
            )
        )

        if not db_path.exists():
            return 0.0, 0.0

        try:
            async with aiosqlite.connect(str(db_path), timeout=5) as db:
                # OpenAI spend
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(cost_usd), 0)
                    FROM usage
                    WHERE provider = 'openai' 
                    AND timestamp > datetime('now', '-24 hours')
                    """
                )
                row = await cursor.fetchone()
                openai_spend = row[0] if row else 0.0

                # Gemini spend
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(cost_usd), 0)
                    FROM usage
                    WHERE provider = 'gemini'
                    AND timestamp > datetime('now', '-24 hours')
                    """
                )
                row = await cursor.fetchone()
                gemini_spend = row[0] if row else 0.0

                return float(openai_spend), float(gemini_spend)
        except Exception:
            return 0.0, 0.0

    async def _is_ollama_available(self) -> bool:
        """Check if Ollama is configured and reachable."""
        config = get_config()
        if not config.ollama_base_url:
            return False

        # This is a placeholder — real check would do a HEAD request
        # For now, just check if configured
        return True


# Module-level singleton instance
_tracker: Optional[QuotaTracker] = None


def get_tracker() -> QuotaTracker:
    """Get the module-level QuotaTracker singleton."""
    global _tracker
    if _tracker is None:
        _tracker = QuotaTracker()
    return _tracker


async def get_snapshot() -> QuotaSnapshot:
    """Convenience function: get current quota snapshot.
    
    Always safe to call. Never raises. Returns fallback on failure.
    """
    return await get_tracker().get_snapshot()


async def force_refresh() -> QuotaSnapshot:
    """Convenience function: force OAuth refresh with retries.
    
    Raises RuntimeError if all retries fail.
    """
    return await get_tracker().force_refresh()
