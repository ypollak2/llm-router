"""Tests for health tracking and circuit breaker."""

import time

from llm_router.health import HealthTracker, ProviderHealth


class TestProviderHealth:
    def test_starts_healthy(self):
        h = ProviderHealth()
        assert h.is_healthy()

    def test_stays_healthy_below_threshold(self):
        h = ProviderHealth()
        h.record_failure()
        h.record_failure()
        assert h.is_healthy()  # threshold is 3

    def test_unhealthy_after_threshold(self):
        h = ProviderHealth()
        for _ in range(3):
            h.record_failure()
        assert not h.is_healthy()

    def test_success_resets_failures(self):
        h = ProviderHealth()
        h.record_failure()
        h.record_failure()
        h.record_success()
        assert h.consecutive_failures == 0
        assert h.is_healthy()

    def test_recovers_after_cooldown(self, mock_env):
        h = ProviderHealth()
        for _ in range(3):
            h.record_failure()
        assert not h.is_healthy()

        # Simulate cooldown elapsed
        h.last_failure_time = time.monotonic() - 61
        assert h.is_healthy()
        assert h.consecutive_failures == 0  # reset on recovery

    def test_tracks_totals(self):
        h = ProviderHealth()
        h.record_success()
        h.record_success()
        h.record_failure()
        assert h.total_calls == 3
        assert h.total_failures == 1


class TestHealthTracker:
    def test_independent_providers(self):
        tracker = HealthTracker()
        for _ in range(3):
            tracker.record_failure("openai")
        tracker.record_success("gemini")

        assert not tracker.is_healthy("openai")
        assert tracker.is_healthy("gemini")

    def test_status_report(self, mock_env):
        tracker = HealthTracker()
        tracker.record_success("openai")
        report = tracker.status_report()
        assert "openai" in report
