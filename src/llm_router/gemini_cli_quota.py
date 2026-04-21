"""Gemini CLI quota tracking — monitor daily request limits.

Gemini CLI quotas are based on subscription tier:
- Google AI Pro: 1,500 requests/day
- Google One AI Pro: 1,500 requests/day
- Vertex AI: ~1,500 requests/day
- Free: ~250 requests/day

This module provides two-layer quota tracking:
1. Parse `gemini /stats` output for real auth method and tier info
2. Fall back to local counter if stats unavailable
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

TIER_LIMITS = {
    "gemini_code_assist_individual": 1000,
    "google_ai_pro": 1500,
    "google_one_ai_pro": 1500,
    "google_workspace_standard": 1500,
    "google_ai_ultra": 2000,
    "google_workspace_enterprise": 2000,
    "vertex_ai_express": 1500,
    "api_free": 250,
    "free": 250,
}
"""Known tier → daily request limit mapping."""

_QUOTA_CACHE_FILE = Path.home() / ".llm-router" / "gemini_quota.json"
_QUOTA_CACHE_TTL = 300  # 5 minutes


async def _run_gemini_stats() -> dict | None:
    """Run `gemini /stats` subprocess and parse tier/request data.

    Returns:
        Dict with keys: auth_method, tier, count, daily_limit
        Returns None if `gemini` not found or `/stats` not supported.
    """
    try:
        from llm_router.gemini_cli_agent import find_gemini_binary

        binary = find_gemini_binary()
        if not binary:
            return None

        # Attempt to run `gemini /stats` (may not be supported on all versions)
        proc = await asyncio.create_subprocess_exec(
            binary, "/stats",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        stats_output = stdout.decode("utf-8", errors="replace").strip()

        # Try to parse JSON output (expected format)
        try:
            data = json.loads(stats_output)
            return data
        except json.JSONDecodeError:
            # Fallback: try to extract tier from text output
            # Common output format: "Auth: Google One AI Pro\nRequests: 127/1500"
            lines = stats_output.lower()
            for tier_name, limit in TIER_LIMITS.items():
                if tier_name.replace("_", " ") in lines:
                    # Try to extract request count
                    import re
                    match = re.search(r"(\d+)/(\d+)", stats_output)
                    if match:
                        count = int(match.group(1))
                        return {
                            "tier": tier_name,
                            "count": count,
                            "daily_limit": limit,
                            "auth_method": tier_name,
                        }
            return None
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


def _load_quota_cache() -> dict | None:
    """Load cached quota from ~/.llm-router/gemini_quota.json if fresh.

    Returns:
        Cached quota dict if file exists and is <5 min old, else None.
    """
    if not _QUOTA_CACHE_FILE.exists():
        return None

    try:
        data = json.loads(_QUOTA_CACHE_FILE.read_text())
        cached_time = data.get("cached_at", 0)
        age_sec = time.time() - cached_time
        if age_sec < _QUOTA_CACHE_TTL:
            return data
    except Exception:
        pass
    return None


def _save_quota_cache(data: dict) -> None:
    """Save quota data to ~/.llm-router/gemini_quota.json."""
    try:
        _QUOTA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cache = {**data, "cached_at": time.time()}
        _QUOTA_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass  # Silently fail — quota tracking is best-effort


def _get_local_quota() -> dict | None:
    """Get or initialize local quota counter.

    Local fallback when `gemini /stats` is unavailable.
    Increments count for today, resets if date changed.
    """
    if not _QUOTA_CACHE_FILE.exists():
        return None

    try:
        data = json.loads(_QUOTA_CACHE_FILE.read_text())
        today = datetime.now(timezone.utc).date().isoformat()
        cached_date = data.get("date")

        if cached_date == today:
            # Same day — return existing count
            return data
        else:
            # New day — reset counter
            tier = data.get("tier", "google_one_ai_pro")
            daily_limit = TIER_LIMITS.get(tier, 1500)
            return {
                "date": today,
                "count": 0,
                "tier": tier,
                "daily_limit": daily_limit,
                "auth_method": data.get("auth_method", tier),
            }
    except Exception:
        # Initialize with sensible defaults
        tier = os.environ.get("GEMINI_CLI_TIER", "google_one_ai_pro")
        daily_limit = TIER_LIMITS.get(tier, 1500)
        return {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "count": 0,
            "tier": tier,
            "daily_limit": daily_limit,
            "auth_method": tier,
        }


async def get_gemini_pressure() -> float:
    """Get budget pressure for Gemini CLI (count / daily_limit).

    Fetches real quota from `gemini /stats` if available (accurate),
    falls back to local counter if unavailable.

    Returns:
        Float 0.0–1.0 representing daily quota usage.
        0.0 = unused, 1.0 = limit reached.
    """
    # Try fresh stats
    fresh_stats = await _run_gemini_stats()
    if fresh_stats:
        count = fresh_stats.get("count", 0)
        limit = fresh_stats.get("daily_limit", 1500)
        pressure = min(1.0, max(0.0, count / limit if limit > 0 else 0.0))
        _save_quota_cache(fresh_stats)
        return pressure

    # Try cache
    cached = _load_quota_cache()
    if cached:
        count = cached.get("count", 0)
        limit = cached.get("daily_limit", 1500)
        pressure = min(1.0, max(0.0, count / limit if limit > 0 else 0.0))
        return pressure

    # Fall back to local counter
    local = _get_local_quota()
    if local:
        count = local.get("count", 0)
        limit = local.get("daily_limit", 1500)
        pressure = min(1.0, max(0.0, count / limit if limit > 0 else 0.0))
        return pressure

    # Unknown — assume 0 pressure (quota available)
    return 0.0


async def get_gemini_quota_status() -> dict:
    """Get detailed Gemini CLI quota status.

    Returns:
        Dict with keys: count, daily_limit, tier, auth_method, pressure.
    """
    # Try fresh stats first
    fresh_stats = await _run_gemini_stats()
    if fresh_stats:
        count = fresh_stats.get("count", 0)
        limit = fresh_stats.get("daily_limit", 1500)
        _save_quota_cache(fresh_stats)
        return {
            "count": count,
            "daily_limit": limit,
            "tier": fresh_stats.get("tier", "unknown"),
            "auth_method": fresh_stats.get("auth_method", "unknown"),
            "pressure": min(1.0, max(0.0, count / limit if limit > 0 else 0.0)),
        }

    # Try cache
    cached = _load_quota_cache()
    if cached:
        count = cached.get("count", 0)
        limit = cached.get("daily_limit", 1500)
        return {
            "count": count,
            "daily_limit": limit,
            "tier": cached.get("tier", "unknown"),
            "auth_method": cached.get("auth_method", "unknown"),
            "pressure": min(1.0, max(0.0, count / limit if limit > 0 else 0.0)),
        }

    # Fall back to local
    local = _get_local_quota()
    if local:
        count = local.get("count", 0)
        limit = local.get("daily_limit", 1500)
        return {
            "count": count,
            "daily_limit": limit,
            "tier": local.get("tier", "unknown"),
            "auth_method": local.get("auth_method", "unknown"),
            "pressure": min(1.0, max(0.0, count / limit if limit > 0 else 0.0)),
        }

    # Unknown state
    return {
        "count": 0,
        "daily_limit": 1500,
        "tier": "unknown",
        "auth_method": "unknown",
        "pressure": 0.0,
    }


def log_gemini_request() -> None:
    """Increment local Gemini request counter for today.

    Called after each successful Gemini CLI call to track local quota.
    Resets counter if date has changed.
    """
    try:
        local = _get_local_quota() or {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "count": 0,
            "tier": "google_one_ai_pro",
            "daily_limit": 1500,
        }
        local["count"] = local.get("count", 0) + 1
        _save_quota_cache(local)
    except Exception:
        pass  # Silently fail
