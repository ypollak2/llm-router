"""Provider health tracking with circuit breaker pattern.

Implements a dual-track health system: one track for hard failures (connection
errors, 5xx responses) and another for rate limits (429s). Rate limits use a
shorter cooldown because they typically clear faster than infrastructure issues.

Each provider gets its own ``ProviderHealth`` state, managed by a central
``HealthTracker`` singleton. The circuit breaker prevents routing to providers
that are likely to fail, reducing wasted latency and API costs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from llm_router.config import get_config


RATE_LIMIT_COOLDOWN_SECONDS = 15
"""Cooldown period (seconds) after a 429 rate-limit error before retrying.
Shorter than failure cooldown because rate limits typically resolve quickly
once the provider's token bucket refills."""


@dataclass
class ProviderHealth:
    """Health state for a single LLM provider, tracking two failure modes.

    The dual-track design separates rate limits from hard failures because they
    have different recovery characteristics: rate limits clear in seconds, while
    infrastructure failures may take minutes.

    Attributes:
        consecutive_failures: Number of hard failures in a row since the last
            success. Resets to 0 on any successful call.
        last_failure_time: Monotonic timestamp of the most recent hard failure,
            used to compute cooldown elapsed time.
        total_calls: Lifetime count of all calls (successes + failures + rate limits).
        total_failures: Lifetime count of hard failures (does not reset on success).
        rate_limited: Whether the provider is currently in rate-limit cooldown.
        rate_limit_time: Monotonic timestamp of the most recent rate-limit event.
        rate_limit_count: Lifetime count of rate-limit events.
    """

    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    total_calls: int = 0
    total_failures: int = 0
    rate_limited: bool = False
    rate_limit_time: float = 0.0
    rate_limit_count: int = 0

    def record_success(self) -> None:
        """Record a successful API call.

        Resets both the consecutive failure counter and the rate-limit flag,
        since a success proves the provider is operational.
        """
        self.consecutive_failures = 0
        self.rate_limited = False
        self.total_calls += 1

    def record_failure(self) -> None:
        """Record a hard failure (connection error, 5xx, timeout, etc.).

        Increments both the consecutive and lifetime failure counters and
        timestamps the event for cooldown tracking.
        """
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_calls += 1
        self.last_failure_time = time.monotonic()

    def record_rate_limit(self) -> None:
        """Record a 429/rate-limit error.

        Uses a separate, shorter cooldown than hard failures because rate
        limits typically resolve within seconds once the provider's token
        bucket refills.
        """
        self.rate_limited = True
        self.rate_limit_time = time.monotonic()
        self.rate_limit_count += 1
        self.total_calls += 1

    def is_healthy(self) -> bool:
        """Determine whether this provider should receive traffic.

        Checks are evaluated in priority order:
        1. **Rate limit**: If currently rate-limited and cooldown hasn't elapsed,
           return False. If cooldown has elapsed, clear the flag and continue.
        2. **Hard failures**: If consecutive failures exceed the configured
           threshold and cooldown hasn't elapsed, return False. If cooldown
           has elapsed, reset failures and allow a retry (half-open state).
        3. **Default**: Return True (healthy).

        Returns:
            True if the provider is considered healthy enough to try.
        """
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
            # Reset and allow retry (circuit breaker half-open → closed)
            self.consecutive_failures = 0
            return True
        return False

    @property
    def status(self) -> str:
        """Human-readable status string for display in health reports.

        Returns:
            One of:
            - ``"rate-limited (Xs remaining)"`` with seconds until cooldown ends
            - ``"healthy"``
            - ``"unhealthy (failures=N)"`` with the current consecutive count
        """
        if self.rate_limited:
            elapsed = time.monotonic() - self.rate_limit_time
            remaining = max(0, RATE_LIMIT_COOLDOWN_SECONDS - elapsed)
            return f"rate-limited ({remaining:.0f}s remaining)"
        if self.is_healthy():
            return "healthy"
        return f"unhealthy (failures={self.consecutive_failures})"


@dataclass
class HealthTracker:
    """Central registry of per-provider health states.

    Lazily creates a ``ProviderHealth`` instance for each provider on first
    access. All providers share the same tracker singleton (via ``get_tracker()``),
    so health state persists across routing calls within a process lifetime.

    Attributes:
        _providers: Internal dict mapping provider name to its ProviderHealth.
    """

    _providers: dict[str, ProviderHealth] = field(default_factory=dict)

    def _get(self, provider: str) -> ProviderHealth:
        """Get or create the health state for a provider.

        Args:
            provider: Provider name (e.g. "openai", "gemini", "anthropic").

        Returns:
            The ProviderHealth instance for this provider.
        """
        if provider not in self._providers:
            self._providers[provider] = ProviderHealth()
        return self._providers[provider]

    def is_healthy(self, provider: str) -> bool:
        """Check whether a provider is healthy enough to receive traffic.

        Args:
            provider: Provider name.

        Returns:
            True if the provider's circuit breaker is closed or half-open.
        """
        return self._get(provider).is_healthy()

    def record_success(self, provider: str) -> None:
        """Record a successful call to the given provider.

        Args:
            provider: Provider name.
        """
        self._get(provider).record_success()

    def record_failure(self, provider: str) -> None:
        """Record a hard failure for the given provider.

        Args:
            provider: Provider name.
        """
        self._get(provider).record_failure()

    def record_rate_limit(self, provider: str) -> None:
        """Record a rate-limit (429) error for the given provider.

        Args:
            provider: Provider name.
        """
        self._get(provider).record_rate_limit()

    def reset_stale(self, max_age_seconds: float = 1800.0) -> list[str]:
        """Reset circuit breakers for providers whose last failure is older than max_age_seconds.

        Prevents stale failures (e.g. from yesterday's outage) from blocking
        providers that are healthy again. Called at session start so each new
        Claude Code session begins with clean provider health state.

        Args:
            max_age_seconds: Age threshold in seconds (default 30 minutes).

        Returns:
            List of provider names that were reset.
        """
        now = time.monotonic()
        reset = []
        for provider, health in self._providers.items():
            if health.consecutive_failures > 0:
                age = now - health.last_failure_time
                if age >= max_age_seconds:
                    health.consecutive_failures = 0
                    reset.append(provider)
            if health.rate_limited:
                age = now - health.rate_limit_time
                if age >= max_age_seconds:
                    health.rate_limited = False
                    if provider not in reset:
                        reset.append(provider)
        return reset

    def status_report(self) -> dict[str, str]:
        """Generate a health report for all configured providers.

        Iterates over all providers listed in the router config (not just those
        that have been called), so the report always shows the full picture.

        Returns:
            Dict mapping provider name to a status string like
            ``"healthy | key configured | 42 calls"``.
        """
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
"""Lazily-initialized singleton instance. Use ``get_tracker()`` to access."""


def get_tracker() -> HealthTracker:
    """Get or create the global HealthTracker singleton.

    All routing code shares this instance so health state accumulates across
    calls within the same process.

    Returns:
        The shared HealthTracker instance.
    """
    global _tracker
    if _tracker is None:
        _tracker = HealthTracker()
    return _tracker
