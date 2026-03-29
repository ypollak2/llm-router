"""Provider health tracking with circuit breaker pattern."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from llm_router.config import get_config


RATE_LIMIT_COOLDOWN_SECONDS = 15  # shorter cooldown for rate limits vs hard failures


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    rate_limited: bool = False
    rate_limit_time: float = 0.0
    rate_limit_count: int = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.rate_limited = False
        self.total_calls += 1

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_calls += 1
        self.last_failure_time = time.monotonic()

    def record_rate_limit(self) -> None:
        """Record a 429/rate-limit error — uses shorter cooldown than hard failures."""
        self.rate_limited = True
        self.rate_limit_time = time.monotonic()
        self.rate_limit_count += 1
        self.total_calls += 1

    def is_healthy(self) -> bool:
        # Check rate limit first (shorter cooldown)
        if self.rate_limited:
            elapsed = time.monotonic() - self.rate_limit_time
            if elapsed < RATE_LIMIT_COOLDOWN_SECONDS:
                return False
            self.rate_limited = False

        config = get_config()
        if self.consecutive_failures < config.health_failure_threshold:
            return True
        # Check if cooldown has elapsed
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed >= config.health_cooldown_seconds:
            # Reset and allow retry
            self.consecutive_failures = 0
            return True
        return False

    @property
    def status(self) -> str:
        if self.rate_limited:
            elapsed = time.monotonic() - self.rate_limit_time
            remaining = max(0, RATE_LIMIT_COOLDOWN_SECONDS - elapsed)
            return f"rate-limited ({remaining:.0f}s remaining)"
        if self.is_healthy():
            return "healthy"
        return f"unhealthy (failures={self.consecutive_failures})"


@dataclass
class HealthTracker:
    _providers: dict[str, ProviderHealth] = field(default_factory=dict)

    def _get(self, provider: str) -> ProviderHealth:
        if provider not in self._providers:
            self._providers[provider] = ProviderHealth()
        return self._providers[provider]

    def is_healthy(self, provider: str) -> bool:
        return self._get(provider).is_healthy()

    def record_success(self, provider: str) -> None:
        self._get(provider).record_success()

    def record_failure(self, provider: str) -> None:
        self._get(provider).record_failure()

    def record_rate_limit(self, provider: str) -> None:
        self._get(provider).record_rate_limit()

    def status_report(self) -> dict[str, str]:
        config = get_config()
        available = config.available_providers
        report = {}
        for provider in sorted(available):
            health = self._get(provider)
            has_key = "key configured" if provider in available else "no key"
            report[provider] = f"{health.status} | {has_key} | {health.total_calls} calls"
        return report


# Module-level singleton
_tracker: HealthTracker | None = None


def get_tracker() -> HealthTracker:
    global _tracker
    if _tracker is None:
        _tracker = HealthTracker()
    return _tracker
