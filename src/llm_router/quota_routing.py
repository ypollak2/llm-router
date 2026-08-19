"""F4: per-identity quota enforcement at the routing chokepoint.

Bridges :class:`llm_router.enterprise.quotas.QuotaTracker` into ``route_and_call``.
The tracker shipped fully-formed (its docstring even says "the router calls
``would_exceed()`` BEFORE dispatching") but had **zero callers from the routing
path** — per-identity caps were configurable via the admin API yet never
enforced at routing time. This module closes that gap.

Mirrors :mod:`llm_router.rbac_routing` / :mod:`llm_router.audit_routing`: a thin,
enterprise-gated wrapper with three modes via ``LLM_ROUTER_QUOTA_MODE``:

* **off** — no-op (the pre-F4 behaviour; developer-profile default).
* **warn** — log + audit a breach signal but allow the turn (dual-write window).
* **strict** — raise :class:`QuotaExceeded` BEFORE any reservation / dispatch /
  provider call. The caller pays nothing for the deny.

G-001-style default flip: when ``LLM_ROUTER_QUOTA_MODE`` is unset, enterprise profile
defaults to **strict**, developer profile to **off**. An explicit value always
wins so an operator can canary in ``warn`` before flipping to ``strict``.

Scope note: enforced for the **user** AND **team** scopes (P0-2). ``TurnIdentity``
now carries ``team_id`` (from the authenticated user in enterprise/OIDC mode,
``LLM_ROUTER_TEAM_ID`` in developer mode). Each populated scope is checked; the
tightest breach denies. ``record_consumption`` charges every populated scope, so
a *team* cap aggregates spend across all its users. A ``None`` team_id reduces to
the pre-P0-2 user-only behaviour.

🥷 Backslash-Security: using vibe-coding rules for secured Authentication & Authorization
"""
from __future__ import annotations

import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_QUOTA_MODE_ENV = "LLM_ROUTER_QUOTA_MODE"
_STRICT_VALUES = {"strict", "hard"}
_WARN_VALUES = {"warn", "soft", "shadow"}

# Lazy singleton tracker (mirrors rbac/audit bridges). Tests inject their own.
_tracker = None  # type: ignore[var-annotated]


def _resolve_mode() -> str:
    """Return ``'off'`` / ``'warn'`` / ``'strict'`` from env + profile."""
    raw = (os.environ.get(_QUOTA_MODE_ENV) or "").strip().lower()
    if raw in _STRICT_VALUES:
        return "strict"
    if raw in _WARN_VALUES:
        return "warn"
    if raw == "off":
        return "off"
    # Unset / unrecognised → profile-driven default.
    from llm_router.profile import is_enterprise
    return "strict" if is_enterprise() else "off"


def _get_tracker():
    """Resolve the process-wide QuotaTracker (cross-thread safe)."""
    global _tracker
    if _tracker is None:
        from llm_router.enterprise.quotas import QuotaTracker
        _tracker = QuotaTracker(check_same_thread=False)
    return _tracker


def check_quota(
    identity: Any,
    prospective_cost_usd: float = 0.0,
    *,
    tracker=None,
) -> tuple[str, bool, dict]:
    """Pre-dispatch quota gate. Returns ``(mode, breached, info)``.

    ``breached`` is True only for a HARD cap breach (per ``QuotaTracker``
    semantics); ``info`` carries the cap/consumed/period for the audit row and
    the structured refusal. With the default ``prospective_cost_usd=0.0`` this
    refuses a user already over their cap; the post-success ``record_consumption``
    advances the accumulator so the next over-cap call is refused.
    """
    mode = _resolve_mode()
    if mode == "off":
        return ("off", False, {})
    t = tracker or _get_tracker()
    # Check each populated scope, tightest attribution first (user → team).
    # The first HARD breach denies; a team cap bites even when the user has
    # none. A store hiccup on one scope never breaks routing.
    for scope, ident in _scopes(identity):
        try:
            breached, info = t.would_exceed(scope, ident, prospective_cost_usd)
        except Exception as exc:  # never let a quota-store hiccup break routing
            log.warning("quota_check_failed", scope=scope, error=str(exc))
            continue
        if breached:
            return (mode, True, info)
    return (mode, False, {})


def _scopes(identity: Any) -> list[tuple[str, str]]:
    """The (scope, identifier) pairs to enforce for this turn, in deny-priority
    order. Skips scopes the identity doesn't carry — ``team_id`` is None for
    single-user developer installs, collapsing to user-only enforcement."""
    pairs = []
    user_id = getattr(identity, "user_id", None)
    if user_id:
        pairs.append(("user", user_id))
    team_id = getattr(identity, "team_id", None)
    if team_id:
        pairs.append(("team", team_id))
    return pairs


def raise_quota_denied(info: dict):
    """Build the :class:`QuotaExceeded` to raise on a strict breach."""
    from llm_router.enterprise.quotas import QuotaExceeded

    return QuotaExceeded(
        scope=info.get("scope", "user"),
        identifier=info.get("identifier", "?"),
        period=info.get("period", "daily"),
        cap_usd=info.get("cap_usd", 0.0),
        consumed_usd=info.get("consumed_usd", 0.0),
        proposed_usd=info.get("proposed_usd", 0.0),
    )


def record_consumption(
    identity: Any,
    actual_cost_usd: float,
    *,
    tracker=None,
) -> None:
    """Record real per-identity spend after a successful, paid turn.

    Enterprise-gated and a no-op for off-mode, missing identity, or zero cost
    (cached / local / free turns spend nothing). Failures are logged, never
    raised — quota accounting must not break a turn the caller already received.
    """
    if _resolve_mode() == "off":
        return
    if not actual_cost_usd or actual_cost_usd <= 0:
        return
    t = tracker or _get_tracker()
    # Charge every populated scope — the same spend counts against the user's
    # cap and (when present) the team's cap, so a team budget aggregates.
    for scope, ident in _scopes(identity):
        try:
            t.consume(scope, ident, float(actual_cost_usd))
        except Exception as exc:
            log.warning("quota_consume_failed", scope=scope, error=str(exc))
