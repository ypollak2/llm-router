# Ported from Chuzom's execution_ledger tests; adapted to llm-router's
# LedgerEvent/Accounting API and pytest's tmp_path fixture (path is injected
# explicitly into every call — no global config/env patching needed since
# every public function in execution_ledger.py accepts `path: Path | None`).
"""Tests for src/llm_router/execution_ledger.py (WS1).

Covers: schema shape matches contracts.EXECUTION_EVENTS_COLUMNS; migrations
are idempotent against both a fresh and a pre-migration DB; INSERT OR IGNORE
dedup (INV-COST-003); INV-COST-002 (actual_cost_usd == Sigma attempt costs);
Gate 18 realization-gated savings; brand-leak absence.
"""

from __future__ import annotations

import sqlite3

import pytest

from llm_router import execution_ledger as el
from llm_router.contracts import EXECUTION_EVENTS_COLUMNS


def _db(tmp_path, name="usage.db"):
    return tmp_path / name


class TestSchema:
    def test_columns_match_contracts(self, tmp_path):
        path = _db(tmp_path)
        conn = el._connect(path)
        try:
            cur = conn.execute("PRAGMA table_info(execution_events)")
            actual_columns = tuple(row[1] for row in cur.fetchall())
        finally:
            conn.close()
        assert actual_columns == EXECUTION_EVENTS_COLUMNS

    def test_event_id_is_primary_key(self, tmp_path):
        path = _db(tmp_path)
        conn = el._connect(path)
        try:
            cur = conn.execute("PRAGMA table_info(execution_events)")
            pk_columns = [row[1] for row in cur.fetchall() if row[5]]
        finally:
            conn.close()
        assert pk_columns == ["event_id"]


class TestMigrations:
    def test_connect_twice_is_idempotent(self, tmp_path):
        path = _db(tmp_path)
        el._connect(path).close()
        # Second connect must not raise (ALTER TABLE columns already exist).
        conn = el._connect(path)
        try:
            cur = conn.execute("PRAGMA table_info(execution_events)")
            columns = tuple(row[1] for row in cur.fetchall())
        finally:
            conn.close()
        assert columns == EXECUTION_EVENTS_COLUMNS

    def test_migrates_pre_migration_table(self, tmp_path):
        """A DB whose table predates the Gap 1/2/3 columns must be upgraded
        in place by `_MIGRATIONS`, mirroring a pre-existing `usage.db` that
        only cost.py (never execution_ledger.py) had written to."""
        path = _db(tmp_path)
        pre_migration_ddl = """
        CREATE TABLE execution_events (
            schema_version INTEGER NOT NULL,
            event_id TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            session_id TEXT,
            turn_id TEXT,
            route_id TEXT,
            attempt_id TEXT,
            event_type TEXT NOT NULL,
            task_type TEXT,
            routing_profile TEXT,
            host_mode TEXT,
            provider TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            measured_cost_usd REAL,
            baseline_equivalent_cost_usd REAL,
            hook_input_tokens INTEGER,
            hook_output_tokens INTEGER,
            accepted INTEGER,
            rejected INTEGER,
            rejection_reason TEXT,
            escalation_reason TEXT,
            fallback_reason TEXT,
            provider_failure_reason TEXT,
            used_by_host INTEGER,
            realization_status TEXT,
            override_type TEXT,
            terminal_state TEXT,
            metadata TEXT
        );
        """
        raw = sqlite3.connect(str(path))
        try:
            raw.executescript(pre_migration_ddl)
            raw.commit()
        finally:
            raw.close()

        conn = el._connect(path)
        try:
            cur = conn.execute("PRAGMA table_info(execution_events)")
            columns = tuple(row[1] for row in cur.fetchall())
        finally:
            conn.close()
        # Migrated columns are appended after the pre-migration set (ALTER
        # TABLE ADD COLUMN always appends), not reordered to match _DDL.
        assert set(columns) == set(EXECUTION_EVENTS_COLUMNS)
        for extra in (
            "classifier_cost_usd",
            "failed_attempt_cost_usd",
            "baseline_tokens",
            "adoption_method",
        ):
            assert extra in columns


class TestDedup:
    def test_insert_or_ignore_dedup(self, tmp_path):
        path = _db(tmp_path)
        ev = el.LedgerEvent(
            event_id="fixed-event-id",
            route_id="route-1",
            event_type="attempt_completed",
            measured_cost_usd=0.01,
        )
        assert el.record_event(ev, path=path) is True
        # Re-recording the identical event_id must be a silent no-op.
        ev2 = el.LedgerEvent(
            event_id="fixed-event-id",
            route_id="route-1",
            event_type="attempt_completed",
            measured_cost_usd=999.0,  # would corrupt totals if double-counted
        )
        assert el.record_event(ev2, path=path) is True

        rows = el._load_rows("route_id = ?", ("route-1",), path)
        assert len(rows) == 1
        assert rows[0]["measured_cost_usd"] == 0.01

    def test_record_event_fail_open_never_raises(self, tmp_path, monkeypatch):
        # Point at a path whose parent cannot be created (a file, not a dir),
        # forcing an internal failure; record_event must return False, not raise.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        bogus_path = blocker / "usage.db"
        ev = el.LedgerEvent(event_id="e1", event_type="attempt_completed")
        assert el.record_event(ev, path=bogus_path) is False


