"""Real-time session spend tracking.

Writes spend data to ~/.llm-router/session_spend.json after every routed
call. Uses a flat JSON file (not SQLite) so hook scripts can read it with
zero Python dependencies.

The file is reset at session start and updated atomically after each call.
Anomaly detection fires when session spend exceeds a threshold in under
10 minutes — a signal of runaway costs (e.g. accidentally routing a tight
loop to an expensive model).

**Known limitation — spend resets on MCP server restart.**
The in-memory accumulator (`_spend_singleton`) resets to $0.00 every time
the MCP server process restarts (e.g. Claude Code update, crash, or manual
restart). This means:
  - ``LLM_ROUTER_ESCALATE_ABOVE`` and ``LLM_ROUTER_HARD_STOP_ABOVE`` thresholds
    are per-process-lifetime, not per-session.
  - A user who restarts mid-session gets a fresh $0.00 baseline, allowing
    escalation thresholds to be crossed again.
There is no fix without adding a persistent SQLite read on every call.
Workaround: set thresholds conservatively, or use ``LLM_ROUTER_MONTHLY_BUDGET``
which reads from the persistent SQLite store and is not affected by restarts.

Usage:
    from llm_router.session_spend import get_session_spend
    get_session_spend().record(model="gpt-4o", tool="llm_code",
                                input_tokens=500, output_tokens=200)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

SESSION_SPEND_FILE = Path.home() / ".llm-router" / "session_spend.json"

# Default anomaly threshold: $0.50 in one session is unusual for most users.
# Override via LLM_ROUTER_ANOMALY_THRESHOLD env var.
_DEFAULT_ANOMALY_THRESHOLD_USD = 0.50

# Model cost table (per 1K output tokens, USD) — used for live estimation
# when the router hasn't logged the exact cost yet.
_COST_PER_1K_OUT: dict[str, float] = {
    "gpt-4o":                0.010,
    "gpt-4o-mini":           0.00060,
    "gpt-4.1":               0.008,
    "gpt-4.1-mini":          0.00040,
    "o3":                    0.060,
    "o3-mini":               0.004,
    "claude-opus-4-6":       0.075,
    "claude-sonnet-4-6":     0.015,
    "claude-haiku-4-5":      0.00125,
    "gemini-2.5-pro":        0.007,
    "gemini-2.5-flash":      0.00030,
    "gemini-2.0-flash":      0.00030,
    "gemini-1.5-pro":        0.007,
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Rough cost estimate based on output tokens (input is ~3x cheaper, often negligible)."""
    # Strip provider prefix (e.g. "openai/gpt-4o" → "gpt-4o")
    short = model.split("/", 1)[-1] if "/" in model else model
    rate = _COST_PER_1K_OUT.get(short, 0.01)  # conservative fallback
    return (output_tokens * rate + input_tokens * rate * 0.3) / 1000


@dataclass
class SessionSpend:
    """Tracks per-session LLM spend with anomaly detection."""

    total_usd: float = 0.0
    session_start: float = field(default_factory=time.time)
    call_count: int = 0
    anomaly_flag: bool = False
    per_model: dict[str, dict] = field(default_factory=dict)
    per_tool: dict[str, int] = field(default_factory=dict)

    def record(
        self,
        model: str,
        tool: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None = None,
    ) -> None:
        """Record one routed call. If cost_usd is unknown, it is estimated."""
        if cost_usd is None:
            cost_usd = _estimate_cost(model, input_tokens, output_tokens)

        self.total_usd += cost_usd
        self.call_count += 1

        # Per-model stats
        if model not in self.per_model:
            self.per_model[model] = {"calls": 0, "cost_usd": 0.0, "tokens": 0}
        self.per_model[model]["calls"] += 1
        self.per_model[model]["cost_usd"] += cost_usd
        self.per_model[model]["tokens"] += input_tokens + output_tokens

        # Per-tool call counts
        self.per_tool[tool] = self.per_tool.get(tool, 0) + 1

        # Anomaly check
        threshold = float(os.getenv("LLM_ROUTER_ANOMALY_THRESHOLD",
                                    str(_DEFAULT_ANOMALY_THRESHOLD_USD)))
        elapsed = time.time() - self.session_start
        if threshold > 0 and self.total_usd >= threshold and elapsed < 600:
            self.anomaly_flag = True

        self._persist()

    def get_summary(self) -> dict:
        """Return a JSON-serialisable summary dict."""
        top_model = (
            max(self.per_model, key=lambda m: self.per_model[m]["cost_usd"])
            if self.per_model else None
        )
        return {
            "total_usd": round(self.total_usd, 6),
            "call_count": self.call_count,
            "anomaly_flag": self.anomaly_flag,
            "session_start": self.session_start,
            "top_model": top_model,
            "per_model": self.per_model,
            "per_tool": self.per_tool,
        }

    def _persist(self) -> None:
        """Write spend data to disk atomically."""
        try:
            SESSION_SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SESSION_SPEND_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.get_summary(), indent=2))
            tmp.replace(SESSION_SPEND_FILE)
        except OSError:
            pass  # Never crash the router due to disk issues

    def reset(self) -> None:
        """Reset for a new session."""
        self.total_usd = 0.0
        self.session_start = time.time()
        self.call_count = 0
        self.anomaly_flag = False
        self.per_model = {}
        self.per_tool = {}
        self._persist()

    @classmethod
    def load(cls) -> "SessionSpend":
        """Load existing session spend from disk, or return a fresh instance."""
        try:
            data = json.loads(SESSION_SPEND_FILE.read_text())
            obj = cls()
            obj.total_usd = float(data.get("total_usd", 0.0))
            obj.session_start = float(data.get("session_start", time.time()))
            obj.call_count = int(data.get("call_count", 0))
            obj.anomaly_flag = bool(data.get("anomaly_flag", False))
            obj.per_model = data.get("per_model", {})
            obj.per_tool = data.get("per_tool", {})
            return obj
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return cls()


# Module-level singleton
_spend: SessionSpend | None = None


def get_session_spend() -> SessionSpend:
    """Return the singleton SessionSpend instance, loading from disk on first call."""
    global _spend
    if _spend is None:
        _spend = SessionSpend.load()
    return _spend


def reset_session_spend() -> None:
    """Reset spend tracking for a new session (called by session-start hook)."""
    global _spend
    _spend = SessionSpend()
    _spend._persist()
