"""Secure OAuth token rotation and refresh strategy.

This module implements token refresh logic to prevent token compromise from
lasting indefinitely. OAuth tokens have a limited lifespan and should be
refreshed periodically.

Strategy:
- Tokens are refreshed every OAUTH_REFRESH_INTERVAL (default: 1 hour)
- Expired tokens are detected and refreshed on-demand
- Refresh is asynchronous to avoid blocking operations
- Failed refreshes don't block token usage (graceful degradation)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class OAuthTokenRefreshError(Exception):
    """Raised when OAuth token refresh fails."""
    pass


class TokenRefreshStrategy:
    """Manages OAuth token refresh with configurable intervals and error handling."""

    def __init__(
        self,
        token_getter: callable,
        token_refresher: callable,
        refresh_interval: int = 3600,  # 1 hour
        expiry_buffer: int = 300,  # Refresh 5 min before expiry
    ):
        """Initialize token refresh strategy.

        Args:
            token_getter: Async function to retrieve current token.
            token_refresher: Async function to refresh token (returns new token).
            refresh_interval: Seconds between forced refreshes (default: 3600).
            expiry_buffer: Seconds before actual expiry to refresh (default: 300).
        """
        self.token_getter = token_getter
        self.token_refresher = token_refresher
        self.refresh_interval = refresh_interval
        self.expiry_buffer = expiry_buffer

        self._last_refresh_time = 0.0
        self._current_token: Optional[str] = None
        self._refresh_lock = asyncio.Lock()

    async def get_token(self) -> str | None:
        """Get current token, refreshing if necessary.

        Returns:
            Current valid token, or None if refresh failed and no token available.
        """
        async with self._refresh_lock:
            now = time.time()
            # Check if refresh is needed (inside lock to prevent multiple concurrent refreshes)
            if now - self._last_refresh_time > self.refresh_interval:
                # Double-check after acquiring lock (another coroutine may have refreshed)
                now = time.time()
                if now - self._last_refresh_time > self.refresh_interval:
                    try:
                        new_token = await self.token_refresher()
                        if new_token:
                            self._current_token = new_token
                            self._last_refresh_time = now
                    except Exception as exc:
                        logger.warning("OAuth token refresh failed: %s", exc, exc_info=True)
            return self._current_token

    async def _refresh_token_internal(self) -> None:
        """Refresh token with locking to prevent concurrent refreshes."""
        async with self._refresh_lock:
            # Double-check after acquiring lock (another coroutine may have refreshed)
            now = time.time()
            if now - self._last_refresh_time <= self.refresh_interval:
                return

            try:
                # Attempt to get fresh token from source
                new_token = await self.token_refresher()
                if new_token:
                    self._current_token = new_token
                    self._last_refresh_time = now
                else:
                    # Refresh returned None — log but don't crash
                    pass
            except Exception as exc:
                # Refresh failed but we don't raise — graceful degradation
                # Log the error for debugging, but allow old token to be used
                logger.warning("OAuth token refresh failed in _refresh_token_internal: %s", exc, exc_info=True)

    async def force_refresh(self) -> bool:
        """Force immediate token refresh (e.g., after detecting expiry).

        Returns:
            True if refresh succeeded, False if it failed or token is None.
        """
        async with self._refresh_lock:
            try:
                new_token = await self.token_refresher()
                if new_token:
                    self._current_token = new_token
                    self._last_refresh_time = time.time()
                    return True
                return False
            except Exception:
                return False

    def reset(self) -> None:
        """Reset refresh state (for testing or logout scenarios)."""
        self._current_token = None
        self._last_refresh_time = 0.0


class ExpiryTracker:
    """Tracks OAuth token expiry and detects when a token should be refreshed.

    Typical OAuth tokens have an 'exp' claim in the JWT that indicates
    expiration time. This tracker helps detect expired tokens proactively.
    """

    @staticmethod
    def decode_jwt_exp(token: str) -> int | None:
        """Extract expiration timestamp from JWT token.

        OAuth tokens are often JWTs with an 'exp' field in the payload.
        This method decodes the JWT (without verification) to get expiry.

        Args:
            token: JWT token string (format: header.payload.signature)

        Returns:
            Unix timestamp of expiration, or None if token is invalid/expired/implausible.
        """
        try:
            # JWT format: header.payload.signature
            parts = token.split(".")
            if len(parts) != 3:
                return None

            # Decode payload (add padding if needed for base64)
            import base64
            import json

            payload_str = parts[1]
            # Add padding if necessary
            padding = 4 - (len(payload_str) % 4)
            if padding != 4:
                payload_str += "=" * padding

            payload_json = base64.urlsafe_b64decode(payload_str)
            payload = json.loads(payload_json)

            exp = payload.get("exp")
            if exp is None:
                return None
            
            # Sanity check: reject implausibly far-future expirations (>1 year)
            # This prevents attackers from setting exp to arbitrary values
            now = time.time()
            if exp > now + 365 * 86400:
                logger.warning("JWT has implausibly far-future expiration (%d > now+1yr); rejecting", exp)
                return None
            
            return exp  # Unix timestamp
        except Exception:
            return None

    @staticmethod
    def is_token_expired(token: str, buffer_seconds: int = 300) -> bool:
        """Check if token is expired (or expiring soon).

        Args:
            token: JWT token to check
            buffer_seconds: Consider token expired if expiry is within this many seconds

        Returns:
            True if token is expired or expiring soon, False otherwise.
            For undecodeable tokens, returns True to force refresh (safety-first).
        """
        exp = ExpiryTracker.decode_jwt_exp(token)
        if exp is None:
            # Can't determine expiry (invalid token, implausible exp, etc.)
            # Force refresh for safety rather than trusting the token indefinitely
            return True

        now = time.time()
        return now + buffer_seconds >= exp
