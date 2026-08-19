"""B7 / INV-ENF-002/003: a plain-text override updates realization accounting.

Before the fix, stop-enforce.py detected a plain-text bypass (Claude answered a
routed Q&A prompt directly, no tool call) but only wrote a strike/log line — it never
touched session_spend or any savings accounting, so realized_savings_usd overcounted.
This proves _record_override now (a) increments session_spend.overridden_turns and
(b) emits a canonical plain_text_override ledger event with verified_overridden.

Hermetic: ledger points at a tmp DB; session_spend is a controlled in-memory instance.
"""
from __future__ import annotations

import importlib.util

import pytest
from pathlib import Path

HOOK =Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks" / "stop-enforce.py"


def _load_stop_enforce():
    spec = importlib.util.spec_from_file_location("stop_enforce_mod", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plain_text_override_updates_session_spend_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "usage.db"))
    from llm_router import execution_ledger
    from llm_router.session_spend import SessionSpend

    spend = SessionSpend()
    spend.prompt_sequence = 7
    assert spend.overridden_turns == 0

    # Route _record_override's lazy get_session_spend() to our controlled instance.
    import llm_router.session_spend as ss
    monkeypatch.setattr(ss, "get_session_spend", lambda: spend)

    mod = _load_stop_enforce()
    mod._record_override("sess-b7", "query")

    # (a) realization accounting updated — parity with the tool-call override path.
    assert spend.overridden_turns == 1
    assert spend.last_overridden_seq == 7

    # (b) canonical ledger carries the override event.
    rows = execution_ledger._load_rows([("session_id", "=", "sess-b7")])
    ev = [r for r in rows if r["event_type"] == "plain_text_override"]
    assert len(ev) == 1
    assert ev[0]["override_type"] == "plain_text"
    assert ev[0]["realization_status"] == "verified_overridden"
    assert ev[0]["used_by_host"] == 0  # False


def test_record_override_is_fail_open(tmp_path, monkeypatch):
    """A hook must never raise: if session_spend blows up, override still no-ops cleanly."""
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "usage.db"))
    import llm_router.session_spend as ss

    def _boom():
        raise RuntimeError("spend backend down")

    monkeypatch.setattr(ss, "get_session_spend", _boom)
    mod = _load_stop_enforce()
    # Must not raise despite the session_spend failure.
    mod._record_override("sess-fail", "analyze")
    # Ledger event still recorded (independent try/except).
    from llm_router import execution_ledger
    rows = execution_ledger._load_rows([("session_id", "=", "sess-fail")])
    assert any(r["event_type"] == "plain_text_override" for r in rows)


def test_dedup_same_prompt_sequence_counts_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "usage.db"))
    from llm_router.session_spend import SessionSpend

    spend = SessionSpend()
    spend.prompt_sequence = 3
    import llm_router.session_spend as ss
    monkeypatch.setattr(ss, "get_session_spend", lambda: spend)

    mod = _load_stop_enforce()
    mod._record_override("s", "query")
    mod._record_override("s", "query")  # same prompt_sequence → deduped
    assert spend.overridden_turns == 1


class TestLoadRowsRejectsSqlText:
    """`_load_rows` takes structured filters so an f-string cannot reach the query.

    It previously took `where: str` and interpolated it. All four callers passed
    literals, so it was safe — but the safety lived in the callers and had to be
    re-established by reading them, and the signature invited an f-string from
    the next one. 32_BANDIT_TRIAGE §3 filed it as "safe today", which documents a
    hazard instead of removing it.

    These assert the constraint is enforced rather than merely conventional: the
    column is checked against the table's own definition and the operator against
    a fixed set, so neither side of a predicate can carry SQL.
    """

    def test_unknown_column_is_rejected(self):
        from llm_router import execution_ledger

        with pytest.raises(ValueError, match="unknown ledger column"):
            execution_ledger._load_rows([("session_id = 'x' OR 1=1 --", "=", "v")])

    def test_injection_via_the_operator_is_rejected(self):
        from llm_router import execution_ledger

        with pytest.raises(ValueError, match="unsupported operator"):
            execution_ledger._load_rows([("session_id", "= ? OR 1=1 --", "v")])

    def test_a_real_column_with_a_real_operator_works(self, tmp_path):
        from llm_router import execution_ledger

        db = tmp_path / "ledger.db"
        execution_ledger.record_event(
            execution_ledger.LedgerEvent(
                session_id="sess-ok", event_type="route_started"
            ),
            path=db,
        )
        rows = execution_ledger._load_rows([("session_id", "=", "sess-ok")], db)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess-ok"

    def test_multiple_filters_are_anded(self, tmp_path):
        """The one caller needing two predicates (`ts >= ? AND ts < ?`) must still
        express itself, or the structured form would have been a downgrade."""
        from llm_router import execution_ledger

        db = tmp_path / "ledger.db"
        for ts in (100.0, 200.0, 300.0):
            execution_ledger.record_event(
                execution_ledger.LedgerEvent(
                    session_id="s", ts=ts, event_type="route_started"
                ),
                path=db,
            )
        rows = execution_ledger._load_rows(
            [("ts", ">=", 150.0), ("ts", "<", 300.0)], db
        )
        assert [r["ts"] for r in rows] == [200.0]
