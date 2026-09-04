"""``SUBSCRIPTION_LOCAL`` routing profile — enterprise cost-inverted
capability routing.

Models the common enterprise shape: **one** subscription provider
(the org's paid seat — Claude Team, ChatGPT Enterprise, Gemini
Workspace, …) plus **a free bucket** (Ollama, vLLM, llama.cpp, LM
Studio, and any org-hosted internal-models service). Cost-inverted
because the *direction* of preference flips on task complexity:

* **Simple / moderate.** The free bucket goes first. Subscription
  is the *fallback* so a routine prompt that Ollama could handle
  but Ollama happens to be down does not fail — it falls through
  to the seat the org already paid for.
* **Complex.** Subscription goes first. The free bucket is the
  *fallback* — if the seat is rate-limited, complex prompts try
  to complete on local capability rather than failing outright.

This matches the principle "minimize incremental spend without
hurting completion-rate." The pre-G-002 profiles (BUDGET / BALANCED
/ PREMIUM) treat subscription as just-another-paid-provider; they
have no concept of "the seat we already paid for, zero marginal
cost up to the quota."

Configuration:

* ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` — single provider name
  (e.g. ``anthropic`` / ``openai`` / ``gemini``). Unset → the seat
  table written by install / doctor / session-start
  (``~/.llm-router/seats.json``, see ``llm_router.seats``) supplies
  the strongest logged-in seat. No env and no seat → the profile
  no-ops. ``LLM_ROUTER_SEATS_AUTO=off`` disables the seat default.
* ``LLM_ROUTER_INTERNAL_PROVIDERS`` — comma-separated provider names
  the org hosts internally (e.g. ``internal_llm,company_mistral``).
  These join ``LOCAL_PROVIDERS`` to form the "free bucket."

The reorder is a stable sort on a small key, so the relative order
*within* each tier (free / subscription / other paid) is preserved
from whatever the scorer produced. Disabled-provider filtering
(G-006-F2) still runs *after* this, so an emergency-disabled
subscription is correctly excluded even when this profile would
have preferred it.

See: ``docs/audit/post-remediation/GAP_ANALYSIS.md`` — new entry
for SUBSCRIPTION_LOCAL coordination + the user request 2026-06-10.
"""
from __future__ import annotations

import os

from llm_router.types import LOCAL_PROVIDERS, RoutingProfile


_SUBSCRIPTION_PROVIDER_ENV = "LLM_ROUTER_SUBSCRIPTION_PROVIDER"
_INTERNAL_PROVIDERS_ENV = "LLM_ROUTER_INTERNAL_PROVIDERS"
_PRESSURE_THRESHOLD_ENV = "LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD"
# Option #2 (post-Stage-4 decision): when set to an off-ish value,
# restrict the reorder to the SUBSCRIPTION_LOCAL profile only,
# restoring pre-extension behaviour. Default ``on`` extends the
# reorder across BALANCED / PREMIUM / QUOTA_BALANCED so a strained
# subscription is demoted regardless of profile choice. See
# docs/audit/post-remediation/SUBSCRIPTION_DEMOTION_DECISION.md.
_REORDER_ALL_PROFILES_ENV = "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES"
_REORDER_OFF_VALUES = {"off", "0", "false", "no", "disabled"}

# Default: when the subscription's 5-hour quota is at or above this
# fraction, the reorder demotes subscription to AFTER the free
# bucket regardless of complexity — preserve the strained seat for
# the prompts that really need it.
_DEFAULT_PRESSURE_THRESHOLD = 0.80

# Complexity tiers whose default preference is *free first*. Anything
# not in this set is treated as complex (premium first). Matches the
# existing classifier vocabulary in ``llm_router.classifier``.
_FREE_FIRST_COMPLEXITIES: frozenset[str] = frozenset({"simple", "moderate"})

# Map subscription provider names (the ``LLM_ROUTER_SUBSCRIPTION_PROVIDER``
# value, lowercased) to the ``quota_balance.get_provider_pressures``
# dict keys. Unmapped providers return ``None`` pressure — the reorder
# falls back to complexity-only logic so we never crash on an
# unsupported subscription source.
_PROVIDER_TO_PRESSURE_KEY: dict[str, str] = {
    "anthropic": "claude",
    "openai": "codex",
    "gemini": "gemini_cli",
}


