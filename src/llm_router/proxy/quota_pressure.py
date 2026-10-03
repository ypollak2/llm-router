"""Claude subscription pressure for the proxy's tier decision (``proxy/tiers.py``).

The value is the higher of the session (5-hour) and weekly percentages in the
cached ``usage.json`` (``paths.state_path("usage.json")``) that the usage
refreshers already write: the statusline's background refresh (every
``LLM_ROUTER_USAGE_TTL_SEC``, default 300 s, while Claude Code is drawing), the
SessionStart hook and the ``llm_*`` PostToolUse hook. All of them write the
percentages as 0-100 plus a wall-clock ``updated_at``.

This module never makes a network call: it reads that one small local file and
nothing else, so the proxy hot path cannot wait on the OAuth endpoint. Anything
it cannot trust is reported, not guessed -- a missing or unreadable file, the
install-time ``pending`` placeholder, the refresh-failed ``is_fallback`` 50%
marker, or a reading older than ``max_age_s`` -- and the caller then leaves the
tier decision alone (fail open).

``LLM_ROUTER_PROXY_QUOTA_PRESSURE=off`` (or ``0``/``false``/``no``) is the kill
switch: the reading comes back ``off`` and the pressure step does nothing.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

STATE_OK = "ok"
STATE_UNKNOWN = "unknown"  # no file, unreadable, placeholder, fallback marker, no percentages
STATE_STALE = "stale"      # older than max_age_s
STATE_OFF = "off"          # kill switch

DEFAULT_MAX_AGE_S = 1800.0
_OFF_VALUES = ("0", "off", "false", "no")


@dataclass(frozen=True)
class QuotaReading:
    pressure: float | None  # 0.0-1.0, max(session, weekly); None unless measured
    state: str
    age_s: float | None = None


def disabled() -> bool:
    """True when the kill switch env var turns the pressure step off."""
    return (os.environ.get("LLM_ROUTER_PROXY_QUOTA_PRESSURE") or "").strip().lower() in _OFF_VALUES


def _pct(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value))) / 100.0


def read(path: str | Path | None = None, *, max_age_s: float = DEFAULT_MAX_AGE_S,
         now: float | None = None) -> QuotaReading:
    """The current pressure, or why there is none. Never raises."""
    if disabled():
        return QuotaReading(None, STATE_OFF)
    if path is None:
        from llm_router import paths

        path = paths.state_path("usage.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return QuotaReading(None, STATE_UNKNOWN)
    if not isinstance(data, dict) or data.get("pending") or data.get("is_fallback"):
        return QuotaReading(None, STATE_UNKNOWN)
    pcts = [p for p in (_pct(data.get("session_pct")), _pct(data.get("weekly_pct"))) if p is not None]
    updated = data.get("updated_at")
    if not pcts or isinstance(updated, bool) or not isinstance(updated, (int, float)) or updated <= 0:
        return QuotaReading(None, STATE_UNKNOWN)
    age = (time.time() if now is None else now) - float(updated)
    pressure = round(max(pcts), 4)
    if age > max_age_s:
        return QuotaReading(pressure, STATE_STALE, round(age, 1))
    return QuotaReading(pressure, STATE_OK, round(max(age, 0.0), 1))
