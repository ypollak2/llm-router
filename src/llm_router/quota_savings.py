"""Quota-saved metric — weekly + 5h subscription-percentage counterfactual.

Translates llm_router's per-call ``saved_usd`` (= Opus-equivalent counterfactual
minus actual cost) into the same denominator the user sees on
claude.ai: **percentage points of subscription quota**.

Surfaces
--------
1. The routing notice line (``hooks/response_formatter.format_echo_context``)
   appends a short form: ``"saved Xpp wk / Ypp 5h"``.
2. The MCP tool ``llm_quota_saved`` returns the full breakdown.

Calibration
-----------
``weekly_pct`` from claude.ai is denominated in opaque subscription units;
``saved_usd`` is denominated in dollars. To convert, we need a
``$_per_pp`` ratio. That ratio is derived from what the SUBSCRIPTION COSTS —
$200/month over 4.345 weeks, ≈$46/week — not from any token rate, and is
overridable via ``LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH`` or
``LLM_ROUTER_WEEKLY_QUOTA_USD``.

It previously read "default $50 — roughly the Opus-equivalent dollar value of
one week of Claude Pro Max", computed from $15/$75 per million: the retired
Opus 3 rate. See the note beside ``_default_weekly_quota_usd``. An "observed
calibration" path deriving the ratio from each user's own historical
claude_usage remains a follow-up (T-QS-2).

**Quota is not cash.** These figures are percentage points of prepaid capacity.
They are labelled as quota and must never be added to a dollar saving — a
subscription user has not been handed money, they have been handed headroom
(WP-05 / RED8-05).

Time windows
------------
* **Weekly** — UTC Monday 00:00 to now. Matches claude.ai's reset cadence.
* **5h** — last 5 hours rolling. Matches the session-limit window.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llm_router.logging import get_logger

log = get_logger("llm_router.quota_savings")


# Default calibration: USD value of one week of subscription quota.
#
# RED8-05 / WP-05: this was a hardcoded 50.0, justified in-comment as a "rough
# Opus-cost equivalent at $15-in/$75-out per million tokens". That is the
# RETIRED Opus 3 rate — the same 3x-stale price WP-03 removed everywhere else.
#
# The number survives scrutiny, but not for the stated reason, and the
# difference matters. What a week of quota is WORTH is anchored to what the
# subscription COSTS, not to any token rate: $200/month over 4.345 weeks is
# ~$46/week. Deriving it from token prices was the wrong model entirely — under
# that reasoning the figure would have had to drop by two thirds when Opus
# repriced, and nobody would have known to touch it.
#
# Anchoring to the subscription price also keeps the two quantities honestly
# distinct: quota is prepaid capacity, cash is money spent. WP-05 forbids
# summing them, and they are labelled separately downstream.
_SUBSCRIPTION_USD_PER_MONTH_DEFAULT = 200.0
_WEEKS_PER_MONTH = 4.345  # 365 / 12 / 7


def _default_weekly_quota_usd() -> float:
    """Weekly quota value in USD, overridable for a different plan.

    Read at call time rather than frozen at import, so a user on a $20 or $100
    plan can set an env var and have every derived figure follow. The old
    constant could not be corrected without editing the source.
    """
    override = os.environ.get("LLM_ROUTER_WEEKLY_QUOTA_USD", "").strip()
    if override:
        try:
            return float(override)
        except ValueError:
            log.warning("LLM_ROUTER_WEEKLY_QUOTA_USD=%r is not a number; ignoring", override)
    monthly = os.environ.get("LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH", "").strip()
    if monthly:
        try:
            return float(monthly) / _WEEKS_PER_MONTH
        except ValueError:
            log.warning("LLM_ROUTER_SUBSCRIPTION_USD_PER_MONTH=%r is not a number", monthly)
    return _SUBSCRIPTION_USD_PER_MONTH_DEFAULT / _WEEKS_PER_MONTH


@dataclass(frozen=True)
class QuotaSavingsSnapshot:
    """One snapshot of the user's quota-savings position.

    Pp = "percentage points" (additive, not multiplicative). E.g.
    counterfactual 47% with current 40% = ``7.0`` pp saved.
    """

    weekly_current_pct: float
    weekly_pp_saved: float
    weekly_counterfactual_pct: float
    weekly_saved_usd: float

    session_current_pct: float
    session_pp_saved: float
    session_counterfactual_pct: float
    session_saved_usd: float

    calibration_usd_per_pp: float
    calibration_source: str  # "configured" | "observed" — latter reserved

    def is_meaningful(self, threshold_pp: float = 0.5) -> bool:
        """True iff at least one window saved more than ``threshold_pp``.
        The routing-notice surface uses this to suppress noise when
        llm_router hasn't done anything cost-relevant yet."""
        return (
            self.weekly_pp_saved >= threshold_pp
            or self.session_pp_saved >= threshold_pp
        )

    def short_form(self) -> str:
        """Compact suffix for the routing notice line, e.g.
        ``"saved 7pp wk / 3pp 5h"``."""
        return (
            f"saved {self.weekly_pp_saved:.1f}pp wk / "
            f"{self.session_pp_saved:.1f}pp 5h"
        )


