"""PR5 — `llm-router summary` must show VERIFIED savings, not a bare lineage
estimate, and verified/unverified must each carry their own n (North Star
points 6, 8, 12, 13).

Four things are pinned here:

1. ``_lineage_verified_state`` correctly converts a lineage row's epoch float
   timestamp to ISO before applying ``savings.is_verified_saving`` — the TRAP
   this PR fixes (a naive string compare of an epoch float against the ISO
   REALIZED_GATE_SINCE gate is a silent False for every row). Since no real
   lineage row ever carries host="claude_code" (see the function's own
   docstring for why), this is proven with a constructed row rather than
   production data — the function must be able to say "verified" when the
   inputs actually clear the gate.
2. host+timestamp alone is NOT enough to be verified — `mode` must be
   "block". A 2026-09-24 external review reproduced live that a
   mode="echo"/NULL, host="claude_code" row with a nonzero saved amount read
   as verified before this fix: host is stamped on every DIRECT-hook row
   regardless of realized-ness, "block" vs "echo" is what the writer actually
   observed.
3. ``collect()``'s verified figure is sourced from ``savings_stats`` (via
   ``_verified_savings_window``), not from the lineage-derived baseline
   estimate — cross-checked against the same aggregation
   ``commands/savings_report.py`` uses over the identical seeded rows.
4. The wordmark is the llm-router brand, not CHUZOM (asserted by value).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest

from llm_router.observability.summary import (
    _LLM_ROUTER_LOGO_ASCII,
    _LLM_ROUTER_WORDMARK,
    _lineage_verified_state,
    _verified_savings_window,
    collect,
    render_markdown,
)
from llm_router.savings import REALIZED_GATE_SINCE, is_verified_saving

_AFTER_GATE = datetime(2026, 9, 13, 17, 57, 17, tzinfo=timezone.utc)
_BEFORE_GATE = datetime(2026, 9, 13, 17, 57, 10, tzinfo=timezone.utc)


class _FakeLineage:
    def __init__(self, rows):
        self._rows = rows

    def recent(self, limit=5000):
        return self._rows


class _FakeSessions:
    def rollup(self, sid):
        raise AssertionError("rollup should not be called")


def _lineage_row(**kw):
    base = {
        "timestamp": time.time(),
        "cost_usd": 0.0,
        "latency_ms": 0.0,
        "model_chosen": "ollama/x",
        "input_tokens": 0,
        "output_tokens": 0,
        "host": "ollama",
    }
    base.update(kw)
    return base


def _seed_savings_stats(db_path, rows):
    """rows: list of (timestamp_iso, host, model_used, saved, external_cost,
    input_tokens, output_tokens, mode, is_simulated)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE savings_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            estimated_claude_cost_saved REAL NOT NULL,
            external_cost REAL NOT NULL,
            model_used TEXT NOT NULL,
            host TEXT NOT NULL DEFAULT 'claude_code',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            mode TEXT,
            is_simulated INTEGER
        )
        """
    )
    for ts, host, model, saved, ext, in_tok, out_tok, mode, sim in rows:
        conn.execute(
            "INSERT INTO savings_stats (timestamp, session_id, task_type, "
            "estimated_claude_cost_saved, external_cost, model_used, host, "
            "input_tokens, output_tokens, mode, is_simulated) "
            "VALUES (?, 'sess', 'query', ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, saved, ext, model, host, in_tok, out_tok, mode, sim),
        )
    conn.commit()
    conn.close()


# ── 1. TRAP fix: epoch -> ISO conversion ───────────────────────────────────

class TestLineageVerifiedState:
    def test_row_after_gate_with_trusted_host_and_mode_is_verified(self):
        """A lineage row that DOES clear savings.is_verified_saving's full
        predicate (host=claude_code, non-agentic model, timestamp after the
        gate, mode=block) must classify as verified — proving the epoch->ISO
        conversion is correct, not just "always False"."""
        row = _lineage_row(
            host="claude_code",
            model_chosen="anthropic/claude-haiku",
            timestamp=_AFTER_GATE.timestamp(),
            mode="block",
        )
        assert _lineage_verified_state(row) == "verified"

    def test_row_after_gate_right_host_but_no_mode_is_unverified(self):
        """The exact external-review repro, replayed through the lineage
        classifier: host=claude_code + after-gate is NOT enough without
        mode="block". Lineage rows never carry "mode" at all, so
        row.get("mode") is None here — and None is correctly not verified."""
        row = _lineage_row(
            host="claude_code",
            model_chosen="anthropic/claude-haiku",
            timestamp=_AFTER_GATE.timestamp(),
        )
        assert "mode" not in row
        assert _lineage_verified_state(row) == "unverified"

    def test_row_after_gate_right_host_but_echo_mode_is_unverified(self):
        row = _lineage_row(
            host="claude_code",
            model_chosen="anthropic/claude-haiku",
            timestamp=_AFTER_GATE.timestamp(),
            mode="echo",
        )
        assert _lineage_verified_state(row) == "unverified"

    def test_row_before_gate_is_unverified_not_unmeasured(self):
        row = _lineage_row(
            host="claude_code",
            model_chosen="anthropic/claude-haiku",
            timestamp=_BEFORE_GATE.timestamp(),
            mode="block",
        )
        assert _lineage_verified_state(row) == "unverified"

    def test_untrusted_host_after_gate_is_unverified(self):
        """Real lineage rows: host is a provider string ("ollama", "codex",
        …), never "claude_code" — see the function's docstring. Confirms the
        predicate, not a hardcoded shortcut, produces this."""
        row = _lineage_row(host="ollama", timestamp=_AFTER_GATE.timestamp(), mode="block")
        assert _lineage_verified_state(row) == "unverified"

    def test_missing_host_is_unmeasured(self):
        row = _lineage_row(timestamp=_AFTER_GATE.timestamp())
        row["host"] = None
        assert _lineage_verified_state(row) == "unmeasured"

    def test_missing_timestamp_is_unmeasured(self):
        row = _lineage_row(host="claude_code")
        row["timestamp"] = None
        assert _lineage_verified_state(row) == "unmeasured"

    def test_naive_string_compare_would_have_failed_every_row(self):
        """Demonstrates the trap this function avoids: comparing the RAW
        epoch float (or str(epoch)) against REALIZED_GATE_SINCE as a string
        is never >= the ISO gate string, for any real epoch value."""
        epoch = _AFTER_GATE.timestamp()
        assert not (str(epoch) >= REALIZED_GATE_SINCE)


class TestCollectClassifiesLineageRows:
    def test_collect_counts_unverified_lineage_rows(self):
        rows = [_lineage_row(host="ollama"), _lineage_row(host="codex")]
        data = collect(_FakeLineage(rows), _FakeSessions(), since_seconds=None)
        assert data.lineage_unverified_count == 2
        assert data.lineage_verified_count == 0
        assert data.lineage_unmeasured_count == 0

    def test_collect_counts_unmeasured_lineage_rows(self):
        row = _lineage_row()
        row["host"] = None
        data = collect(_FakeLineage([row]), _FakeSessions(), since_seconds=None)
        assert data.lineage_unmeasured_count == 1
        assert data.lineage_unverified_count == 0


# ── 2. mode is required, not just host+timestamp ───────────────────────────
# The external review's exact repro, at the savings.py predicate level.

class TestModeIsRequiredNotJustHostAndTimestamp:
    def test_echo_mode_host_claude_code_after_gate_is_not_verified(self):
        """THE reproduction: host="claude_code", after the gate, a nonzero
        saved amount, mode="echo" (a discarded draft — Claude answered the
        turn anyway). Must NOT be verified."""
        assert is_verified_saving(
            "claude_code", "anthropic/haiku", _AFTER_GATE.isoformat(), "echo"
        ) is False

    def test_null_mode_host_claude_code_after_gate_is_not_verified(self):
        """Every live savings_stats row on the machine that found this bug:
        host="claude_code", after the gate, mode=NULL (the writer never
        recorded one — realized-ness unknown, S9 says unknown must not be
        favourable)."""
        assert is_verified_saving(
            "claude_code", "anthropic/haiku", _AFTER_GATE.isoformat(), None
        ) is False

    def test_block_mode_host_claude_code_after_gate_is_verified(self):
        assert is_verified_saving(
            "claude_code", "anthropic/haiku", _AFTER_GATE.isoformat(), "block"
        ) is True

    def test_verified_saved_sql_agrees_with_the_python_twin(self, tmp_path):
        """SQL and Python must not fork on the mode check either — same
        parity requirement test_a31 already pins for host/model/timestamp."""
        from llm_router.savings import VERIFIED_SAVED_SQL

        db = tmp_path / "t.db"
        _seed_savings_stats(db, rows=[
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 5.0, 0.0, 1, 1, "echo", 0),
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 7.0, 0.0, 1, 1, "block", 0),
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 9.0, 0.0, 1, 1, None, 0),
        ])
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                f"SELECT host, model_used, timestamp, mode, "
                f"CASE WHEN {VERIFIED_SAVED_SQL} > 0 THEN 1 ELSE 0 END "
                f"FROM savings_stats ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert [v for *_, v in rows] == [0, 1, 0], "only the mode='block' row is verified"
        for host, model, ts, mode, verified in rows:
            assert is_verified_saving(host, model, ts, mode) == bool(verified), (
                host, model, ts, mode
            )


# ── 3. verified figure sourced from savings_stats, cross-checked ──────────

class TestVerifiedFigureFromSavingsStats:
    def test_verified_window_splits_by_host_model_gate_and_mode(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        db = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
        _seed_savings_stats(db, rows=[
            # verified: claude_code, non-agentic, after gate, mode=block
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 1.00, 0.10, 100, 50, "block", 0),
            # unverified: wrong host (mode=block doesn't save it)
            (_AFTER_GATE.isoformat(), "router", "anthropic/haiku", 2.00, 0.20, 100, 50, "block", 0),
            # unverified: before the gate
            (_BEFORE_GATE.isoformat(), "claude_code", "anthropic/haiku", 3.00, 0.30, 100, 50, "block", 0),
            # unverified: agentic model
            (_AFTER_GATE.isoformat(), "claude_code", "llm_router-agentic-x", 4.00, 0.40, 100, 50, "block", 0),
            # unverified: right host/model/gate but mode=echo (discarded draft)
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 5.00, 0.50, 100, 50, "echo", 0),
            # unverified: right host/model/gate but mode=NULL (unknown)
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 6.00, 0.60, 100, 50, None, 0),
            # excluded entirely: simulated/benchmark row
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 999.00, 0.0, 1, 1, "block", 1),
        ])
        verified_usd, verified_n, unverified_usd, unverified_n = _verified_savings_window(None)
        assert verified_n == 1
        assert verified_usd == pytest.approx(1.00)
        assert unverified_n == 5
        assert unverified_usd == pytest.approx(2.00 + 3.00 + 4.00 + 5.00 + 6.00)

    def test_verified_matches_savings_report_canonical_n(self, monkeypatch, tmp_path):
        """The alternate acceptable test named in the plan: since lineage
        cannot express "realized" (see TestLineageVerifiedState), summary's
        verified n/usd must equal the SAME aggregation
        commands/savings_report.py computes over the identical savings_stats
        rows."""
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        db = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
        _seed_savings_stats(db, rows=[
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 1.00, 0.10, 100, 50, "block", 0),
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 0.50, 0.05, 80, 40, "block", 0),
            (_AFTER_GATE.isoformat(), "router", "anthropic/haiku", 2.00, 0.20, 100, 50, "block", 0),
            (_BEFORE_GATE.isoformat(), "claude_code", "anthropic/haiku", 3.00, 0.30, 100, 50, "block", 0),
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 4.00, 0.40, 100, 50, "echo", 0),
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 999.00, 0.0, 1, 1, "block", 1),
        ])

        verified_usd, verified_n, unverified_usd, unverified_n = (
            _verified_savings_window(None)
        )

        from llm_router.commands.savings_report import _query

        paid = _query(db, "all", paid=True)
        free = _query(db, "all", paid=False)
        ref_verified_usd = paid["saved"] + free["saved"]
        ref_verified_n = (
            (paid["calls"] - paid["unverified_calls"])
            + (free["calls"] - free["unverified_calls"])
        )

        assert verified_usd == pytest.approx(ref_verified_usd)
        assert verified_n == ref_verified_n
        assert verified_n == 2  # the two claude_code/after-gate/block/non-simulated rows
        assert verified_usd == pytest.approx(1.50)

    def test_collect_populates_verified_fields_from_db(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        db = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
        _seed_savings_stats(db, rows=[
            (_AFTER_GATE.isoformat(), "claude_code", "anthropic/haiku", 1.00, 0.10, 100, 50, "block", 0),
            (_AFTER_GATE.isoformat(), "router", "anthropic/haiku", 2.00, 0.20, 100, 50, "block", 0),
        ])
        rows = [_lineage_row(host="ollama", cost_usd=0.001, input_tokens=10, output_tokens=10)]
        data = collect(_FakeLineage(rows), _FakeSessions(), since_seconds=None)
        assert data.verified_usd == pytest.approx(1.00)
        assert data.verified_n == 1
        assert data.unverified_usd == pytest.approx(2.00)
        assert data.unverified_n == 1

    def test_missing_db_gives_zero_not_a_crash(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "does-not-exist.db"))
        assert _verified_savings_window(None) == (0.0, 0, 0.0, 0)

    def test_table_without_a_mode_column_is_all_unverified_not_a_crash(self, monkeypatch, tmp_path):
        """savings_split_sql's other fallback branch: a table that predates
        the `mode` column (pre-PR5 schema) cannot say whether any row was
        realized, so every row is unverified — not an error."""
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        db = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE savings_stats (timestamp TEXT, session_id TEXT, "
            "task_type TEXT, estimated_claude_cost_saved REAL, external_cost REAL, "
            "model_used TEXT, host TEXT, input_tokens INTEGER, output_tokens INTEGER, "
            "is_simulated INTEGER)"
        )
        conn.execute(
            "INSERT INTO savings_stats VALUES (?, 's', 'query', 1.0, 0.0, 'm', "
            "'claude_code', 0, 0, 0)",
            (_AFTER_GATE.isoformat(),),
        )
        conn.commit()
        conn.close()
        verified_usd, verified_n, unverified_usd, unverified_n = _verified_savings_window(None)
        assert (verified_usd, verified_n) == (0.0, 0)
        assert (unverified_usd, unverified_n) == (1.0, 1)


# ── 4. wordmark ─────────────────────────────────────────────────────────

def test_wordmark_is_llm_router_not_chuzom():
    assert "CHUZOM" not in _LLM_ROUTER_WORDMARK
    assert "chuzom" not in _LLM_ROUTER_WORDMARK.lower()
    collapsed = _LLM_ROUTER_WORDMARK.replace(" ", "").replace("·", "").upper()
    assert "LLM" in collapsed and "ROUTER" in collapsed


def test_markdown_logo_ascii_is_llm_router_not_chuzom():
    """The hand-drawn figlet block in render_markdown's ``_LLM_ROUTER_LOGO_ASCII``
    is a SEPARATE constant from the wordmark and spelled "CHUZOM" in block
    letters until this PR — missed on the first pass because it doesn't reuse
    _LLM_ROUTER_WORDMARK. Assert the value, not source text."""
    assert "CHUZOM" not in _LLM_ROUTER_LOGO_ASCII
    assert "chuzom" not in _LLM_ROUTER_LOGO_ASCII.lower()
    collapsed = _LLM_ROUTER_LOGO_ASCII.replace(" ", "").replace("\n", "").upper()
    assert "LLM" in collapsed and "ROUTER" in collapsed


# ── 5. rendering: no bare-$ headline, and the demoted figure carries n ────

def test_render_markdown_shows_verified_before_unverified(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    _seed_savings_stats(db, rows=[
        (_AFTER_GATE.isoformat(), "router", "anthropic/haiku", 5.00, 0.50, 100, 50, None, 0),
    ])
    rows = [_lineage_row(host="ollama", cost_usd=0.0, input_tokens=100, output_tokens=100)]
    data = collect(_FakeLineage(rows), _FakeSessions(), since_seconds=None)
    md = render_markdown(data)
    assert "Verified savings" in md
    assert data.verified_usd == 0.0
    assert data.unverified_n == 1
    assert "unverified, n=1" in md
    v_idx = md.index("Verified savings")
    u_idx = md.index("unverified, n=1")
    assert v_idx < u_idx, "verified must render before unverified"


def test_markdown_demoted_baseline_figure_carries_its_n(monkeypatch, tmp_path):
    """MEDIUM finding: the lineage-derived '≈$X (Y%) vs always-premium
    baseline (unverified)' figure had no n. It must now say how many rows it
    was computed over."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    rows = [
        _lineage_row(host="ollama", cost_usd=0.0, input_tokens=1000, output_tokens=1000),
        _lineage_row(host="codex", cost_usd=0.0, input_tokens=1000, output_tokens=1000),
    ]
    data = collect(_FakeLineage(rows), _FakeSessions(), since_seconds=None)
    assert data.total_decisions == 2
    md = render_markdown(data)
    assert f"n={data.total_decisions}" in md
    assert "Savings vs always-premium" in md
