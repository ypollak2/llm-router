"""Tests for OAuth token rotation and refresh strategy."""

import asyncio
import json
import time
from base64 import urlsafe_b64encode

import pytest

from llm_router.oauth_token_rotation import (
    TokenRefreshStrategy,
    ExpiryTracker,
)


class TestTokenRefreshStrategy:
    """Test TokenRefreshStrategy for managing token lifecycle."""

    @pytest.mark.asyncio
    async def test_initial_state(self):
        """TokenRefreshStrategy refreshes on first call."""
        async def dummy_getter():
            return "token"

        async def dummy_refresher():
            return "new_token"

        strategy = TokenRefreshStrategy(dummy_getter, dummy_refresher)
        token = await strategy.get_token()
        # First call triggers refresh (time since last refresh is infinite)
        assert token == "new_token"

    @pytest.mark.asyncio
    async def test_refresh_on_interval(self):
        """Token is refreshed after refresh_interval seconds."""
        refresh_count = 0

        async def dummy_getter():
            return "token"

        async def dummy_refresher():
            nonlocal refresh_count
            refresh_count += 1
            return f"token_{refresh_count}"

        strategy = TokenRefreshStrategy(
            dummy_getter, dummy_refresher, refresh_interval=1
        )

        # First call triggers refresh (interval_elapsed = inf)
        token1 = await strategy.get_token()
        assert token1 == "token_1"
        assert refresh_count == 1

        # Second call within interval doesn't refresh
        token2 = await strategy.get_token()
        assert token2 == "token_1"
        assert refresh_count == 1

        # Wait for interval to elapse
        await asyncio.sleep(1.1)

        # Third call triggers refresh
        token3 = await strategy.get_token()
        assert token3 == "token_2"
        assert refresh_count == 2

    @pytest.mark.asyncio
    async def test_refresh_handles_exceptions(self):
        """Token refresh handles exceptions gracefully."""
        refresh_count = 0
        fail_once = True

        async def dummy_getter():
            return "token"

        async def failing_refresher():
            nonlocal refresh_count, fail_once
            refresh_count += 1
            if fail_once:
                fail_once = False
                raise ValueError("Refresh failed")
            return f"token_{refresh_count}"

        strategy = TokenRefreshStrategy(
            dummy_getter, failing_refresher, refresh_interval=1
        )

        # First refresh fails (exception caught)
        token1 = await strategy.get_token()
        assert token1 is None  # No token available
        assert refresh_count == 1

        # Wait for interval
        await asyncio.sleep(1.1)

        # Second refresh succeeds
        token2 = await strategy.get_token()
        assert token2 == "token_2"
        assert refresh_count == 2

    @pytest.mark.asyncio
    async def test_force_refresh(self):
        """force_refresh() immediately refreshes token."""
        refresh_count = 0

        async def dummy_getter():
            return "token"

        async def dummy_refresher():
            nonlocal refresh_count
            refresh_count += 1
            return f"token_{refresh_count}"

        strategy = TokenRefreshStrategy(
            dummy_getter, dummy_refresher, refresh_interval=3600
        )

        # Initial refresh
        token1 = await strategy.get_token()
        assert token1 == "token_1"

        # Force refresh even though interval hasn't elapsed
        success = await strategy.force_refresh()
        assert success is True
        token2 = await strategy.get_token()
        assert token2 == "token_2"

    @pytest.mark.asyncio
    async def test_force_refresh_handles_failures(self):
        """force_refresh() returns False on failure."""
        async def dummy_getter():
            return "token"

        async def failing_refresher():
            raise ValueError("Refresh failed")

        strategy = TokenRefreshStrategy(dummy_getter, failing_refresher)

        success = await strategy.force_refresh()
        assert success is False

    @pytest.mark.asyncio
    async def test_concurrent_refreshes(self):
        """Concurrent refresh calls don't trigger multiple refreshes."""
        refresh_count = 0

        async def dummy_getter():
            return "token"

        async def slow_refresher():
            nonlocal refresh_count
            refresh_count += 1
            await asyncio.sleep(0.1)  # Simulate slow refresh
            return f"token_{refresh_count}"

        strategy = TokenRefreshStrategy(
            dummy_getter, slow_refresher, refresh_interval=1
        )

        # Trigger multiple concurrent refreshes
        await asyncio.sleep(1.1)  # Ensure refresh is needed
        tasks = [strategy.get_token() for _ in range(5)]
        tokens = await asyncio.gather(*tasks)

        # All should return same token (refresh only happened once)
        assert all(t == tokens[0] for t in tokens)
        assert refresh_count == 1  # Only one refresh despite 5 concurrent calls

    def test_reset(self):
        """reset() clears token and refresh state."""
        async def dummy_getter():
            return "token"

        async def dummy_refresher():
            return "new_token"

        strategy = TokenRefreshStrategy(dummy_getter, dummy_refresher)
        strategy._current_token = "token_1"
        strategy._last_refresh_time = time.time()

        strategy.reset()
        assert strategy._current_token is None
        assert strategy._last_refresh_time == 0.0


