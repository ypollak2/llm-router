"""Phase 0.2b — remove two inflated counters (audit 2026-09-29, claims C8 and C3).

C8. ``enforce-route.py`` recorded ``verified_used`` / ``door_call`` the moment ANY
``llm_*`` tool was called to release a PreToolUse hold, with no check that the
tool's output was used. 176 of 176 organic ``verified_used`` rows in 30 days
traced to this path, including throwaway calls made only to release the hold.
A door call proves the route was ACKNOWLEDGED, not that its output was USED.

C3. ``agentic/telemetry.py`` persisted a flat ``milestones x $0.20`` "saving"
for every delegation whatever its outcome: 529 rows, $110.20, 91% of the
$121.06 all-time ``savings_stats`` total, all ``model_used =
'llm_router-agentic-router'`` and all with zero token counts.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from llm_router import contracts, dashboard_data, execution_ledger
from llm_router.execution_ledger import (
    LedgerEvent,
    get_route_accounting,
    record_event,
)

ROOT = Path(__file__).resolve().parents[1]
ENFORCE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


enforce = _load(ENFORCE_HOOK, "llm_router_enforce_route_hook_p02b")


# ── C8: door_call is an acknowledgement, not a use ─────────────────────────

def test_route_acknowledged_is_a_declared_realization_status():
    assert "route_acknowledged" in execution_ledger.RealizationStatus.__args__
    assert "route_acknowledged" in contracts.RealizationStatus.__args__


def test_door_call_no_longer_counts_as_realized():
    assert "door_call" not in execution_ledger._COUNTS_AS_REALIZED
    assert "door_call" not in contracts.COUNTS_AS_REALIZED
    # The agent-side marker is untouched.
    assert "agent_marked" in execution_ledger._COUNTS_AS_REALIZED


def test_door_call_is_recorded_as_route_acknowledged(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))

    enforce._record_realization_used("sess-A", {"task_type": "query", "route_id": "r-1"})

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT realization_status, adoption_method, used_by_host FROM execution_events"
    ))
    conn.close()
    assert len(rows) == 1
    assert rows[0]["realization_status"] == "route_acknowledged"
    assert rows[0]["adoption_method"] == "door_call"
    # Whether the host used the output is unknown at door time — never True.
    assert rows[0]["used_by_host"] is None


def _attempt(route_id: str) -> LedgerEvent:
    return LedgerEvent(
        session_id="s-p02b", route_id=route_id, event_type="attempt_completed",
        measured_cost_usd=0.01, baseline_equivalent_cost_usd=0.05,
        host_mode="metered", provider="ollama", input_tokens=100, output_tokens=50,
    )


def _realization(route_id: str, status: str, adoption: str | None) -> LedgerEvent:
    return LedgerEvent(
        session_id="s-p02b", route_id=route_id, event_type="route_realized",
        realization_status=status, adoption_method=adoption,
    )


@pytest.mark.parametrize(
    ("status", "adoption"),
    [
        ("route_acknowledged", "door_call"),  # new writer
        ("verified_used", "door_call"),       # legacy rows written before this fix
        ("verified_used", None),              # legacy pre-Phase-0 NULL-adoption rows
    ],
)
def test_acknowledged_and_legacy_rows_are_not_realized(tmp_path, status, adoption):
    db = tmp_path / "usage.db"
    record_event(_attempt("r"), path=db)
    record_event(_realization("r", status, adoption), path=db)

    acc = get_route_accounting("r", path=db)
    assert acc.potential_savings_usd == pytest.approx(0.04)
    assert acc.realized_savings_usd == 0.0
    assert acc.net_realized_savings_usd <= 0.0
    assert acc.realized_routes == 0
    assert acc.acknowledged_routes == 1
    assert acc.realized_by_adoption_method == {}
    assert acc.realized_quota_tokens_saved == 0


def test_agent_marked_still_counts_as_realized(tmp_path):
    db = tmp_path / "usage.db"
    record_event(_attempt("r"), path=db)
    record_event(_realization("r", "verified_used", "agent_marked"), path=db)

    acc = get_route_accounting("r", path=db)
    assert acc.realized_savings_usd == pytest.approx(0.04)
    assert acc.realized_routes == 1
    assert acc.acknowledged_routes == 0


def test_dashboard_realized_surface_excludes_door_calls(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    record_event(_attempt("r"), path=db)
    record_event(_realization("r", "verified_used", "door_call"), path=db)

    t = dashboard_data.query_realized_savings("lifetime", db_path=db)
    assert t.realized_savings_usd == 0.0
    assert t.realized_routes == 0
    assert t.acknowledged_routes == 1


# ── C3: flat per-turn agentic credits ──────────────────────────────────────

_FLAT_RESULT = {
    "outcome": "complete",
    "task_type": "code",
    # What compute_savings produces today: milestones x $0.20, no tokens.
    "savings": {"actual_usd": 0.0, "baseline_usd": 0.6, "saved_usd": 0.6},
}


def _savings_rows(db: Path) -> list[tuple]:
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT estimated_claude_cost_saved, model_used, input_tokens, output_tokens "
            "FROM savings_stats"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


async def test_flat_credit_without_token_counts_is_not_persisted(tmp_path, monkeypatch):
    from llm_router.agentic.telemetry import record_delegation_savings

    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    payload = await record_delegation_savings(_FLAT_RESULT)
    assert _savings_rows(db) == []
    assert payload["saved_usd"] == 0.0
    assert payload["persisted"] is False


async def test_unfinished_delegation_is_not_persisted_even_with_tokens(tmp_path, monkeypatch):
    from llm_router.agentic.telemetry import record_delegation_savings

    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    result = dict(_FLAT_RESULT, outcome="surfaced",
                  usage={"input_tokens": 10_000, "output_tokens": 2_000})
    await record_delegation_savings(result)
    assert _savings_rows(db) == []


async def test_measured_delegation_is_priced_from_its_tokens(tmp_path, monkeypatch):
    from llm_router import pricing
    from llm_router.agentic.telemetry import MEASURED_MODEL, record_delegation_savings

    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    result = dict(_FLAT_RESULT, usage={"input_tokens": 10_000, "output_tokens": 2_000})
    result["savings"] = {"actual_usd": 0.01, "saved_usd": 0.6}
    await record_delegation_savings(result)

    expected = pricing.cost_usd(pricing.savings_baseline_model(), 10_000, 2_000) - 0.01
    rows = _savings_rows(db)
    assert len(rows) == 1
    saved, model, in_tok, out_tok = rows[0]
    assert saved == pytest.approx(expected)
    assert saved != pytest.approx(0.6)
    assert model == MEASURED_MODEL != "llm_router-agentic-router"
    assert (in_tok, out_tok) == (10_000, 2_000)


_DDL = """CREATE TABLE savings_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, session_id TEXT, task_type TEXT,
    estimated_claude_cost_saved REAL NOT NULL, external_cost REAL DEFAULT 0,
    model_used TEXT, host TEXT, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, mode TEXT, is_simulated INTEGER)"""


def test_legacy_flat_agentic_rows_are_filtered_at_read_time(tmp_path):
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute(_DDL)
    now = "2026-09-28 20:17:32"
    conn.executemany(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, "
        "estimated_claude_cost_saved, model_used, host, is_simulated) "
        "VALUES (?, 's', 'code', ?, ?, ?, 0)",
        [
            (now, 1.0, "llm_router-agentic-router", "agentic"),
            (now, 0.25, "ollama/qwen3.5:latest", "router"),
        ],
    )
    conn.commit()
    conn.close()

    s = dashboard_data.summary("lifetime", db_path=db)
    assert s.estimated_usd == pytest.approx(0.25)
    assert s.estimated_n == 1
    assert "llm_router-agentic-router" not in s.by_model

    # Read-time filter only: the row itself is untouched.
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*), SUM(estimated_claude_cost_saved) FROM savings_stats "
        "WHERE model_used = 'llm_router-agentic-router'"
    ).fetchone() == (1, 1.0)
    conn.close()


def test_surface_status_twin_drops_legacy_flat_agentic_rows():
    from llm_router.savings import is_excluded_saving

    assert is_excluded_saving("llm_router-agentic-router") is True
    assert is_excluded_saving("ollama/qwen3.5:latest") is False
    assert is_excluded_saving(None) is False


@pytest.mark.parametrize(
    "model",
    ["llm_router-agentic-router", "LLM_Router-Agentic-Router",
     "llm_router-agentic-measured", "ollama/qwen3.5:latest", None],
)
def test_excluded_twin_agrees_with_the_sql(model):
    from llm_router.savings import EXCLUDED_SAVINGS_PRED_SQL, is_excluded_saving

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE savings_stats (model_used TEXT)")
    conn.execute("INSERT INTO savings_stats VALUES (?)", (model,))
    (sql,) = conn.execute(
        f"SELECT CASE WHEN {EXCLUDED_SAVINGS_PRED_SQL} THEN 1 ELSE 0 END FROM savings_stats"
    ).fetchone()
    conn.close()
    assert bool(sql) == is_excluded_saving(model)