# ── Time window helpers ────────────────────────────────────────────────────


def _start_of_week_utc(now: datetime | None = None) -> datetime:
    """UTC Monday 00:00 most recently preceding ``now``."""
    now = now or datetime.now(timezone.utc)
    # weekday(): Mon=0..Sun=6
    days_since_mon = now.weekday()
    monday = (now - timedelta(days=days_since_mon)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday


def _start_of_5h_window_utc(now: datetime | None = None) -> datetime:
    """Now minus 5 hours."""
    now = now or datetime.now(timezone.utc)
    return now - timedelta(hours=5)


# ── Calibration ────────────────────────────────────────────────────────────


def _resolve_weekly_quota_usd() -> float:
    """Read the configured weekly quota in Opus-equivalent USD."""
    raw = os.environ.get("LLM_ROUTER_WEEKLY_QUOTA_USD_OPUS_EQUIV", "")
    if not raw:
        return _default_weekly_quota_usd()
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "invalid_weekly_quota_env",
            value=raw,
            fallback=_default_weekly_quota_usd(),
        )
        return _default_weekly_quota_usd()
    if value <= 0:
        return _default_weekly_quota_usd()
    return value


def _calibration_usd_per_pp() -> tuple[float, str]:
    """Return ``(usd_per_pp, source)``. ``source`` is "configured" or
    (future) "observed". Today the configured path is the only one;
    the second tuple element documents that for callers."""
    weekly_usd = _resolve_weekly_quota_usd()
    return weekly_usd / 100.0, "configured"


# ── DB query ───────────────────────────────────────────────────────────────


def _default_db_path() -> Path:
    """Resolve the usage DB path. Honours LLM_ROUTER_USAGE_DB_PATH for tests."""
    override = os.environ.get("LLM_ROUTER_USAGE_DB_PATH")
    if override:
        return Path(override)
    return Path.home() / ".llm-router" / "usage.db"


def _sum_saved_usd_since(db_path: Path, since: datetime) -> float:
    """Sum ``saved_usd`` from the ``usage`` table since ``since`` (UTC).

    Returns ``0.0`` when the DB is missing or the column is absent — the
    metric is purely additive and a missing DB should never break the
    routing notice. The ``usage.saved_usd`` column was added by an
    idempotent ALTER migration, so its presence isn't guaranteed on
    older deployments."""
    if not db_path.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            since_iso = since.strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "SELECT COALESCE(SUM(saved_usd), 0.0) FROM usage WHERE timestamp >= ?",
                (since_iso,),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0
        finally:
            conn.close()
    except sqlite3.OperationalError as err:
        # Most common: ``no such column: saved_usd`` on pre-migration DBs.
        log.debug("quota_savings_query_failed", error=str(err))
        return 0.0
    except Exception as err:
        log.warning("quota_savings_query_unexpected", error=str(err))
        return 0.0


# ── Public API ─────────────────────────────────────────────────────────────