def _seats_auto_enabled() -> bool:
    """Seat-derived defaults are on unless ``LLM_ROUTER_SEATS_AUTO`` is off-ish."""
    raw = (os.environ.get("LLM_ROUTER_SEATS_AUTO") or "").strip().lower()
    return raw not in _REORDER_OFF_VALUES


def _cached_seats():
    """The seat table install / doctor / session-start wrote, or ``None``.
    A missing or unreadable cache means "no seat known" -- never an error."""
    if not _seats_auto_enabled():
        return None
    try:
        from llm_router.seats import load_seats
        return load_seats()
    except Exception:  # noqa: BLE001 -- a cache problem must not break routing
        return None


def get_subscription_provider() -> str | None:
    """Return the subscription provider name, or ``None`` when there is none.

    ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` wins when set. Otherwise the seat
    table (``~/.llm-router/seats.json``) supplies the default: the strongest
    logged-in seat, so a user who is logged in to Claude Code gets the
    cost-inverted reorder without setting anything. ``LLM_ROUTER_SEATS_AUTO=off``
    restores the env-only behaviour.
    """
    raw = (os.environ.get(_SUBSCRIPTION_PROVIDER_ENV) or "").strip().lower()
    if raw:
        return raw
    seats = _cached_seats()
    return seats.subscription_provider() if seats is not None else None


def get_internal_providers() -> frozenset[str]:
    """Return the set of org-hosted internal-model providers. Empty
    when unset. Combined with ``LOCAL_PROVIDERS`` to form the free
    bucket."""
    raw = (os.environ.get(_INTERNAL_PROVIDERS_ENV) or "").strip()
    if not raw:
        return frozenset()
    return frozenset(
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    )


def get_free_bucket() -> frozenset[str]:
    """Union of LOCAL_PROVIDERS + LLM_ROUTER_INTERNAL_PROVIDERS + the seats that
    are logged in but are not the subscription seat (a ChatGPT seat makes
    ``codex`` free; a Google seat makes ``gemini_cli`` free). All are "zero
    incremental cost" so the router treats them identically."""
    bucket = LOCAL_PROVIDERS | get_internal_providers()
    seats = _cached_seats()
    if seats is not None:
        bucket = bucket | seats.seat_free_providers()
    return bucket


def _provider_of(model_id: str) -> str:
    """Extract the provider segment of a ``provider/model`` id.
    Mirrors ``chain_builder._provider_of`` so both code paths use
    the same naive-split contract."""
    head, _, _ = model_id.partition("/")
    return (head or model_id).lower()


def get_pressure_threshold() -> float:
    """Threshold at/above which the subscription seat is treated as
    "strained" and demoted to last-paid. Defaults to ``0.80``;
    operators tune via ``LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD``."""
    raw = (os.environ.get(_PRESSURE_THRESHOLD_ENV) or "").strip()
    if not raw:
        return _DEFAULT_PRESSURE_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_PRESSURE_THRESHOLD
    # Clamp to a sane range — a 0.0 threshold would always demote, a
    # > 1.0 threshold would never demote; both useful for tests but
    # operators should know.
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


async def get_subscription_pressure() -> float | None:
    """Resolve the subscription provider's current quota pressure as
    a value in ``[0.0, 1.0]``. Returns ``None`` when:

    * No subscription provider is configured, or
    * The configured provider has no pressure source in
      ``quota_balance`` (e.g. a future / custom provider name), or
    * The underlying pressure-fetch raises for any reason.

    The reorder treats ``None`` as "no demotion signal" and uses the
    base complexity-driven ordering — never crash on a missing
    pressure source."""
    sub = get_subscription_provider()
    if sub is None:
        return None
    key = _PROVIDER_TO_PRESSURE_KEY.get(sub)
    if key is None:
        return None
    try:
        from llm_router.quota_balance import get_provider_pressures
        pressures = await get_provider_pressures()
    except Exception:  # noqa: BLE001 — pressure fetch never breaks routing
        return None
    return pressures.get(key)


def is_subscription_strained(pressure: float | None) -> bool:
    """True when the subscription provider's pressure is at or above
    the configured threshold. ``None`` pressure → False (no demotion
    signal)."""
    if pressure is None:
        return False
    return pressure >= get_pressure_threshold()


