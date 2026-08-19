"""Real-time session spend tracking.

Writes spend data to ~/.llm-router/session_spend.json after every routed
call. Uses a flat JSON file (not SQLite) so hook scripts can read it with
zero Python dependencies.

The file is reset at session start and updated atomically after each call.
Anomaly detection fires when session spend exceeds a threshold in under
10 minutes — a signal of runaway costs (e.g. accidentally routing a tight
loop to an expensive model).

**Cross-process persistence.**
Spend survives an MCP-server restart: ``get_session_spend()`` reloads the JSON
on first access, so a restarted router process continues the session's running
total instead of starting from $0.

The session-start hook resets ``session_spend.json`` directly (it is a standalone
script and cannot touch the router's in-memory singleton). A long-lived router —
one that stays up across a new Claude Code session — must notice that reset, or
its stale singleton would keep accumulating on the old baseline and re-persist it,
silently clobbering the reset. So ``get_session_spend()`` reloads whenever the
on-disk ``session_start`` is newer than the in-memory one. Only the session-start
reset writes a newer ``session_start``, so this never fires spuriously mid-session.
Per-session thresholds (``LLM_ROUTER_ANOMALY_THRESHOLD``, paid caps) therefore start
clean each session even when the MCP server process is reused.

Usage:
    from llm_router.session_spend import get_session_spend
    get_session_spend().record(model="gpt-4o", tool="llm_code",
                                input_tokens=500, output_tokens=200)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

SESSION_SPEND_FILE = Path.home() / ".llm-router" / "session_spend.json"

# Durable, cross-session routing-outcome ledger. session_spend.json is reset each
# session, so the deduped override/routed counts it holds vanish — leaving the
# "drift back to base models" signal (routable turns the main model handled
# ITSELF instead of routing) unobservable over time. This SQLite table keeps one
# always-current row per session (INSERT OR REPLACE from _persist), so the audit
# agent can trend base-model drift across sessions/days. (G-METRIC-1.)
#
# Derived from SESSION_SPEND_FILE.parent (not a fixed path) so a test that
# isolates SESSION_SPEND_FILE isolates this DB too — same pattern as usage.db.
def _routing_outcomes_db() -> Path:
    return SESSION_SPEND_FILE.parent / "routing_outcomes.db"


def _current_session_key() -> str:
    """Stable per-session key for the durable outcomes row."""
    import os

    return os.environ.get("CLAUDE_SESSION_ID", "").strip() or "unknown-session"


def _current_llm_router_version() -> str:
    """Running llm_router version — stamped on each durable row so base-drift can be
    scoped per release (behaviour carried from an older version must not
    contaminate the current version's trend)."""
    try:
        from llm_router import __version__

        return str(__version__)
    except Exception:
        return "0.0.0+unknown"


def read_base_drift(
    period: str = "all", *, version: str | None = None, db_path: Path | None = None
) -> dict:
    """Aggregate the durable outcomes ledger into a base-model-drift signal.

    This is what makes "drift back to base subscription models" measurable over
    time (G-METRIC-1): ``base_drift_share`` = the fraction of routed turns the
    main model handled ITSELF (override) instead of using the cheap routed
    answer. A rising share across cycles = drift back to base.

    Args:
        period: "today", "week", "month", or "all".
        version: when set, count only sessions recorded by that llm-routing
            version. Pass ``"current"`` to resolve to the running version. This
            scopes the drift signal to a single release so behaviour carried over
            from older versions doesn't contaminate the trend (the durable series
            spans upgrades). ``None`` (default) aggregates across every version.
            Rows written before this column existed carry a NULL version and are
            therefore excluded from any version-filtered read.
        db_path: outcomes DB (overridable for tests).

    Returns dict with: sessions, routed_turns, overridden_turns, base_drift_share,
    capture_rate, potential_savings_usd, realized_savings_usd, version. All zeros
    when the ledger is empty (no data yet — the series accrues from this fix
    forward), or when no session matches the requested version.
    """
    db_path = db_path or _routing_outcomes_db()
    if version == "current":
        version = _current_llm_router_version()
    cutoff = {
        "today": time.time() - 86400,
        "week": time.time() - 7 * 86400,
        "month": time.time() - 30 * 86400,
        "all": 0.0,
    }.get(period, 0.0)

    empty = {
        "sessions": 0, "routed_turns": 0, "overridden_turns": 0,
        "base_drift_share": 0.0, "capture_rate": 1.0,
        "potential_savings_usd": 0.0, "realized_savings_usd": 0.0,
        "version": version,
    }
    try:
        if not Path(db_path).exists():
            return empty
        where = "updated_at >= ?"
        params: list = [cutoff]
        if version is not None:
            where += " AND llm_router_version = ?"
            params.append(version)
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(call_count),0), "
                "COALESCE(SUM(overridden_turns),0), "
                "COALESCE(SUM(potential_savings_usd),0), "
                "COALESCE(SUM(realized_savings_usd),0) "
                f"FROM session_outcomes WHERE {where}",
                params,
            ).fetchone()
    except Exception:
        return empty

    sessions, routed, overridden, potential, realized = (
        int(row[0]), int(row[1]), int(row[2]), float(row[3]), float(row[4])
    )
    share = round(overridden / routed, 4) if routed > 0 else 0.0
    return {
        "sessions": sessions, "routed_turns": routed,
        "overridden_turns": overridden,
        "base_drift_share": share, "capture_rate": round(1.0 - share, 4),
        "potential_savings_usd": round(potential, 6),
        "realized_savings_usd": round(realized, 6),
        "version": version,
    }

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
    # Round-tripped so DIRECT-path persistence (which goes through this class)
    # does not drop the prompt_sequence counter the auto-route hook maintains.
    prompt_sequence: int = 0
    # Honest-savings split: a routed turn's savings are only REALIZED if the main
    # model actually used the routed answer. When the enforce hook sees the model
    # do the work itself (a routing violation), it marks the turn overridden —
    # those savings are POTENTIAL, not realized. Deduped per prompt_sequence so
    # multiple blocked tool-calls in one turn count as a single override.
    overridden_turns: int = 0
    last_overridden_seq: int = -1

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
        """Opus-baseline avoided: what Opus would have cost minus actual spend.

        NOTE: this is the *baseline-avoided* figure, NOT dollars the user would
        actually have paid. On a flat-rate Claude Code subscription the marginal
        cost of the host (Opus) call is ~$0, so this over-states real dollars.
        Use ``real_dollars_avoided_usd`` for the honest cash figure. Kept under
        this name for back-compat; ``baseline_avoided_usd`` is the clearer alias.
        See RETROSPECTIVE B-7.
        """
        return max(0.0, self.opus_equivalent_usd - self.total_usd)

    @property
    def baseline_avoided_usd(self) -> float:
        """Clear alias for ``net_savings_usd`` — the Opus-baseline-avoided figure."""
        return self.net_savings_usd

    @property
    def real_dollars_avoided_usd(self) -> float:
        """Dollars the user would ACTUALLY have paid absent routing.

        ~$0 on a flat-rate subscription (the host call is marginal-$0 until the
        quota cap); equals the baseline-avoided figure only in metered API mode,
        where the host Opus call really would have been billed. The
        subscription-vs-metered distinction comes from ``LLM_ROUTER_CLAUDE_SUBSCRIPTION``
        (see ``cost._host_is_metered``). This is the honest counterfactual for a
        subscription user; over-the-cap overage is modelled by the counterfactual
        simulator (bench/experiments), not attributable per-row here.
        RETROSPECTIVE B-7 / M-2.
        """
        try:
            from llm_router.cost import _host_is_metered

            metered = _host_is_metered()
        except Exception:
            metered = False
        return self.net_savings_usd if metered else 0.0

    @property
    def potential_savings_usd(self) -> float:
        """Counterfactual savings from routing — what a baseline model would have
        cost minus actual spend. This is what the routed turns *could* have saved;
        it is only fully realized if the main model used each routed answer."""
        return self.net_savings_usd

    @property
    def realized_savings_usd(self) -> float:
        """Savings actually preserved: potential minus the share attributed to
        turns the main model overrode (did the work itself, so both models ran).
        Prorated evenly across routed turns — conservative and clearly labelled."""
        if self.call_count <= 0:
            return 0.0
        kept = max(0, self.call_count - self.overridden_turns)
        return round(self.potential_savings_usd * kept / self.call_count, 6)

    def mark_overridden(self, prompt_sequence: int) -> None:
        """Flag the current routed turn as overridden by the main model.

        Called by the enforce hook when it observes a routing violation. Deduped
        on prompt_sequence so several blocked tool-calls in one turn count once.
        """
        if prompt_sequence == self.last_overridden_seq:
            return
        self.last_overridden_seq = prompt_sequence
        self.overridden_turns += 1
        self._persist()

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
            # RETROSPECTIVE B-7: two clearly-labelled figures, never conflated.
            "baseline_avoided_usd": round(self.baseline_avoided_usd, 6),
            "real_dollars_avoided_usd": round(self.real_dollars_avoided_usd, 6),
            "extension_minutes": round(self.extension_minutes, 1),
            "gate_pass_rate": round(self.gate_pass_rate, 1),
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
            "prompt_sequence": self.prompt_sequence,
            # Honest-savings split (potential = counterfactual, realized = used)
            "potential_savings_usd": round(self.potential_savings_usd, 6),
            "realized_savings_usd": self.realized_savings_usd,
            "overridden_turns": self.overridden_turns,
            "last_overridden_seq": self.last_overridden_seq,
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
        self._upsert_durable_outcome()

    def _upsert_durable_outcome(self) -> None:
        """Keep one always-current row per session in the durable outcomes DB.

        Fail-safe: any error here must never disturb spend tracking. Stores the
        deduped override/routed counts so base-model drift is queryable across
        sessions (G-METRIC-1) — INSERT OR REPLACE keeps it to one row/session.
        """
        try:
            # A durable per-session drift row is only meaningful with a real
            # session id (production always sets CLAUDE_SESSION_ID; test
            # subprocesses running hooks do not). Skipping the unresolvable case
            # is both semantically correct AND keeps unisolated tests / hook
            # subprocesses from polluting the real ~/.llm-router ledger.
            if _current_session_key() == "unknown-session":
                return
            db = _routing_outcomes_db()
            db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db), timeout=2.0) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS session_outcomes ("
                    "session_key TEXT PRIMARY KEY, updated_at REAL, "
                    "call_count INTEGER, overridden_turns INTEGER, "
                    "potential_savings_usd REAL, realized_savings_usd REAL, "
                    "llm_router_version TEXT)"
                )
                # Migrate pre-versioned ledgers (G-METRIC-1 shipped without the
                # column). Existing rows keep llm_router_version NULL, so a
                # current-version query simply excludes them from the trend.
                try:
                    conn.execute(
                        "ALTER TABLE session_outcomes ADD COLUMN llm_router_version TEXT"
                    )
                except sqlite3.OperationalError:
                    pass  # column already present
                conn.execute(
                    "INSERT OR REPLACE INTO session_outcomes "
                    "(session_key, updated_at, call_count, overridden_turns, "
                    "potential_savings_usd, realized_savings_usd, llm_router_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _current_session_key(), time.time(), self.call_count,
                        self.overridden_turns, round(self.potential_savings_usd, 6),
                        self.realized_savings_usd, _current_llm_router_version(),
                    ),
                )
                conn.commit()
        except Exception:
            pass  # Durable-outcome logging is best-effort; never break spend.

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
        self.prompt_sequence = 0
        self.overridden_turns = 0
        self.last_overridden_seq = -1
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
            obj.prompt_sequence = int(data.get("prompt_sequence", 0))
            obj.overridden_turns = int(data.get("overridden_turns", 0))
            obj.last_overridden_seq = int(data.get("last_overridden_seq", -1))
            return obj
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return cls()


# Module-level singleton
_spend: SessionSpend | None = None


def get_session_spend() -> SessionSpend:
    """Return the singleton SessionSpend instance, loading from disk on first call.

    Re-syncs if the session-start hook reset ``session_spend.json`` for a new
    session (detected by a newer on-disk ``session_start``) — otherwise a
    long-lived router process would keep accumulating on the old session's
    baseline and re-persist it, clobbering the reset. See the module docstring.
    """
    global _spend
    if _spend is None:
        _spend = SessionSpend.load()
        return _spend
    try:
        disk_start = float(json.loads(SESSION_SPEND_FILE.read_text()).get("session_start", 0.0))
        # 1s guard against float jitter / same-session re-persists.
        if disk_start > _spend.session_start + 1.0:
            _spend = SessionSpend.load()
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        pass  # Never let a spend-file read error break routing.
    return _spend


def reset_session_spend() -> None:
    """Reset spend tracking for a new session (called by session-start hook)."""
    global _spend
    _spend = SessionSpend()
    _spend._persist()
