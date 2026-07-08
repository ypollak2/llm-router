"""``SUBSCRIPTION_LOCAL`` routing — cost-inverted capability routing.

Models the common shape: **one** subscription provider (the paid seat — Claude
Pro/Team, ChatGPT, Gemini …) plus **a free bucket** (Ollama, vLLM, llama.cpp, LM
Studio, and any self-hosted internal models). Cost-inverted because the direction
of preference flips on task complexity:

* **Simple / moderate.** Free bucket first; subscription is the fallback, so a
  routine prompt Ollama could handle but Ollama is down still completes on the
  seat you already pay for.
* **Complex.** Subscription first; the free bucket is the fallback, so if the
  seat is rate-limited, hard prompts try to complete locally rather than fail.

The reorder is a stable sort on a small tier key, so relative order *within* a
tier (whatever the scorer produced) is preserved.

Ported from Chuzom's ``subscription_local_routing.py``; env vars renamed to
``LLM_ROUTER_*`` and the quota-pressure source made a pluggable hook.

Configuration (all optional — unset ⇒ this is a complete no-op):

* ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` — single provider name
  (``anthropic`` / ``openai`` / ``gemini`` / …). Empty/unset ⇒ no reorder.
* ``LLM_ROUTER_INTERNAL_PROVIDERS`` — comma-separated self-hosted providers that
  join ``LOCAL_PROVIDERS`` to form the free bucket.
* ``LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD`` — quota fraction (default 0.80)
  at/above which the seat is demoted to last regardless of complexity.
* ``LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES`` — ``off`` restricts the
  reorder to the explicit ``SUBSCRIPTION_LOCAL`` profile (default: apply under
  any profile once a subscription is configured).
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable, Optional

from llm_router.types import LOCAL_PROVIDERS, RoutingProfile

_SUBSCRIPTION_PROVIDER_ENV = "LLM_ROUTER_SUBSCRIPTION_PROVIDER"
_INTERNAL_PROVIDERS_ENV = "LLM_ROUTER_INTERNAL_PROVIDERS"
_PRESSURE_THRESHOLD_ENV = "LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD"
_REORDER_ALL_PROFILES_ENV = "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES"
_REORDER_OFF_VALUES = {"off", "0", "false", "no", "disabled"}

_DEFAULT_PRESSURE_THRESHOLD = 0.80

# Complexity tiers whose default preference is *free first*. Anything else is
# treated as complex (subscription first).
_FREE_FIRST_COMPLEXITIES: frozenset = frozenset({"simple", "moderate"})

# Pluggable quota-pressure source. Host apps can register a coroutine returning
# a {provider_name: pressure_0_to_1} dict (e.g. from quota_tracker). Unset ⇒ the
# reorder uses complexity-only logic and never demotes on pressure.
_PressureProvider = Callable[[], Awaitable[dict]]
_pressure_provider: Optional[_PressureProvider] = None


def set_pressure_provider(provider: Optional[_PressureProvider]) -> None:
    """Register (or clear with ``None``) the async quota-pressure source used by
    :func:`get_subscription_pressure`. Keeps this module decoupled from any
    specific quota backend."""
    global _pressure_provider
    _pressure_provider = provider


def get_subscription_provider() -> Optional[str]:
    """The subscription provider name, or ``None`` when unset (⇒ no-op)."""
    raw = (os.environ.get(_SUBSCRIPTION_PROVIDER_ENV) or "").strip().lower()
    return raw or None


def get_internal_providers() -> frozenset:
    """Self-hosted internal-model providers (comma-separated env). Empty when unset."""
    raw = (os.environ.get(_INTERNAL_PROVIDERS_ENV) or "").strip()
    if not raw:
        return frozenset()
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def get_free_bucket() -> frozenset:
    """LOCAL_PROVIDERS ∪ internal providers — all zero incremental cost."""
    return LOCAL_PROVIDERS | get_internal_providers()


def _provider_of(model_id: str) -> str:
    """Provider segment of a ``provider/model`` id (naive split)."""
    head, _, _ = model_id.partition("/")
    return (head or model_id).lower()


def get_pressure_threshold() -> float:
    """Quota fraction at/above which the seat is demoted to last. Default 0.80;
    clamped to [0, 1]."""
    raw = (os.environ.get(_PRESSURE_THRESHOLD_ENV) or "").strip()
    if not raw:
        return _DEFAULT_PRESSURE_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_PRESSURE_THRESHOLD
    return max(0.0, min(1.0, value))


async def get_subscription_pressure() -> Optional[float]:
    """Current quota pressure (0..1) of the subscription provider, or ``None``.

    Resolves via the registered pressure provider (see :func:`set_pressure_provider`).
    Returns ``None`` when no subscription is configured, no provider is registered,
    the provider has no entry, or the fetch raises — the reorder treats ``None`` as
    "no demotion signal" and never crashes routing on a missing source.
    """
    sub = get_subscription_provider()
    if sub is None or _pressure_provider is None:
        return None
    try:
        pressures = await _pressure_provider()
    except Exception:  # pressure fetch never breaks routing
        return None
    if not isinstance(pressures, dict):
        return None
    val = pressures.get(sub)
    return float(val) if isinstance(val, (int, float)) else None


def is_subscription_strained(pressure: Optional[float]) -> bool:
    """True when pressure ≥ threshold. ``None`` ⇒ False (no signal)."""
    if pressure is None:
        return False
    return pressure >= get_pressure_threshold()


def _reorder_all_profiles_enabled() -> bool:
    raw = (os.environ.get(_REORDER_ALL_PROFILES_ENV) or "").strip().lower()
    return raw not in _REORDER_OFF_VALUES


def is_cross_profile_extension_enabled() -> bool:
    """Whether the reorder applies under any profile (not just SUBSCRIPTION_LOCAL)
    when a subscription is configured. Default True; ``...REORDER_ALL_PROFILES=off``
    restricts it to the explicit profile."""
    return _reorder_all_profiles_enabled()


def is_subscription_local_active(profile: RoutingProfile) -> bool:
    """True when the cost-inverted reorder should drive ordering. Requires
    ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` set; then active for the explicit
    ``SUBSCRIPTION_LOCAL`` profile, or any profile when the cross-profile
    extension is enabled (default)."""
    if get_subscription_provider() is None:
        return False
    if profile == RoutingProfile.SUBSCRIPTION_LOCAL:
        return True
    return _reorder_all_profiles_enabled()


def reorder_for_subscription_local(
    chain: list,
    *,
    complexity: str,
    profile: RoutingProfile,
    subscription_pressure: Optional[float] = None,
) -> list:
    """Reorder ``chain`` per the cost-inverted rules. No-op (returns the input
    unchanged) when unconfigured, so callers can apply it unconditionally.

    Three regimes (stable sort on a single tier key):

    1. **Strained seat** (pressure ≥ threshold): ``free → other paid → subscription``.
    2. **Healthy, simple/moderate**: ``free → subscription → other paid``.
    3. **Healthy, complex/other**: ``subscription → free → other paid``.
    """
    if not is_subscription_local_active(profile):
        return chain

    sub = get_subscription_provider()
    free = get_free_bucket()
    strained = is_subscription_strained(subscription_pressure)
    free_first = complexity in _FREE_FIRST_COMPLEXITIES

    def tier_for(model_id: str) -> int:
        provider = _provider_of(model_id)
        if strained:
            if provider in free:
                return 0
            if provider == sub:
                return 2
            return 1
        if free_first:
            if provider in free:
                return 0
            if provider == sub:
                return 1
            return 2
        if provider == sub:
            return 0
        if provider in free:
            return 1
        return 2

    return sorted(chain, key=tier_for)


__all__ = [
    "set_pressure_provider",
    "get_subscription_provider",
    "get_internal_providers",
    "get_free_bucket",
    "get_pressure_threshold",
    "get_subscription_pressure",
    "is_subscription_strained",
    "is_subscription_local_active",
    "is_cross_profile_extension_enabled",
    "reorder_for_subscription_local",
]
