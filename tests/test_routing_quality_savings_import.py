"""MCP-routed calls were invisible to every savings_stats reader.

Bug (verified 2026-09-27): `llm(task="research")` and friends append one
`RouteLedgerRecord` per call to `~/.llm-router/routing_quality.jsonl`, carrying
a `saved_usd` estimate — but `llm-router status` and `savings-report` read
ONLY `dashboard_data._JSONL_TABLE` ("savings_stats"), and nothing drained the
North Star ledger into it. `cost.import_savings_log()` only ever drained
`savings_log.jsonl` (the hook/gateway bridge path). So an MCP-routed call's
savings never showed up anywhere — not even as unverified.

`cost.import_routing_quality_ledger()` closes the gap: idempotent (keyed on
`route_id`, backed by `idx_savings_stats_route_id`), read-only against the
source ledger (never truncates it — `routing_quality.summarize()` and Ground
Truth sampling still need every row), `mode` always NULL (the MCP tool has no
"the caller used this answer" signal, so these rows can never read as
realized/verified — savings.REALIZED_MODE == 'block' is unreachable here),
and synthetic/unknown-provenance rows excluded via the SAME
`routing_quality.is_evaluable` gate the rest of the repo already uses.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from llm_router import cost


@pytest.fixture
def rq_env(tmp_path, monkeypatch):
    """Isolated DB + isolated routing_quality ledger, same shape as
    test_ac5_dual_writer.py's savings_env: LLM_ROUTER_DB_PATH picks the DB,
    LLM_ROUTER_HOME (already set by the suite-wide autouse fixture, reset here
    to this test's own tmp_path) resolves routing_quality's default ledger
    path, via `paths.state_path`."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    import llm_router.config as config_module
    config_module._config = None  # re-read env
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))

    from llm_router import routing_quality as rq
    ledger = rq._default_ledger()
    return db, ledger


def _row(
    route_id: str,
    *,
    saved_usd: float = 0.05,
    session_id: str = "sess-1",
    synthetic: bool | None = False,
    schema_version: int = 4,
    ts: float = 1790441507.0,
    task_type: str = "research",
    final_model: str = "ollama/qwen3-coder:30b",
) -> dict:
    row = {
        "schema_version": schema_version,
        "route_id": route_id,
        "route_kind": "completion",
        "task_type": task_type,
        "route_outcome": "success",
        "route_succeeded": True,
        "actual_cost_usd": 0.0,
        "baseline_cost_usd": saved_usd,
        "saved_usd": saved_usd,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "chosen_model": final_model,
        "final_model": final_model,
        "ts": ts,
        "session_id": session_id,
    }
    if synthetic is not None:
        row["synthetic"] = synthetic
    return row


def _write_ledger(ledger, *rows: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _savings_rows(db) -> list[tuple]:
    if not db.exists():
        return []  # nothing was ever imported -> the DB was never created
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT route_id, mode, estimated_claude_cost_saved, host "
            "FROM savings_stats ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# ── 1. a routing_quality row appears (unverified/unmeasured, n=1) ───────────

@pytest.mark.asyncio
async def test_mcp_routed_row_appears_in_savings_stats_unverified(rq_env):
    db, ledger = rq_env
    _write_ledger(ledger, _row("r1", saved_usd=0.05))

    imported = await cost.import_routing_quality_ledger()
    assert imported == 1

    rows = _savings_rows(db)
    assert len(rows) == 1
    route_id, mode, saved, host = rows[0]
    assert route_id == "r1"
    assert saved == pytest.approx(0.05)

    # The figure surfaces through the SAME split every other savings_stats
    # reader uses (dashboard_data._JSONL_TABLE / savings.savings_split_sql).
    from llm_router import dashboard_data
    totals = dashboard_data.query_window("lifetime", db_path=db)
    assert totals.unverified_calls == 1
    assert totals.unverified_saved_usd == pytest.approx(0.05)
    assert totals.saved_usd == pytest.approx(0.0)  # never counted as verified


# ── 2. importing twice gives n=1 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reimport_is_idempotent(rq_env):
    db, ledger = rq_env
    _write_ledger(ledger, _row("r2", saved_usd=0.10))

    first = await cost.import_routing_quality_ledger()
    second = await cost.import_routing_quality_ledger()

    assert first == 1
    assert second == 0            # nothing new the second time
    assert len(_savings_rows(db)) == 1  # n=1, not 2


# ── 3. mode stays NULL; the row never counts as realized ────────────────────

@pytest.mark.asyncio
async def test_imported_row_mode_is_null_and_never_realized(rq_env):
    db, ledger = rq_env
    _write_ledger(ledger, _row("r3", saved_usd=0.20))

    await cost.import_routing_quality_ledger()

    rows = _savings_rows(db)
    assert len(rows) == 1
    _route_id, mode, _saved, _host = rows[0]
    assert mode is None  # never 'block' — no "the caller used this" signal

    from llm_router.savings import VERIFIED_SAVED_SQL, is_verified_saving
    conn = sqlite3.connect(str(db))
    try:
        verified = conn.execute(
            f"SELECT COALESCE(SUM({VERIFIED_SAVED_SQL}), 0) FROM savings_stats"
        ).fetchone()[0]
    finally:
        conn.close()
    assert verified == 0.0
    # Python twin agrees row-for-row (tests/test_a31_router_savings_unverified.py
    # pins the parity for the general case; this pins it for THIS row).
    assert is_verified_saving("routing_quality", "ollama/qwen3-coder:30b",
                               "2026-09-27T00:00:00", None) is False


# ── 4. a synthetic row is not imported ───────────────────────────────────────

@pytest.mark.asyncio
async def test_synthetic_row_is_not_imported(rq_env):
    db, ledger = rq_env
    _write_ledger(ledger, _row("r4", saved_usd=0.30, synthetic=True))

    imported = await cost.import_routing_quality_ledger()
    assert imported == 0
    assert _savings_rows(db) == []


@pytest.mark.asyncio
async def test_unknown_provenance_row_is_not_imported(rq_env):
    """synthetic ABSENT (written before the field existed) is UNKNOWN, and
    `is_evaluable` (routing_quality.py) excludes unknown, not just True — the
    same rule CLAUDE.md documents for this exact ledger."""
    db, ledger = rq_env
    _write_ledger(ledger, _row("r4b", saved_usd=0.30, synthetic=None))

    imported = await cost.import_routing_quality_ledger()
    assert imported == 0
    assert _savings_rows(db) == []


# ── 5. a route already present via the hook/savings_log path is not
#      double-counted ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_already_present_is_not_double_counted(rq_env):
    db, ledger = rq_env

    # Simulate a savings_stats row already recorded under this route_id —
    # whichever writer put it there (the hook/savings_log path never mints a
    # route_id today, so in production this can only be a prior run of this
    # same importer; the dedup mechanism itself must not care which).
    _bootstrap = await cost._get_db()  # create + migrate the schema
    await _bootstrap.close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO savings_stats "
            "(timestamp, session_id, task_type, estimated_claude_cost_saved, "
            "external_cost, model_used, host, route_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-09-27T00:00:00+00:00", "sess-1", "research", 0.05, 0.0,
             "ollama/qwen3-coder:30b", "router", "r5"),
        )
        conn.commit()
    finally:
        conn.close()

    _write_ledger(ledger, _row("r5", saved_usd=0.05))

    imported = await cost.import_routing_quality_ledger()
    assert imported == 0                     # already present -> not re-added
    assert len(_savings_rows(db)) == 1        # still exactly one row for r5