def compute_quota_savings(
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> QuotaSavingsSnapshot | None:
    """Compute the current quota-savings snapshot.

    Returns ``None`` when no usage cache is available — without
    ``state.get_last_usage()`` we cannot anchor the counterfactual to
    a meaningful current %.
    """
    from llm_router import state as _state

    cached = _state.get_last_usage()
    if cached is None:
        return None

    # ClaudeSubscriptionUsage returns 0.0-1.0 fractions; we surface 0-100.
    weekly_current_pct = cached.weekly_pct * 100.0
    session_current_pct = cached.session_pct * 100.0

    db = db_path or _default_db_path()
    weekly_saved = _sum_saved_usd_since(db, _start_of_week_utc(now))
    session_saved = _sum_saved_usd_since(db, _start_of_5h_window_utc(now))

    usd_per_pp, source = _calibration_usd_per_pp()
    # Guard against pathological calibration (env injected as 0).
    if usd_per_pp <= 0:
        usd_per_pp = _default_weekly_quota_usd() / 100.0
        source = "configured"

    weekly_pp = weekly_saved / usd_per_pp if weekly_saved > 0 else 0.0
    session_pp = session_saved / usd_per_pp if session_saved > 0 else 0.0

    return QuotaSavingsSnapshot(
        weekly_current_pct=weekly_current_pct,
        weekly_pp_saved=weekly_pp,
        weekly_counterfactual_pct=weekly_current_pct + weekly_pp,
        weekly_saved_usd=weekly_saved,
        session_current_pct=session_current_pct,
        session_pp_saved=session_pp,
        session_counterfactual_pct=session_current_pct + session_pp,
        session_saved_usd=session_saved,
        calibration_usd_per_pp=usd_per_pp,
        calibration_source=source,
    )


# ── Per-call provider hint (subscription vs API tier) ────────────────────


_SUBSCRIPTION_PROVIDERS = frozenset({"anthropic", "cc"})
# Subscription-tier providers that auth via a parent subscription (no
# per-call dollar cost recorded in usage.db). They show the Claude
# subscription wk/5h pressure as a proxy for "AI routing pressure" —
# imperfect because codex is on an OpenAI ChatGPT account, but the
# best signal llm_router can surface without an OpenAI-side quota API.
_SUBSCRIPTION_AUTH_PROVIDERS = frozenset({"codex"})
_API_PROVIDERS = frozenset({"openai", "gemini", "groq", "deepseek"})
# RED2-3-03: gemini_cli is treated as free-local for cap routing (router.py's
# _FREE_LOCAL_PROVIDERS), so it must be in this bucket too or its calls silently
# lose their quota-hint line.
_FREE_LOCAL_PROVIDERS = frozenset({"ollama", "vllm", "lm_studio", "gemini_cli"})


def _sum_cost_usd_since_for_provider(
    db_path: Path, since: datetime, provider: str
) -> float:
    """Sum ``cost_usd`` from the ``usage`` table for one provider since
    ``since``. Best-effort: missing DB or schema → 0.0."""
    if not db_path.exists():
        return 0.0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            since_iso = since.strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage "
                "WHERE provider = ? AND timestamp >= ?",
                (provider, since_iso),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0.0
    except Exception:
        return 0.0


def provider_route_hint(
    provider: str,
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return a short routing-notice suffix specific to ``provider``.

    * **Claude subscription** (``anthropic`` / ``cc``) — show how much of
      the Claude subscription quota is still available, in the same
      weekly + 5h denomination the user sees on claude.ai. Reads from
      the cached usage snapshot.
    * **Other subscription-auth** (``codex`` — ChatGPT-account auth) —
      surface "<provider> sub" prefix plus the same Claude weekly + 5h
      numbers as a proxy for overall AI-routing pressure. Codex doesn't
      expose its own quota API, and an actual_cost of $0 was making the
      hint silently disappear under the API path; treating it as a
      subscription-auth provider keeps the routing notice consistent
      regardless of which model handled the turn.
    * **API tier** (``gemini``, ``openai``, ``groq``, ``deepseek``) — show
      the cumulative cost in the rolling last 30 days for THIS provider,
      so the user can see whether the routing they just got was pulling
      from a budget that is starting to bite.
    * **Free / local** (``ollama``, ``vllm``, ``lm_studio``) — return
      ``None``; no metric makes sense for a zero-cost local call.

    Best-effort: returns ``None`` on any data gap rather than raising.
    The routing notice must never break because a metric isn't ready.
    """
    if provider in _FREE_LOCAL_PROVIDERS:
        return None
    if provider in _SUBSCRIPTION_PROVIDERS:
        from llm_router import state as _state
        cached = _state.get_last_usage()
        if cached is None:
            return None
        weekly_left = max(0.0, 100.0 - cached.weekly_pct * 100.0)
        session_left = max(0.0, 100.0 - cached.session_pct * 100.0)
        return f"wk left {weekly_left:.0f}% · 5h left {session_left:.0f}%"
    if provider in _SUBSCRIPTION_AUTH_PROVIDERS:
        from llm_router import state as _state
        cached = _state.get_last_usage()
        if cached is None:
            return None
        weekly_left = max(0.0, 100.0 - cached.weekly_pct * 100.0)
        session_left = max(0.0, 100.0 - cached.session_pct * 100.0)
        return (
            f"{provider} sub · wk left {weekly_left:.0f}% "
            f"· 5h left {session_left:.0f}%"
        )
    if provider in _API_PROVIDERS:
        db = db_path or _default_db_path()
        since = (now or datetime.now(timezone.utc)) - timedelta(days=30)
        total = _sum_cost_usd_since_for_provider(db, since, provider)
        if total <= 0.0:
            return None
        return f"30d on {provider}: ${total:.2f}"
    return None


__all__ = [
    "QuotaSavingsSnapshot",
    "compute_quota_savings",
    "provider_route_hint",
]
