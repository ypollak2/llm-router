"""Tests for Claude subscription usage parsing."""

from __future__ import annotations

from llm_router.claude_usage import parse_api_response, parse_usage_texts


# Real API response from claude.ai (captured via Playwright browser_evaluate)
REAL_API_DATA = {
    "org_id": "7b339253-a40b-407c-817b-8cc0bb163ddf",
    "usage": {
        "five_hour": {
            "utilization": 17,
            "resets_at": "2026-03-29T20:00:00.917146+00:00",
        },
        "seven_day": {
            "utilization": 21,
            "resets_at": "2026-04-03T11:59:59.917163+00:00",
        },
        "seven_day_sonnet": {
            "utilization": 26,
            "resets_at": "2026-04-01T09:00:00.917171+00:00",
        },
        "seven_day_opus": None,
        "seven_day_oauth_apps": None,
        "seven_day_cowork": None,
        "extra_usage": {
            "is_enabled": False,
            "monthly_limit": None,
            "used_credits": None,
            "utilization": None,
        },
    },
    "subscription": {
        "status": "active",
        "next_charge_date": "2026-04-09",
        "billing_interval": "monthly",
        "currency": "GBP",
    },
    "overage": {
        "is_enabled": False,
        "monthly_credit_limit": 5000,
        "used_credits": 2005,
    },
    "credits": {"amount": 0, "currency": "USD"},
}


