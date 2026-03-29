"""Real-time Claude subscription usage via claude.ai internal API.

Fetches usage data from claude.ai/api/organizations/{org}/usage -- the same
JSON endpoint the web UI calls. Requires an authenticated browser session
(Playwright MCP) since auth cookies are httpOnly.

One ``browser_evaluate(fetch(...))`` call returns everything -- no DOM scraping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


FETCH_USAGE_JS = """async () => {
    const orgId = document.cookie.match(/lastActiveOrg=([^;]+)/)?.[1];
    if (!orgId) return { error: 'No org ID in cookies — not logged in?' };

    const base = `/api/organizations/${orgId}`;
    const [usage, sub, limit, credits] = await Promise.all([
        fetch(`${base}/usage`).then(r => r.json()),
        fetch(`${base}/subscription_details`).then(r => r.json()),
        fetch(`${base}/overage_spend_limit`).then(r => r.json()),
        fetch(`${base}/prepaid/credits`).then(r => r.json()),
    ]);
    return { org_id: orgId, usage, subscription: sub, overage: limit, credits };
}"""
"""JavaScript snippet executed inside an authenticated claude.ai browser tab.

Extracts the organization ID from cookies, then makes four parallel fetch calls
to the internal API to collect usage quotas, subscription details, overage spend
limits, and prepaid credit balances. Returns a single JSON object combining all
four responses, avoiding multiple roundtrips through the Playwright MCP bridge.
"""


@dataclass(frozen=True)
class UsageLimit:
    """A single usage limit bucket (e.g. session, weekly, model-specific).

    Attributes:
        label: Human-readable name for display (e.g. "Session (5h)", "Weekly (all)").
        pct_used: Fraction of this limit consumed, 0.0-1.0. Derived from the
            API's ``utilization`` percentage divided by 100.
        resets_at: ISO 8601 timestamp (or human-readable string) indicating when
            this limit resets. Empty string if unknown.
        detail: Optional additional context (e.g. token count breakdown).
    """

    label: str
    pct_used: float
    resets_at: str
    detail: str | None = None


@dataclass(frozen=True)
class ClaudeSubscriptionUsage:
    """Parsed usage data from the claude.ai API, combining all limit buckets.

    Contains both raw limit data and computed properties for routing decisions.
    The ``effective_pressure`` property is the primary signal consumed by the
    model selector to decide when to downshift from expensive models.

    Attributes:
        session: 5-hour rolling session limit (most volatile, resets frequently).
        weekly_all: 7-day limit across all models.
        weekly_sonnet: 7-day limit specific to Sonnet usage.
        weekly_opus: 7-day limit specific to Opus usage (None if not on Opus plan).
        extra_usage_enabled: Whether the user has opted into pay-per-use overage.
        extra_usage_spent: USD spent on overage this billing cycle.
        extra_usage_limit: Maximum USD allowed for overage spending.
        prepaid_balance: Remaining prepaid credit balance in USD.
        plan_status: Subscription status string (e.g. "active", "trialing").
        next_charge: Date string of the next billing charge.
        org_id: The Claude organization ID extracted from cookies.
        raw: The complete unprocessed API response for debugging.
    """

    session: UsageLimit | None = None
    weekly_all: UsageLimit | None = None
    weekly_sonnet: UsageLimit | None = None
    weekly_opus: UsageLimit | None = None
    extra_usage_enabled: bool = False
    extra_usage_spent: float = 0.0
    extra_usage_limit: float = 0.0
    prepaid_balance: float = 0.0
    plan_status: str = ""
    next_charge: str = ""
    org_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def session_pct(self) -> float:
        """Session usage 0.0-1.0 -- the primary budget pressure signal.

        The 5-hour session limit is the most constraining in practice because
        it resets most frequently and throttles first.
        """
        return self.session.pct_used if self.session else 0.0

    @property
    def weekly_pct(self) -> float:
        """Weekly all-models usage 0.0-1.0."""
        return self.weekly_all.pct_used if self.weekly_all else 0.0

    @property
    def sonnet_pct(self) -> float:
        """Weekly Sonnet-only usage 0.0-1.0."""
        return self.weekly_sonnet.pct_used if self.weekly_sonnet else 0.0

    @property
    def highest_pressure(self) -> float:
        """The most constrained limit (raw percentage) across all buckets.

        Used for display and as the starting point for ``effective_pressure``.
        Takes the max of session, weekly, and sonnet limits.
        """
        return max(self.session_pct, self.weekly_pct, self.sonnet_pct)

    @property
    def minutes_until_session_reset(self) -> float | None:
        """Minutes until the 5-hour session limit resets, or None if unknown.

        Returns:
            Non-negative float of minutes remaining, or None if no session
            data or the reset timestamp is unparseable.
        """
        if not self.session or not self.session.resets_at:
            return None
        try:
            dt = datetime.fromisoformat(self.session.resets_at)
            now = datetime.now(timezone.utc)
            return max(0, (dt - now).total_seconds() / 60)
        except (ValueError, TypeError):
            return None

    @property
    def effective_pressure(self) -> float:
        """Time-aware budget pressure for routing decisions.

        This is the key signal the model selector uses. It adjusts raw pressure
        based on how soon the session limit will reset:

        - **Below 85%**: Returns raw pressure unchanged (no adjustment needed).
        - **85%+ with reset <= 30 min away**: Reduces pressure by up to 50%
          because the quota will refresh soon. At 0 minutes remaining, pressure
          is halved; at 30 minutes, it's unchanged. This prevents unnecessary
          model downshifting right before a reset.
        - **85%+ with reset > 30 min away**: Returns raw pressure unchanged
          to preserve remaining quota across the longer session window.
        - **Unknown reset time**: Returns raw pressure unchanged (conservative).

        Returns:
            Adjusted pressure value, 0.0-1.0.
        """
        raw = self.highest_pressure
        mins = self.minutes_until_session_reset

        if mins is None or raw < 0.85:
            return raw

        # Session is 85%+ -- factor in time until reset
        if mins <= 30:
            # Reset imminent -- reduce pressure, don't downshift
            # Scale: at 0 min -> multiply by 0.5, at 30 min -> multiply by 1.0
            time_factor = 0.5 + (mins / 30) * 0.5
            return raw * time_factor
        else:
            # Still plenty of time in session -- keep full pressure
            return raw

    def summary(self) -> str:
        """Render a fixed-width ASCII box summarizing all usage limits and pressure.

        The output includes usage bars for each active limit bucket, extra
        usage / prepaid status if applicable, and a pressure assessment line
        indicating whether model downshifting is active.

        Returns:
            Multi-line string suitable for terminal display (58-char wide box).
        """
        w = 58
        lines = [
            "+" + "-" * w + "+",
            "|" + " Claude Subscription (Live) ".center(w) + "|",
            "+" + "-" * w + "+",
        ]

        if self.session:
            lines.append(_row("Session", self.session_pct, _time_until(self.session.resets_at), w))
        if self.weekly_all:
            lines.append(_row("Weekly (all)", self.weekly_pct, _time_until(self.weekly_all.resets_at), w))
        if self.weekly_sonnet:
            lines.append(_row("Sonnet only", self.sonnet_pct, _time_until(self.weekly_sonnet.resets_at), w))
        if self.weekly_opus:
            lines.append(_row("Opus only", self.weekly_opus.pct_used, _time_until(self.weekly_opus.resets_at), w))

        lines.append("+" + "-" * w + "+")

        if self.extra_usage_limit > 0:
            spent_pct = self.extra_usage_spent / self.extra_usage_limit if self.extra_usage_limit else 0
            status = "ON" if self.extra_usage_enabled else "OFF"
            extra = f"  Extra usage: {status}  ${self.extra_usage_spent:.2f} / ${self.extra_usage_limit:.0f} ({spent_pct:.0%})"
            lines.append(f"| {extra:<{w-1}}|")

        if self.prepaid_balance > 0:
            lines.append(f"| {'  Prepaid: $' + f'{self.prepaid_balance:.2f}':<{w-1}}|")

        pressure = self.highest_pressure
        effective = self.effective_pressure
        if pressure >= 0.90:
            status_line = f"  !! PRESSURE: {pressure:.0%} -- downshifting active"
        elif pressure >= 0.85 and effective < 0.85:
            mins = self.minutes_until_session_reset
            status_line = f"  OK PRESSURE: {pressure:.0%} raw, {effective:.0%} effective (reset in {int(mins or 0)}m)"
        elif pressure >= 0.70:
            status_line = f"  ~~ PRESSURE: {pressure:.0%} -- nearing threshold"
        else:
            status_line = f"  OK PRESSURE: {pressure:.0%} -- full model selection"
        lines.append(f"| {status_line:<{w-1}}|")
        lines.append(f"| {'  Plan: ' + self.plan_status + '  |  Next charge: ' + self.next_charge:<{w-1}}|")
        lines.append("+" + "-" * w + "+")

        return "\n".join(lines)


def _row(label: str, pct: float, time_str: str, box_width: int) -> str:
    """Render a single usage row inside the ASCII summary box.

    Formats: ``"  Session      [====........]  17%  (resets in 2h 15m)"``

    Args:
        label: Limit name, left-aligned in a 12-char column.
        pct: Usage fraction 0.0-1.0, shown as a bar and percentage.
        time_str: Parenthesized reset-time string from ``_time_until()``.
        box_width: Total inner width of the ASCII box (excluding borders).

    Returns:
        A single box row string with ``|`` borders.
    """
    bar = _bar(pct, 12)
    pct_str = f"{pct:.0%}".rjust(4)
    time_short = time_str.strip("()")
    # Truncate time to fit within the available column width
    max_time = box_width - 2 - 2 - 12 - 14 - 1 - 4 - 2  # margins, label, bar, pct
    if len(time_short) > max_time:
        time_short = time_short[:max_time]
    content = f"  {label:<12} {bar} {pct_str}  {time_short}"
    return f"| {content:<{box_width-1}}|"


def _bar(pct: float, width: int = 20) -> str:
    """Render an ASCII progress bar.

    Args:
        pct: Fill fraction, 0.0-1.0.
        width: Number of characters inside the brackets.

    Returns:
        String like ``"[====........]"`` of total length ``width + 2``.
    """
    filled = round(pct * width)
    return "[" + "=" * filled + "." * (width - filled) + "]"


def _time_until(iso_ts: str) -> str:
    """Convert an ISO timestamp to a human-readable "resets in..." string.

    Args:
        iso_ts: ISO 8601 timestamp string (e.g. from the usage API).

    Returns:
        One of:
        - ``""`` if input is empty
        - ``"(reset pending)"`` if the timestamp is in the past
        - ``"(resets in Xh Ym)"`` if less than 24 hours away
        - ``"(resets Mon 3:00 PM)"`` if 24+ hours away (shows local day/time)
        - ``"(resets: <raw>)"`` if the timestamp can't be parsed
    """
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
        now = datetime.now(timezone.utc)
        delta = dt - now
        total_sec = delta.total_seconds()

        if total_sec <= 0:
            return "(reset pending)"
        if total_sec < 86400:  # less than 24h
            hours = int(total_sec // 3600)
            mins = int((total_sec % 3600) // 60)
            return f"(resets in {hours}h {mins}m)"
        else:
            # Show day and time in the user's local timezone
            local = dt.astimezone()
            return f"(resets {local.strftime('%a %I:%M %p')})"
    except (ValueError, TypeError):
        return f"(resets: {iso_ts})"


def parse_api_response(data: dict) -> ClaudeSubscriptionUsage:
    """Parse the JSON response from the claude.ai internal usage API.

    Expects the structure returned by ``FETCH_USAGE_JS``:

    .. code-block:: json

        {
            "org_id": "org-abc123",
            "usage": {
                "five_hour": {"utilization": 17, "resets_at": "2024-01-15T12:00:00Z"},
                "seven_day": {"utilization": 21, "resets_at": "..."},
                "seven_day_sonnet": {"utilization": 26, "resets_at": "..."},
                "seven_day_opus": null
            },
            "subscription": {"status": "active", "next_charge_date": "2024-02-01"},
            "overage": {
                "is_enabled": true,
                "monthly_credit_limit": 5000,
                "used_credits": 2005
            },
            "credits": {"amount": 0}
        }

    Overage amounts are in cents (divided by 100 to get USD). If the response
    contains an ``"error"`` key, returns a mostly-empty ``ClaudeSubscriptionUsage``
    with the raw data preserved for debugging.

    Args:
        data: The parsed JSON dict from ``browser_evaluate(FETCH_USAGE_JS)``.

    Returns:
        A fully-populated ``ClaudeSubscriptionUsage`` instance.
    """
    if "error" in data:
        return ClaudeSubscriptionUsage(raw=data)

    usage = data.get("usage", {})
    sub = data.get("subscription", {})
    overage = data.get("overage", {})
    credits = data.get("credits", {})

    def _limit(key: str, label: str) -> UsageLimit | None:
        """Extract a single usage limit from the usage dict, or None if absent."""
        entry = usage.get(key)
        if not entry:
            return None
        return UsageLimit(
            label=label,
            pct_used=entry.get("utilization", 0) / 100,
            resets_at=entry.get("resets_at", ""),
        )

    # Overage: API returns credits in cents, convert to USD
    limit_cents = overage.get("monthly_credit_limit", 0) or 0
    used_cents = overage.get("used_credits", 0) or 0

    return ClaudeSubscriptionUsage(
        session=_limit("five_hour", "Session (5h)"),
        weekly_all=_limit("seven_day", "Weekly (all)"),
        weekly_sonnet=_limit("seven_day_sonnet", "Weekly (Sonnet)"),
        weekly_opus=_limit("seven_day_opus", "Weekly (Opus)"),
        extra_usage_enabled=overage.get("is_enabled", False),
        extra_usage_spent=used_cents / 100,
        extra_usage_limit=limit_cents / 100,
        prepaid_balance=(credits.get("amount", 0) or 0) / 100,
        plan_status=sub.get("status", "unknown"),
        next_charge=sub.get("next_charge_date", ""),
        org_id=data.get("org_id", ""),
        raw=data,
    )


# ── Legacy DOM parser (kept for backward compat) ─────────────────────────────

def parse_usage_texts(texts: list[str]) -> ClaudeSubscriptionUsage:
    """Parse paragraph texts from DOM scraping (legacy fallback).

    This is the original parser that extracted usage data by scraping visible
    text from the claude.ai usage page. It's kept for backward compatibility
    with older Playwright MCP setups that use ``browser_snapshot`` instead of
    ``browser_evaluate``. The API-based ``parse_api_response`` is preferred
    because it returns structured data without fragile DOM assumptions.

    Expects text blocks in the order they appear on the page:
    - ``"Current session"`` followed by a reset time and a percentage
    - ``"All models"`` followed by a reset time and a percentage
    - ``"Sonnet only"`` followed by a reset time and a percentage
    - ``"Monthly spend limit"`` preceded by a dollar amount
    - ``"Current balance"`` containing a dollar amount

    Args:
        texts: List of paragraph text strings scraped from the usage page DOM.

    Returns:
        A ``ClaudeSubscriptionUsage`` with ``raw["source"]`` set to ``"dom"``
        to distinguish it from API-sourced data.
    """
    import re

    def find_pct(s: str) -> float:
        """Extract the first percentage value from a string."""
        m = re.search(r"(\d+)%", s)
        return int(m.group(1)) / 100 if m else 0.0

    def find_dollar(s: str) -> float:
        """Extract the first dollar amount from a string."""
        m = re.search(r"\$([0-9,.]+)", s)
        return float(m.group(1).replace(",", "")) if m else 0.0

    session = weekly_all = weekly_sonnet = None
    spend_limit = 0.0
    current_balance = 0.0

    i = 0
    while i < len(texts):
        t = texts[i].strip()
        if t == "Current session" and i + 2 < len(texts):
            session = UsageLimit("Session", find_pct(texts[i + 2]), texts[i + 1].strip())
            i += 3
            continue
        if t == "All models" and i + 2 < len(texts):
            weekly_all = UsageLimit("Weekly (all)", find_pct(texts[i + 2]), texts[i + 1].strip())
            i += 3
            continue
        if t == "Sonnet only" and i + 2 < len(texts):
            weekly_sonnet = UsageLimit("Weekly (Sonnet)", find_pct(texts[i + 2]), texts[i + 1].strip())
            i += 3
            continue
        if t == "Monthly spend limit" and i > 0:
            spend_limit = find_dollar(texts[i - 1])
        if "Current balance" in t:
            current_balance = find_dollar(t)
        i += 1

    return ClaudeSubscriptionUsage(
        session=session, weekly_all=weekly_all, weekly_sonnet=weekly_sonnet,
        extra_usage_limit=spend_limit, prepaid_balance=current_balance,
        raw={"source": "dom", "texts": texts},
    )
