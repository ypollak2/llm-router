"""The two defects the T-05 fix itself introduced.

Both were found by exercising the change rather than reading it, and both are
the shapes this audit is about — so they get tests rather than a changelog line.

1. `production_only(include_simulated=True)` returned `""`. Five callers EMBED
   the fragment (`f"WHERE {production_only(..., prefix='')} AND date(...)"`),
   where an empty string composes to the literal `WHERE  AND date(...)` and
   raises `sqlite3.OperationalError`. The escape hatch had never been called.

2. `agentic/telemetry.py` creates `savings_stats` with its own DDL on a fresh
   DB. The INSERT gained `is_simulated`; the DDL did not. `_default_recorder`
   is deliberately fail-open, so the resulting OperationalError went into
   `except: pass` and EVERY agentic savings row was silently dropped on any
   fresh install — total data loss with no error anywhere.
"""

from __future__ import annotations

import sqlite3

import pytest

from llm_router import cost


# ── 1. the escape hatch must actually execute ────────────────────────────────

HATCH_SIMULATED = [
    "get_daily_spend", "get_monthly_spend", "get_daily_claude_tokens",
    "get_daily_claude_breakdown", "get_cache_savings", "get_savings_summary",
    "get_team_savings", "get_realized_savings", "get_lifetime_savings_summary",
    "get_savings_by_period",
]
HATCH_SYNTHETIC = [
    "get_quality_report", "get_routing_savings_vs_sonnet", "get_router_efficiency",
]

_ARGS = {
    "get_daily_spend_by_task_type": ("query",),
}
_KWARGS = {
    "get_cache_savings": {"period": "all"},
    "get_savings_summary": {"period": "all"},
    "get_team_savings": {"period": "all"},
    "get_realized_savings": {"period": "all"},
    "get_lifetime_savings_summary": {"days": 0},
    "get_quality_report": {"days": 30},
    "get_routing_savings_vs_sonnet": {"days": 0},
    "get_router_efficiency": {"period": "all"},
}


@pytest.fixture
async def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    db = await cost._get_db()
    await db.close()
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, flag",
    [(n, "include_simulated") for n in HATCH_SIMULATED + ["get_daily_spend_by_task_type"]]
    + [(n, "include_synthetic") for n in HATCH_SYNTHETIC],
)
async def test_the_escape_hatch_produces_valid_sql(ledger, name, flag):
    """Passing the documented flag must return data, not an OperationalError.

    A filter helper is only half a feature; the other half is that every caller
    composes its output into something SQLite will parse. Nothing exercised
    that until the tests for this change were written.
    """
    fn = getattr(cost, name)
    await fn(*_ARGS.get(name, ()), **_KWARGS.get(name, {}), **{flag: True})


@pytest.mark.asyncio
async def test_the_disabled_filter_is_a_valid_predicate_when_embedded():
    """The unit behind the failure above.

    An appending caller can take `""`; an embedding caller cannot. `prefix=""`
    is what an embedding caller passes, and it must get a predicate.
    """
    assert cost.production_only(True, prefix="") == "1=1"
    assert cost.production_only(True, prefix="AND") == ""
    assert cost.production_only(False, prefix="") == "is_simulated = 0"
    assert "AND is_simulated = 0" == cost.production_only(False, prefix="AND")


def test_no_money_query_can_compose_an_empty_where():
    """Every embedded call site must survive the flag being flipped.

    Static guard for the shape, so a sixth embedding caller added later cannot
    reintroduce it without this failing.
    """
    import inspect
    import re

    src = inspect.getsource(cost)
    embedded = re.findall(r"WHERE \{production_only\(([^)]*)\)\}", src)
    assert embedded, "the embedded-call pattern has disappeared — update this test"
    for call in embedded:
        assert "prefix=''" in call or 'prefix=""' in call, (
            f"embedded production_only() call without an explicit empty prefix: {call!r}. "
            "It will compose a broken WHERE when the filter is disabled."
        )


# ── 2. telemetry's own DDL must match its own INSERT ─────────────────────────

@pytest.mark.asyncio
async def test_agentic_savings_row_survives_on_a_fresh_database(tmp_path, monkeypatch):
    """The row must land on a DB this module creates itself.

    `_default_recorder` is fail-open by design — telemetry must never break a
    delegation — so a schema mismatch here does not raise, it just loses every
    row. The only way to see it is to look for the row.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_DB_PATH", raising=False)
    from llm_router.agentic import telemetry

    await telemetry._default_recorder({
        "session_id": "sess-fresh", "task_type": "code",
        "saved_usd": 1.25, "actual_usd": 0.05, "model": "qwen",
    })

    db = tmp_path / "usage.db"
    assert db.exists(), "no database was created at all"
    conn = sqlite3.connect(str(db))
    rows = list(conn.execute(
        "SELECT estimated_claude_cost_saved, is_simulated FROM savings_stats"))
    conn.close()
    assert len(rows) == 1, (
        f"expected one savings row, got {rows}. `_default_recorder` swallows its "
        "exceptions, so an empty table here means the INSERT and the DDL disagree."
    )
    saved, simulated = rows[0]
    assert saved == pytest.approx(1.25)
    assert simulated == 1, "a row written under pytest must be stamped synthetic"


@pytest.mark.asyncio
async def test_agentic_telemetry_honours_llm_router_home(tmp_path, monkeypatch):
    """It must not write to the operator's real ledger.

    `_db_path()` composed `~/.llm-router/usage.db` directly and ignored
    `LLM_ROUTER_HOME`. Verifying the DDL fix above, a probe that believed it was
    sandboxed put a fake $1.25 row — stamped `is_simulated=0`, i.e. production —
    into the live database. Same shape as evidence/AUDITOR_INCIDENT.md.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_DB_PATH", raising=False)
    from llm_router.agentic import telemetry

    resolved = telemetry._db_path()
    assert str(resolved).startswith(str(tmp_path)), (
        f"_db_path() resolved to {resolved}, outside LLM_ROUTER_HOME={tmp_path}"
    )
    assert ".llm-router/usage.db" not in str(resolved) or str(tmp_path) in str(resolved)