class TestParseApiResponse:
    def test_session_parsed(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.session is not None
        assert usage.session.pct_used == 0.17
        assert "2026-03-29" in usage.session.resets_at

    def test_weekly_all_parsed(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.weekly_all is not None
        assert usage.weekly_all.pct_used == 0.21

    def test_weekly_sonnet_parsed(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.weekly_sonnet is not None
        assert usage.weekly_sonnet.pct_used == 0.26

    def test_weekly_opus_null(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.weekly_opus is None

    def test_overage_spend(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.extra_usage_spent == 20.05
        assert usage.extra_usage_limit == 50.0

    def test_plan_status(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.plan_status == "active"
        assert usage.next_charge == "2026-04-09"

    def test_session_pct_property(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.session_pct == 0.17

    def test_weekly_pct_property(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.weekly_pct == 0.21

    def test_sonnet_pct_property(self):
        usage = parse_api_response(REAL_API_DATA)
        assert usage.sonnet_pct == 0.26

    def test_highest_pressure(self):
        usage = parse_api_response(REAL_API_DATA)
        # Sonnet at 26% is the highest
        assert usage.highest_pressure == 0.26

    def test_summary_contains_sections(self):
        usage = parse_api_response(REAL_API_DATA)
        s = usage.summary()
        assert "Session" in s
        assert "Weekly" in s
        assert "Sonnet" in s
        assert "35%" in s or "OK" in s  # pressure indicator

    def test_error_response(self):
        usage = parse_api_response({"error": "Not logged in"})
        assert usage.session is None
        assert usage.session_pct == 0.0

    def test_empty_response(self):
        usage = parse_api_response({})
        assert usage.session is None
        assert usage.highest_pressure == 0.0

    def test_high_pressure_warning(self):
        data = {**REAL_API_DATA, "usage": {
            "five_hour": {"utilization": 92, "resets_at": "2026-03-29T20:00:00+00:00"},
            "seven_day": {"utilization": 85, "resets_at": "2026-04-03T12:00:00+00:00"},
            "seven_day_sonnet": None,
        }}
        usage = parse_api_response(data)
        assert usage.highest_pressure == 0.92
        summary = usage.summary().lower()
        assert "pressure" in summary or "!!" in summary or "downshift" in summary


class TestLegacyDomParser:
    """Backward compatibility with DOM text parsing."""

    REAL_TEXTS = [
        "Current session", "Resets in 4 hr 19 min", "13% used",
        "Learn more about usage limits",
        "All models", "Resets Fri 1:00 PM", "21% used",
        "Sonnet only", "Resets Wed 10:00 AM", "26% used",
        "Last updated: 2 minutes ago",
        "Turn on extra usage to keep using Claude if you hit a limit. Learn more",
        "$20.05 spent", "Resets Apr 1", "40% used",
        "$50", "Monthly spend limit",
        "$0.00", "Current balance·Auto-reload off",
    ]

    def test_session_parsed(self):
        usage = parse_usage_texts(self.REAL_TEXTS)
        assert usage.session is not None
        assert usage.session.pct_used == 0.13

    def test_weekly_all_parsed(self):
        usage = parse_usage_texts(self.REAL_TEXTS)
        assert usage.weekly_all is not None
        assert usage.weekly_all.pct_used == 0.21

    def test_weekly_sonnet_parsed(self):
        usage = parse_usage_texts(self.REAL_TEXTS)
        assert usage.weekly_sonnet is not None
        assert usage.weekly_sonnet.pct_used == 0.26

    def test_empty_texts(self):
        usage = parse_usage_texts([])
        assert usage.session is None
        assert usage.session_pct == 0.0


class TestEffectivePressure:
    """Tests for time-aware budget pressure."""

    def test_low_pressure_unchanged(self):
        """Under 85%, effective = raw regardless of time."""
        from llm_router.claude_usage import ClaudeSubscriptionUsage, UsageLimit
        usage = ClaudeSubscriptionUsage(
            session=UsageLimit("Session", 0.60, "2026-03-29T20:00:00+00:00"),
        )
        assert usage.effective_pressure == usage.highest_pressure

    def test_high_pressure_reset_imminent(self):
        """At 90% but reset in 5 min → pressure should be reduced."""
        from datetime import datetime, timedelta, timezone
        from llm_router.claude_usage import ClaudeSubscriptionUsage, UsageLimit
        soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        usage = ClaudeSubscriptionUsage(
            session=UsageLimit("Session", 0.90, soon),
        )
        # 5 min left → time_factor = 0.5 + (5/30)*0.5 = 0.583
        # effective = 0.90 * 0.583 ≈ 0.525
        assert usage.effective_pressure < 0.85
        assert usage.effective_pressure < usage.highest_pressure

    def test_high_pressure_reset_far(self):
        """At 90% with 3 hours left → full pressure."""
        from datetime import datetime, timedelta, timezone
        from llm_router.claude_usage import ClaudeSubscriptionUsage, UsageLimit
        far = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        usage = ClaudeSubscriptionUsage(
            session=UsageLimit("Session", 0.90, far),
        )
        assert usage.effective_pressure == 0.90

    def test_pressure_at_reset_boundary(self):
        """At 85% with exactly 30 min left → full pressure (boundary)."""
        from datetime import datetime, timedelta, timezone
        from llm_router.claude_usage import ClaudeSubscriptionUsage, UsageLimit
        boundary = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        usage = ClaudeSubscriptionUsage(
            session=UsageLimit("Session", 0.85, boundary),
        )
        # 30 min → time_factor = 0.5 + (30/30)*0.5 = 1.0
        assert abs(usage.effective_pressure - 0.85) < 0.02

    def test_no_session_data(self):
        """No session data → effective = raw."""
        from llm_router.claude_usage import ClaudeSubscriptionUsage
        usage = ClaudeSubscriptionUsage()
        assert usage.effective_pressure == 0.0

    def test_weekly_higher_than_session(self):
        """Weekly pressure dominates even with session time reduction."""
        from datetime import datetime, timedelta, timezone
        from llm_router.claude_usage import ClaudeSubscriptionUsage, UsageLimit
        soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        far = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        usage = ClaudeSubscriptionUsage(
            session=UsageLimit("Session", 0.90, soon),
            weekly_all=UsageLimit("Weekly", 0.95, far),
        )
        # highest_pressure is 0.95 (weekly), session reset is near
        # but effective_pressure uses session's minutes_until_reset
        # Since raw is 0.95 (from weekly) and session reset is soon,
        # effective still factors in session proximity
        assert usage.effective_pressure < 0.95