class TestInvCost002:
    """INV-COST-002: get_route_accounting(route_id).actual_cost_usd == Sigma attempt costs."""

    @pytest.mark.parametrize(
        "costs",
        [
            [0.01],
            [0.01, 0.02, 0.03],
            [0.0, 0.5, 1.25],
            [0.123456, 0.000001],
        ],
    )
    def test_actual_cost_equals_sum_of_attempts(self, tmp_path, costs):
        path = _db(tmp_path)
        route_id = "route-inv-cost-002"
        for i, cost in enumerate(costs):
            ev = el.LedgerEvent(
                event_id=f"attempt-{i}",
                route_id=route_id,
                event_type="attempt_completed",
                measured_cost_usd=cost,
            )
            assert el.record_event(ev, path=path) is True

        acc = el.get_route_accounting(route_id, path=path)
        assert acc.actual_cost_usd == pytest.approx(round(sum(costs), 6))
        assert acc.billable_attempt_count == len(costs)
        assert acc.cost_unknown_attempts == 0

    def test_cost_unknown_attempts_excluded_from_actual_cost(self, tmp_path):
        path = _db(tmp_path)
        route_id = "route-unknown-cost"
        el.record_event(
            el.LedgerEvent(
                event_id="known",
                route_id=route_id,
                event_type="attempt_completed",
                measured_cost_usd=0.05,
            ),
            path=path,
        )
        el.record_event(
            el.LedgerEvent(
                event_id="unknown",
                route_id=route_id,
                event_type="attempt_failed",
                measured_cost_usd=None,
            ),
            path=path,
        )
        acc = el.get_route_accounting(route_id, path=path)
        assert acc.actual_cost_usd == pytest.approx(0.05)
        assert acc.cost_unknown_attempts == 1
        assert acc.billable_attempt_count == 2

    def test_non_billable_events_do_not_affect_actual_cost(self, tmp_path):
        path = _db(tmp_path)
        route_id = "route-non-billable"
        el.record_event(
            el.LedgerEvent(
                event_id="started",
                route_id=route_id,
                event_type="route_started",
            ),
            path=path,
        )
        el.record_event(
            el.LedgerEvent(
                event_id="attempt",
                route_id=route_id,
                event_type="attempt_completed",
                measured_cost_usd=0.02,
            ),
            path=path,
        )
        acc = el.get_route_accounting(route_id, path=path)
        assert acc.actual_cost_usd == pytest.approx(0.02)
        assert acc.attempt_count == 1  # route_started is not billable


