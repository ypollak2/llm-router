"""Shared server state — module-level variables that multiple tool modules read/write.

This module holds the two cross-cutting pieces of mutable state that several
tool groups need:

- ``_active_profile`` — routing profile override set by ``llm_set_profile``
- ``_last_usage`` — cached Claude subscription usage set by ``llm_update_usage``
  and ``llm_refresh_claude_usage``, read by routing and dashboard tools

All callers should use the get/set helpers rather than importing the private
variables directly, so that future persistence (e.g. writing to disk) can be
added in one place.
"""

from __future__ import annotations

import copy
import threading

from llm_router.config import get_config
from llm_router.types import PRO_FEATURES, Tier

# Imported lazily at call-site to avoid circular imports at module load time.
# Type annotation only — ClaudeSubscriptionUsage is not imported at the top level.

_active_profile = None          # RoutingProfile | None
_last_usage = None              # ClaudeSubscriptionUsage | None
_active_agent = None            # str | None — "claude_code" or "codex"

# Guards all module-level state mutations.  The MCP server runs on asyncio
# (single-threaded), so concurrent coroutines cannot race at the Python level,
# but the server may delegate work to a ThreadPoolExecutor.  A threading.Lock
# covers both cases without requiring callers to become async.
_state_lock = threading.Lock()


def get_active_profile():
    """Return the currently active routing profile (override or config default)."""
    if _active_profile is not None:
        return _active_profile
    return get_config().llm_router_profile


def set_active_profile(profile) -> None:
    """Set the active routing profile override (pass None to clear)."""
    global _active_profile
    with _state_lock:
        _active_profile = profile


def get_last_usage():
    """Return a copy of the cached ClaudeSubscriptionUsage object, or None.

    Returns a shallow copy so callers cannot mutate shared state.
    """
    with _state_lock:
        return copy.copy(_last_usage) if _last_usage is not None else None


def set_last_usage(usage) -> None:
    """Update the cached ClaudeSubscriptionUsage object."""
    global _last_usage
    with _state_lock:
        _last_usage = usage


def get_active_agent() -> str | None:
    """Return the currently active agent context ('claude_code' or 'codex'), or None."""
    with _state_lock:
        return _active_agent


def set_active_agent(agent: str | None) -> None:
    """Set the active agent context (pass None to clear)."""
    global _active_agent
    with _state_lock:
        _active_agent = agent


def _check_tier(feature: str) -> str | None:
    """Gate a feature behind the Pro subscription tier.

    Looks up ``feature`` in the ``PRO_FEATURES`` set.  If the user is on the
    free tier and the feature is Pro-only, returns a human-readable upgrade
    prompt.  Otherwise returns ``None`` (access granted).

    Args:
        feature: Internal feature name (e.g. ``"multi_step"``).

    Returns:
        An error message string if access is denied, or ``None`` if allowed.
    """
    config = get_config()
    if config.llm_router_tier == Tier.FREE and feature in PRO_FEATURES:
        return (
            f"'{feature}' requires Pro tier ($12/mo). "
            f"Current tier: free. Upgrade at https://llm-router.dev/pricing"
        )
    return None


def _format_time(seconds: float) -> str:
    """Format a duration in seconds into a compact human-readable string.

    Returns strings like ``"12.3s"``, ``"2.1m"``, or ``"1.5h"``.
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{seconds / 3600:.1f}h"
