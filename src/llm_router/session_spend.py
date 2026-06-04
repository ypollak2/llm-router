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

# Conservative fallback when the model is unknown to the calibration pricing
# table. The router writes cost_usd on every real call, so this only fires for
# providers we haven't priced yet — keeping it high biases the unknown case
# toward over- (not under-) estimation, which is the safer side for an
# anomaly-detection signal.
_UNKNOWN_MODEL_FALLBACK_USD = 0.01


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost for a (model, input_tokens, output_tokens) tuple.

    Delegates to :func:`llm_router.calibration.cost_for_tokens` so the
    pricing table lives in exactly one place. Plan 07 Cat F deferred site:
    eliminates the duplicate per-model rate dict that lived here, which
    silently drifted from the calibration table over time.

    Returns the unknown-model fallback when calibration prices the model at
    zero (free providers genuinely cost zero; unknown providers return zero
    because the table has no entry). Disambiguating those two cases without
    a sentinel is impossible, so the fallback only fires when output cost is
    zero AND the model isn't one of the known free providers.
    """
    from llm_router.calibration import cost_for_tokens

    cost = cost_for_tokens(model, input_tokens, output_tokens)
    if cost == 0 and not any(model.startswith(p) for p in ("ollama", "codex", "gemini_cli")):
        # Unknown model and not a recognised free provider — bias high so
        # anomaly detection still has something to chew on.
        return output_tokens * _UNKNOWN_MODEL_FALLBACK_USD / 1000
    return cost


@dataclass
class SessionSpend:
    """Tracks per-session LLM spend with anomaly detection and savings.

    v8.8.0: Added reclaimed tokens tracking — tokens that would have been
    consumed by Opus but were handled by cheaper models instead.
    """

    total_usd: float = 0.0
    session_start: float = field(default_factory=time.time)
    call_count: int = 0
    anomaly_flag: bool = False
    per_model: dict[str, dict] = field(default_factory=dict)
    per_tool: dict[str, int] = field(default_factory=dict)
    # v8.8.0: Token reclamation tracking
    tokens_reclaimed: int = 0
    opus_equivalent_usd: float = 0.0
    gates_passed: int = 0
    gates_failed: int = 0

    def record(
        self,
        model: str,
        tool: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None = None,
    ) -> None:
        """Record one routed call. If cost_usd is unknown, it is estimated."""
        # Stub-detection guard: mirrors cost.log_usage. Reject the exact
        # synthetic shapes used in test LLMResponse fixtures so unisolated
        # tests can never pollute ~/.llm-router/session_spend.json.
        if (
            os.environ.get("LLM_ROUTER_ALLOW_STUBS") != "1"
            and input_tokens == 100
            and output_tokens in (50, 100)
            and cost_usd in (0.001, 0.003)
        ):
            return

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

    def record_reclaimed(
        self,
        tokens_reclaimed: int,
        opus_equivalent_usd: float,
        gates_passed: bool,
    ) -> None:
        """Record tokens reclaimed by routing to a cheaper model.

        Args:
            tokens_reclaimed: Tokens that Opus would have consumed.
            opus_equivalent_usd: What Opus would have charged for this call.
            gates_passed: Whether verification gates passed on this call.
        """
        self.tokens_reclaimed += tokens_reclaimed
        self.opus_equivalent_usd += opus_equivalent_usd
        if gates_passed:
            self.gates_passed += 1
        else:
            self.gates_failed += 1
        self._persist()
        # Also persist a SQLite row so the session-end dashboard's cumulative
        # "today/this week/lifetime" savings rollup reflects subscription-funded
        # routing (Claude Code Haiku/Sonnet vs Opus). Without this, only the
        # current-session "Net preserved" panel sees these savings — they vanish
        # the moment the session ends. The dashboard query joins this table
        # via _query_cumulative_savings to surface them.
        try:
            self._persist_to_claude_usage(tokens_reclaimed, opus_equivalent_usd)
        except Exception:
            pass  # Tracking is best-effort — never crash the router.

    def _persist_to_claude_usage(
        self, tokens_reclaimed: int, opus_equivalent_usd: float
    ) -> None:
        """Append a row to ~/.llm-router/usage.db claude_usage table."""
        import sqlite3
        db_path = SESSION_SPEND_FILE.parent / "usage.db"
        if not db_path.exists():
            return  # No DB → no cumulative tracking yet; cost.py creates on first use.
        # Pick the model that took most cost this session as the attribution model
        # (rough but cheap — the alternative is per-call attribution which would
        # require threading the model name through record_reclaimed).
        attribution_model = (
            max(self.per_model, key=lambda m: self.per_model[m]["cost_usd"])
            if self.per_model else "subscription"
        )
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            conn.execute(
                "INSERT INTO claude_usage "
                "(model, tokens_used, complexity, cost_saved_usd) "
                "VALUES (?, ?, ?, ?)",
                (attribution_model, tokens_reclaimed, "auto", opus_equivalent_usd),
            )
            conn.commit()

    @property
    def net_savings_usd(self) -> float:
        """Real money preserved: what Opus would have cost minus actual spend."""
        return max(0.0, self.opus_equivalent_usd - self.total_usd)

    @property
    def extension_minutes(self) -> float:
        """Estimated minutes of extra work the savings bought.

        Based on average token consumption rate this session.
        """
        elapsed = max(1.0, time.time() - self.session_start)
        elapsed_min = elapsed / 60.0
        if self.call_count == 0 or elapsed_min < 0.5:
            return 0.0
        # Average total tokens consumed per minute across all routed calls
        total_tokens = sum(m.get("tokens", 0) for m in self.per_model.values())
        tokens_per_min = total_tokens / elapsed_min if elapsed_min > 0 else 0
        if tokens_per_min == 0:
            return 0.0
        return self.tokens_reclaimed / tokens_per_min

    @property
    def gate_pass_rate(self) -> float:
        """Percentage of routed calls that passed all verification gates."""
        total = self.gates_passed + self.gates_failed
        return (self.gates_passed / total * 100) if total > 0 else 100.0

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
            # v8.8.0: Real savings data
            "tokens_reclaimed": self.tokens_reclaimed,
            "opus_equivalent_usd": round(self.opus_equivalent_usd, 6),
            "net_savings_usd": round(self.net_savings_usd, 6),
            "extension_minutes": round(self.extension_minutes, 1),
            "gate_pass_rate": round(self.gate_pass_rate, 1),
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
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
        self.tokens_reclaimed = 0
        self.opus_equivalent_usd = 0.0
        self.gates_passed = 0
        self.gates_failed = 0
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
            # v8.8.0 fields — gracefully handle missing keys from older data
            obj.tokens_reclaimed = int(data.get("tokens_reclaimed", 0))
            obj.opus_equivalent_usd = float(data.get("opus_equivalent_usd", 0.0))
            obj.gates_passed = int(data.get("gates_passed", 0))
            obj.gates_failed = int(data.get("gates_failed", 0))
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