class TestRealizedSavingsGating:
    """Gate 18: potential savings only count as realized when
    realization_status == verified_used AND adoption_method in
    COUNTS_AS_REALIZED (door_call/agent_marked)."""

    def _route(self, path, route_id, *, realization_status, adoption_method):
        el.record_event(
            el.LedgerEvent(
                event_id=f"{route_id}-attempt",
                route_id=route_id,
                event_type="attempt_completed",
                measured_cost_usd=0.01,
                baseline_equivalent_cost_usd=0.10,
            ),
            path=path,
        )
        el.record_event(
            el.LedgerEvent(
                event_id=f"{route_id}-realized",
                route_id=route_id,
                event_type="route_realized",
                realization_status=realization_status,
                adoption_method=adoption_method,
            ),
            path=path,
        )

    def test_verified_used_door_call_is_realized(self, tmp_path):
        path = _db(tmp_path)
        self._route(
            path, "r1", realization_status="verified_used", adoption_method="door_call"
        )
        acc = el.get_route_accounting("r1", path=path)
        assert acc.potential_savings_usd == pytest.approx(0.09)
        assert acc.realized_savings_usd == pytest.approx(0.09)
        assert acc.realized_routes == 1

    def test_verified_used_agent_marked_is_realized(self, tmp_path):
        path = _db(tmp_path)
        self._route(
            path, "r2", realization_status="verified_used", adoption_method="agent_marked"
        )
        acc = el.get_route_accounting("r2", path=path)
        assert acc.realized_savings_usd == pytest.approx(0.09)

    def test_verified_overridden_is_not_realized(self, tmp_path):
        path = _db(tmp_path)
        self._route(
            path, "r3", realization_status="verified_overridden", adoption_method=None
        )
        acc = el.get_route_accounting("r3", path=path)
        assert acc.potential_savings_usd == pytest.approx(0.09)
        assert acc.realized_savings_usd == 0.0
        assert acc.overridden_routes == 1
        assert acc.realized_routes == 0

    def test_unknown_realization_is_not_realized(self, tmp_path):
        path = _db(tmp_path)
        self._route(path, "r4", realization_status="unknown", adoption_method=None)
        acc = el.get_route_accounting("r4", path=path)
        assert acc.realized_savings_usd == 0.0
        assert acc.realization_unknown_routes == 1

    def test_content_match_is_likely_used_not_realized(self, tmp_path):
        path = _db(tmp_path)
        self._route(
            path, "r5", realization_status="verified_used", adoption_method="content_match"
        )
        acc = el.get_route_accounting("r5", path=path)
        assert acc.realized_savings_usd == 0.0
        assert acc.likely_used_routes == 1
        assert acc.realized_routes == 1  # still counted as a realization event

    def test_verified_used_with_null_adoption_is_back_compat_door_call(self, tmp_path):
        """A pre-migration verified_used row with no adoption_method predates
        Gap 3 gating and must be treated as door_call (the strongest signal),
        not silently dropped from realized savings."""
        path = _db(tmp_path)
        self._route(path, "r6", realization_status="verified_used", adoption_method=None)
        acc = el.get_route_accounting("r6", path=path)
        assert acc.realized_savings_usd == pytest.approx(0.09)

    def test_no_realization_event_never_realized(self, tmp_path):
        path = _db(tmp_path)
        el.record_event(
            el.LedgerEvent(
                event_id="r7-attempt",
                route_id="r7",
                event_type="attempt_completed",
                measured_cost_usd=0.01,
                baseline_equivalent_cost_usd=0.10,
            ),
            path=path,
        )
        acc = el.get_route_accounting("r7", path=path)
        assert acc.potential_savings_usd == pytest.approx(0.09)
        assert acc.realized_savings_usd == 0.0

    def test_late_route_realized_update_supersedes_earlier_status(self, tmp_path):
        """A route_realized event that arrives after an earlier, weaker one
        (e.g. an initial `unknown` recorded before adoption evidence exists)
        must have its LATER status win deterministically, per `_load_rows`'s
        `ORDER BY ts ASC, event_id ASC` — mirroring a real host that reports
        realization asynchronously and sometimes revises its verdict."""
        path = _db(tmp_path)
        route_id = "r8"
        el.record_event(
            el.LedgerEvent(
                event_id=f"{route_id}-attempt",
                route_id=route_id,
                event_type="attempt_completed",
                measured_cost_usd=0.01,
                baseline_equivalent_cost_usd=0.10,
            ),
            path=path,
        )
        el.record_event(
            el.LedgerEvent(
                event_id=f"{route_id}-realized-1",
                route_id=route_id,
                event_type="route_realized",
                realization_status="unknown",
            ),
            path=path,
        )
        acc_before = el.get_route_accounting(route_id, path=path)
        assert acc_before.realized_savings_usd == 0.0
        assert acc_before.realization_unknown_routes == 1

        # A later event confirms the route actually was adopted.
        el.record_event(
            el.LedgerEvent(
                event_id=f"{route_id}-realized-2",
                route_id=route_id,
                event_type="route_realized",
                realization_status="verified_used",
                adoption_method="door_call",
            ),
            path=path,
        )
        acc_after = el.get_route_accounting(route_id, path=path)
        assert acc_after.realized_savings_usd == pytest.approx(0.09)
        assert acc_after.realized_routes == 1
        assert acc_after.realization_unknown_routes == 0


class TestReconciliation:
    def test_reconcile_session_matches_canonical(self, tmp_path):
        path = _db(tmp_path)
        el.record_event(
            el.LedgerEvent(
                event_id="s1",
                session_id="sess-1",
                event_type="attempt_completed",
                measured_cost_usd=0.03,
            ),
            path=path,
        )
        result = el.reconcile_session("sess-1", 0.03, path=path)
        assert result.reconciled is True
        assert result.canonical_actual_usd == pytest.approx(0.03)
        assert result.delta_usd == pytest.approx(0.0)

    def test_reconcile_session_detects_drift(self, tmp_path):
        path = _db(tmp_path)
        el.record_event(
            el.LedgerEvent(
                event_id="s2",
                session_id="sess-2",
                event_type="attempt_completed",
                measured_cost_usd=0.03,
            ),
            path=path,
        )
        result = el.reconcile_session("sess-2", 0.05, path=path)
        assert result.reconciled is False
        assert result.delta_usd == pytest.approx(0.02)

    def test_reconcile_session_flags_unknown_cost(self, tmp_path):
        path = _db(tmp_path)
        el.record_event(
            el.LedgerEvent(
                event_id="s3",
                session_id="sess-3",
                event_type="attempt_failed",
                measured_cost_usd=None,
            ),
            path=path,
        )
        result = el.reconcile_session("sess-3", path=path)
        assert result.cost_unknown_attempts == 1
        assert result.reconciled is False


class TestBrandLeak:
    def test_no_chuzom_in_public_names_or_values(self):
        public_names = [name for name in dir(el) if not name.startswith("_")]
        for name in public_names:
            assert "chuzom" not in name.lower()
            value = getattr(el, name)
            if isinstance(value, str):
                assert "chuzom" not in value.lower()
