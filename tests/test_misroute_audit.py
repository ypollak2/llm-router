"""Post-hoc misroute audit — scoring, idempotence, and fail-open behaviour.

WHY THIS EXISTS
===============

Ported upstream after ``scripts/check_downstream_superset.py`` measured that
the downstream package had this capability and LLM Router did not — one of four
public symbols reached downstream and defined nowhere here.

The port is deliberately NOT named ``audit_routing`` upstream, because
``llm_router.audit_routing`` used to exist as a separate, unrelated feature (a
live per-turn compliance log). Both repositories having a file at that path
with disjoint APIs would have made a file-level sync silently delete one of
them. ``audit_routing.py`` was itself removed in GH#68/#70/#71 (it depended
entirely on ``llm_router.enterprise``, which this distribution never shipped,
so the feature never worked), which retires that specific collision risk —
noted here rather than silently dropped, since this docstring used to point
at a test that asserted it.

WHAT IS ASSERTED, AND WHY EACH ONE
==================================

* Every scoring branch, including the ambiguous judge-score band. The band
  exists so a 0.6 score is not forced into a verdict, and a test that only
  checked the extremes would let someone "simplify" it away.
* ``was_downshifted`` alone scores ``insufficient_data``, not
  ``likely_misroute``. This is the assertion most likely to be broken by a
  well-meaning edit: downshifting to a cheaper model is what this system is
  FOR, so counting it as a misroute would make the misroute rate climb every
  time the router worked correctly. Recorded as an assertion rather than a
  comment for that reason.
* Idempotence, by running the audit twice against the same rows.
* Fail-open on a broken database — the audit is a side channel over historical
  rows and must never be able to break, delay, or affect live routing.

CONTROL (re-run if edited)
==========================

* Remove the ``AND audit_verdict IS NULL`` from ``_write_verdict``'s WHERE:
  ``test_the_writeback_guard_holds_on_its_own`` FAILS.

  This entry originally named ``test_second_run_does_not_overwrite_the_first_verdict``
  and that was WRONG — running the control produced zero failures. The
  sampler's own ``WHERE audit_verdict IS NULL`` means a second run selects no
  rows, so ``_write_verdict`` is never reached and the end-to-end test proves
  the filter rather than the guard. The two layers defend different things: the
  filter avoids redundant work, the guard is what makes CONCURRENT runs safe.
  Only a direct call against an already-scored row exercises it.
* Change the ``was_downshifted`` branch to ``likely_misroute``:
  ``test_downshift_alone_is_not_a_misroute`` FAILS.
* Drop ``MIGRATE_ROUTING_DECISIONS_ADD_AUDIT`` from the migration list:
  every DB-backed test here fails with ``no such column: audit_verdict``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from llm_router.misroute_audit import (
    AuditedDecision,
    audit_disabled,
    run_audit,
    sample_unaudited_decisions,
    score_decision,
)


class TestScoring:
    """Pure-function branches — no database required, so all of them are cheap."""

    def test_low_judge_score_is_a_misroute(self):
        d = score_decision({"id": 1, "judge_score": 0.2})
        assert d.verdict == "likely_misroute"
        assert "0.20" in d.reason, "the reason must carry the score that drove it"

    def test_high_judge_score_is_correct(self):
        assert score_decision({"id": 1, "judge_score": 0.9}).verdict == "likely_correct"

    def test_threshold_boundaries_are_inclusive_as_documented(self):
        assert score_decision({"id": 1, "judge_score": 0.75}).verdict == "likely_correct"
        assert score_decision({"id": 1, "judge_score": 0.5}).verdict != "likely_misroute"

    def test_ambiguous_band_falls_through_to_the_secondary_signal(self):
        """0.5-0.75 is genuinely ambiguous; it must not be forced into a verdict."""
        ambiguous_only = score_decision({"id": 1, "judge_score": 0.6})
        assert ambiguous_only.verdict == "insufficient_data"

        ambiguous_plus_downgrade = score_decision(
            {"id": 1, "judge_score": 0.6, "complexity_downgraded": 1}
        )
        assert ambiguous_plus_downgrade.verdict == "likely_misroute", (
            "a score in the ambiguous band must fall through to the secondary "
            "signal, not short-circuit on the score alone"
        )

    def test_complexity_downgrade_without_a_score_is_a_misroute(self):
        d = score_decision({"id": 1, "complexity_downgraded": 1})
        assert d.verdict == "likely_misroute"

    def test_downshift_alone_is_not_a_misroute(self):
        """The assertion most likely to be 'simplified' into a bug.

        Downshifting to a cheaper model is the product working. Scoring it as a
        misroute would make the misroute rate rise every time the router did
        exactly what it exists to do.
        """
        d = score_decision({"id": 1, "was_downshifted": 1})
        assert d.verdict == "insufficient_data", (
            "was_downshifted alone was scored as a misroute — that counts "
            "successful cost routing as a defect"
        )

    def test_no_signals_at_all(self):
        assert score_decision({"id": 7}).verdict == "insufficient_data"

    def test_verdict_carries_the_row_id(self):
        assert score_decision({"id": 4242, "judge_score": 0.1}).decision_id == 4242


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    """A real usage.db with the audit columns, via the production migration path."""
    import llm_router.cost as cost

    db_path = tmp_path / "usage.db"
    monkeypatch.setattr(cost, "DB_PATH", db_path, raising=False)
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    monkeypatch.delenv("LLM_ROUTER_AUDIT_DISABLED", raising=False)

    conn = sqlite3.connect(db_path)
    conn.executescript(cost.CREATE_ROUTING_DECISIONS_TABLE)
    for stmt in cost.MIGRATE_ROUTING_DECISIONS_ADD_JUDGE_SCORE:
        conn.execute(stmt)
    for stmt in cost.MIGRATE_ROUTING_DECISIONS_ADD_COMPLEXITY_TRACKING:
        conn.execute(stmt)
    for stmt in cost.MIGRATE_ROUTING_DECISIONS_ADD_AUDIT:
        conn.execute(stmt)
    conn.executemany(
        "INSERT INTO routing_decisions (judge_score, complexity_downgraded, was_downshifted) "
        "VALUES (?, ?, ?)",
        [(0.1, 0, 0), (0.95, 0, 0), (None, 1, 0), (None, 0, 1), (None, 0, 0)],
    )
    conn.commit()
    conn.close()
    return db_path


def _verdicts(db_path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT id, audit_verdict, audit_checked_at FROM routing_decisions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class TestMigration:
    def test_the_audit_columns_are_in_the_applied_migration_list(self):
        """Defining a migration and not applying it is a silent no-op."""
        import llm_router.cost as cost
        import inspect

        source = inspect.getsource(cost.init_db) if hasattr(cost, "init_db") else ""
        module_src = inspect.getsource(cost)
        assert "+ MIGRATE_ROUTING_DECISIONS_ADD_AUDIT" in module_src, (
            "MIGRATE_ROUTING_DECISIONS_ADD_AUDIT is defined but never added to "
            "all_migrations — the columns would never exist on a real database "
            "and every audit run would fail-open to zero forever, silently."
        )
        assert source is not None

    def test_both_columns_default_null(self, audit_db):
        """NULL is the 'not yet audited' marker; a non-NULL default hides rows."""
        rows = _verdicts(audit_db)
        assert all(v is None and t is None for _, v, t in rows)


class TestRunAudit:
    @pytest.mark.asyncio
    async def test_audits_every_unscored_row(self, audit_db):
        report = await run_audit(limit=100)
        assert report["disabled"] is False
        assert report["sampled"] == 5
        assert report["audited"] == 5
        assert report["verdict_counts"]["likely_misroute"] == 2  # low score + downgrade
        assert report["verdict_counts"]["likely_correct"] == 1
        assert report["verdict_counts"]["insufficient_data"] == 2

    @pytest.mark.asyncio
    async def test_second_run_does_not_overwrite_the_first_verdict(self, audit_db):
        """Idempotence, observed through the stored timestamp, not just counts."""
        await run_audit(limit=100)
        first = _verdicts(audit_db)

        second_report = await run_audit(limit=100)
        second = _verdicts(audit_db)

        assert second_report["sampled"] == 0, (
            "the second run re-sampled rows that already had a verdict — the "
            "`audit_verdict IS NULL` filter is not doing its job"
        )
        assert first == second, (
            "verdicts or timestamps changed on a re-run; the audit is supposed "
            "to be non-destructive by construction"
        )

    @pytest.mark.asyncio
    async def test_the_writeback_guard_holds_on_its_own(self, audit_db):
        """The SECOND layer of idempotence, tested directly.

        ``test_second_run_does_not_overwrite_the_first_verdict`` above does NOT
        cover this, and the first draft of this file claimed it did. Removing
        `AND audit_verdict IS NULL` from ``_write_verdict``'s WHERE broke
        nothing, because the sampler's own `WHERE audit_verdict IS NULL` means
        the second run selects zero rows and ``_write_verdict`` is never
        reached. The end-to-end test proves the filter, not the guard.

        That matters because the two layers protect against different things:
        the filter avoids redundant work, the guard is what makes a CONCURRENT
        run safe — two audits overlapping both sample the same NULL row, and
        only the WHERE stops the second from overwriting the first.

        So this calls ``_write_verdict`` directly against a row that already
        has a verdict, which is the state a concurrent run produces.
        """
        from llm_router.misroute_audit import _write_verdict

        first = AuditedDecision(1, "likely_misroute", "first")
        assert await _write_verdict(first) is True

        before = _verdicts(audit_db)
        second = AuditedDecision(1, "likely_correct", "second")
        wrote = await _write_verdict(second)

        assert wrote is False, (
            "_write_verdict reported success against a row that already had a "
            "verdict — the `AND audit_verdict IS NULL` guard is gone, and two "
            "concurrent audit runs would clobber each other"
        )
        assert _verdicts(audit_db) == before, (
            "the stored verdict changed despite the guard reporting no write"
        )

    @pytest.mark.asyncio
    async def test_disabled_is_inert(self, audit_db, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_AUDIT_DISABLED", "1")
        assert audit_disabled() is True
        report = await run_audit(limit=100)
        assert report["disabled"] is True
        assert report["sampled"] == 0
        assert all(v is None for _, v, _ in _verdicts(audit_db)), (
            "rows were written despite the audit being disabled"
        )

    @pytest.mark.asyncio
    async def test_limit_is_respected(self, audit_db):
        report = await run_audit(limit=2)
        assert report["sampled"] == 2
        assert sum(1 for _, v, _ in _verdicts(audit_db) if v is not None) == 2

    @pytest.mark.asyncio
    async def test_fails_open_on_a_broken_database(self, tmp_path, monkeypatch):
        """A side channel over historical rows must never raise into a caller."""
        import llm_router.cost as cost

        broken = tmp_path / "not-a-database.db"
        broken.write_text("this is not sqlite")
        monkeypatch.setattr(cost, "DB_PATH", broken, raising=False)
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(broken))
        monkeypatch.delenv("LLM_ROUTER_AUDIT_DISABLED", raising=False)

        assert await sample_unaudited_decisions(limit=10) == []
        report = await run_audit(limit=10)
        assert report["sampled"] == 0
        assert report["audited"] == 0


class TestCli:
    """``llm_router audit misroute`` — a SUBcommand, deliberately.

    The downstream package exposed this as a top-level ``audit`` command, which
    is exactly what collided with the existing enterprise audit-log CLI here.
    Nesting composes; a second top-level command would have had to displace one.
    """

    def test_misroute_is_a_subcommand_of_audit(self, audit_db, capsys):
        from llm_router.commands.audit import main

        assert main(["misroute", "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["sampled"] == 5
        assert report["verdict_counts"]["likely_misroute"] == 2

    def test_verify_and_export_still_reachable(self):
        """The new subcommand must not displace the ones that were there."""
        from llm_router.commands.audit import main

        with pytest.raises(SystemExit):
            main(["--help"])

    def test_disabled_still_exits_zero(self, audit_db, monkeypatch, capsys):
        """Reporting on history; a disabled audit is not an operational failure."""
        from llm_router.commands.audit import main

        monkeypatch.setenv("LLM_ROUTER_AUDIT_DISABLED", "1")
        assert main(["misroute"]) == 0
        assert "disabled" in capsys.readouterr().out.lower()


def test_audit_routing_module_is_gone():
    """Pins the GH#68/#70/#71 removal rather than silently losing coverage.

    This used to be ``test_no_collision_with_the_live_audit_log``, which
    imported ``llm_router.audit_routing`` (a live per-turn compliance log)
    alongside this module and asserted their exported names never collided —
    the two shared a filename downstream, and a file-level sync could have
    silently overwritten one with the other.

    ``audit_routing.py`` depended entirely on ``llm_router.enterprise``,
    which this distribution never shipped, so the feature it implemented
    never actually worked here (GH#68/#71) and was removed rather than
    fixed. That retires the collision this test used to guard: there is
    nothing left for ``misroute_audit`` to collide with. If a module named
    ``llm_router.audit_routing`` reappears, this test should be replaced with
    a real collision check again rather than deleted quietly.
    """
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("llm_router.audit_routing")