def _reorder_all_profiles_enabled() -> bool:
    """Option #2 gate. Default ``on``; operators flip off via
    ``LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES=off`` to restore the
    SUBSCRIPTION_LOCAL-only behaviour from before the Stage-4 escalation."""
    raw = (os.environ.get(_REORDER_ALL_PROFILES_ENV) or "").strip().lower()
    return raw not in _REORDER_OFF_VALUES


def is_subscription_local_active(profile: RoutingProfile) -> bool:
    """True when the cost-inverted reorder should drive chain ordering.

    Two activation paths (option #2):

    * ``profile == SUBSCRIPTION_LOCAL`` — the original explicit
      opt-in. Requires ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` to be set so
      the reorder has a target.
    * Any other profile (``BALANCED`` / ``PREMIUM`` /
      ``QUOTA_BALANCED`` / custom) when
      ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` is set AND the cross-profile
      extension is enabled (default). This is the Stage-4 escalation
      from the characterization audit — it ensures a strained
      subscription gets demoted under any profile, not just the
      explicit SUBSCRIPTION_LOCAL opt-in.

    Either path requires ``LLM_ROUTER_SUBSCRIPTION_PROVIDER``: the env
    alone (with no profile match and the extension disabled) is
    still a no-op. The reorder never silently changes behaviour
    against an operator's intent.
    """
    if get_subscription_provider() is None:
        return False
    if profile == RoutingProfile.SUBSCRIPTION_LOCAL:
        return True
    # Cross-profile extension (Stage-4 decision).
    return _reorder_all_profiles_enabled()


def reorder_for_subscription_local(
    chain: list[str],
    *,
    complexity: str,
    profile: RoutingProfile,
    subscription_pressure: float | None = None,
) -> list[str]:
    """Reorder ``chain`` per the SUBSCRIPTION_LOCAL profile rules.

    No-op when the profile or env is not configured — returns the
    input chain unchanged so callers can apply this unconditionally.

    Stable sort on a single tier key. Three regimes:

    1. **Strained subscription** (``subscription_pressure`` ≥
       ``LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD``, default ``0.80``).
       The 5-hour quota is nearly exhausted. The subscription seat
       drops to the *last* tier regardless of complexity — preserve
       it for the prompts that really need it. Order:
       ``free → other paid → subscription``.

    2. **Healthy subscription, simple / moderate complexity.** The
       free bucket goes first; subscription is the safety-net
       fallback. Order: ``free → subscription → other paid``.

    3. **Healthy subscription, complex / research / unknown.**
       Subscription wins the head; free bucket is the local
       fallback. Order: ``subscription → free → other paid``.

    Models within the same tier preserve their incoming relative
    order — whatever the scorer produced for cost / latency /
    failure-rate is honoured *within* the tier, the reorder just
    decides which tier wins at the head of the chain.

    ``subscription_pressure`` is caller-supplied so the reorder can
    stay synchronous; ``build_chain`` awaits
    ``get_subscription_pressure()`` and passes the float in.
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
            # Strained subscription: free → other paid → subscription.
            if provider in free:
                return 0
            if provider == sub:
                return 2
            return 1
        if free_first:
            # Healthy + simple/moderate: free → subscription → other.
            if provider in free:
                return 0
            if provider == sub:
                return 1
            return 2
        # Healthy + complex: subscription → free → other.
        if provider == sub:
            return 0
        if provider in free:
            return 1
        return 2

    return sorted(chain, key=tier_for)


__all__ = [
    "get_free_bucket",
    "get_internal_providers",
    "get_pressure_threshold",
    "get_subscription_pressure",
    "get_subscription_provider",
    "is_subscription_local_active",
    "is_subscription_strained",
    "reorder_for_subscription_local",
]


# Public re-export of the cross-profile gate so admin-facing tools
# (llm_router status / doctor / etc.) can surface whether the extension
# is currently active without parsing the env value themselves.
def is_cross_profile_extension_enabled() -> bool:
    """Whether option #2 (apply the reorder under any profile when a
    subscription is configured) is currently enabled. Default
    ``True``; flipped off by ``LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES=off``."""
    return _reorder_all_profiles_enabled()


__all__.append("is_cross_profile_extension_enabled")
