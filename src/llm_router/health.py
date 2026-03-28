"""Provider health tracking with circuit breaker pattern."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from llm_router.config import get_config


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    total_calls: int = 0
    total_failures: int = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_calls += 1

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_calls += 1
        self.last_failure_time = time.monotonic()

    def is_healthy(self) -> bool:
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