class TestExpiryTracker:
    """Test ExpiryTracker for JWT token expiry detection."""

    @staticmethod
    def create_jwt(exp: int | None = None) -> str:
        """Create a minimal JWT for testing.

        Args:
            exp: Expiration timestamp (defaults to 1 hour from now)

        Returns:
            JWT string (header.payload.signature)
        """
        if exp is None:
            exp = int(time.time()) + 3600  # 1 hour from now

        header = {"alg": "HS256"}
        payload = {"exp": exp, "sub": "test"}

        def encode(obj):
            json_str = json.dumps(obj)
            return (
                urlsafe_b64encode(json_str.encode()).decode().rstrip("=")
            )

        return f"{encode(header)}.{encode(payload)}.signature"

    def test_decode_jwt_exp_valid(self):
        """decode_jwt_exp() extracts expiration from valid JWT."""
        future_exp = int(time.time()) + 3600
        token = self.create_jwt(exp=future_exp)

        exp = ExpiryTracker.decode_jwt_exp(token)
        assert exp == future_exp

    def test_decode_jwt_exp_invalid_format(self):
        """decode_jwt_exp() returns None for invalid JWT format."""
        # Invalid JWT (wrong number of parts)
        exp = ExpiryTracker.decode_jwt_exp("invalid.token")
        assert exp is None

        # Empty JWT
        exp = ExpiryTracker.decode_jwt_exp("")
        assert exp is None

    def test_decode_jwt_exp_invalid_json(self):
        """decode_jwt_exp() returns None for invalid JSON payload."""
        # JWT with invalid base64/JSON
        exp = ExpiryTracker.decode_jwt_exp("aaa.bbb.ccc")
        assert exp is None

    def test_is_token_expired_valid_token(self):
        """is_token_expired() returns False for valid tokens."""
        future_exp = int(time.time()) + 3600  # 1 hour from now
        token = self.create_jwt(exp=future_exp)

        is_expired = ExpiryTracker.is_token_expired(token, buffer_seconds=300)
        assert is_expired is False

    def test_is_token_expired_expired_token(self):
        """is_token_expired() returns True for expired tokens."""
        past_exp = int(time.time()) - 60  # 1 minute ago
        token = self.create_jwt(exp=past_exp)

        is_expired = ExpiryTracker.is_token_expired(token, buffer_seconds=0)
        assert is_expired is True

    def test_is_token_expired_with_buffer(self):
        """is_token_expired() uses buffer to detect upcoming expiry."""
        # Token expires in 2 minutes
        near_future_exp = int(time.time()) + 120
        token = self.create_jwt(exp=near_future_exp)

        # With small buffer (60s), token is NOT considered expired
        is_expired = ExpiryTracker.is_token_expired(token, buffer_seconds=60)
        assert is_expired is False

        # With large buffer (300s), token IS considered expired
        is_expired = ExpiryTracker.is_token_expired(token, buffer_seconds=300)
        assert is_expired is True

    def test_is_token_expired_invalid_token(self):
        """is_token_expired() returns True for invalid tokens (force refresh for safety)."""
        is_expired = ExpiryTracker.is_token_expired("invalid", buffer_seconds=0)
        assert is_expired is True  # Can't determine, force refresh (safety-first)

    def test_is_token_expired_no_exp_claim(self):
        """is_token_expired() returns True when JWT has no exp claim (force refresh for safety)."""
        # Create JWT without exp
        header = {"alg": "HS256"}
        payload = {"sub": "test"}  # No exp

        def encode(obj):
            json_str = json.dumps(obj)
            return (
                urlsafe_b64encode(json_str.encode()).decode().rstrip("=")
            )

        token = f"{encode(header)}.{encode(payload)}.signature"
        is_expired = ExpiryTracker.is_token_expired(token)
        assert is_expired is True  # No exp → force refresh (safety-first)
