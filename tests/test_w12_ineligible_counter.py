"""W1-2 / UN-08: the ineligible-route counter (``llm_router.ineligible_routes``).

Seeded throwaway ledgers under tmp_path; the real HOME is never read or written.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from llm_router import ineligible_routes as ir

NOW = time.time()
TS = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 3600))

SNAP_OK = {"policy": None, "rows": [
    {"chain_id": "ollama/qwen3", "status": "connected", "kind": "local"},
    {"chain_id": "anthropic/claude-sonnet", "status": "connected", "kind": "subscription"},
]}
# Same inventory, but qwen3 is gone and the anthropic seat is disconnected.
SNAP_GONE = {"policy": None, "rows": [
    {"chain_id": "ollama/other", "status": "connected", "kind": "local"},
    {"chain_id": "anthropic/claude-sonnet", "status": "disconnected", "kind": "subscription"},
]}


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    (tmp_path / ".llm-router").mkdir()
    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE inventory_snapshots (sig TEXT PRIMARY KEY, first_seen_ts TEXT, body_json TEXT)")
    con.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
                "final_model TEXT, final_provider TEXT, reason_code TEXT, classifier_type TEXT, "
                "inventory_sig TEXT, session_id TEXT, is_real INTEGER DEFAULT 1)")
    con.execute("INSERT INTO inventory_snapshots VALUES ('okok', '2026-10-27T00:00:00Z', ?)", (json.dumps(SNAP_OK),))
    con.execute("INSERT INTO inventory_snapshots VALUES ('gone', '2026-10-27T00:00:00Z', ?)", (json.dumps(SNAP_GONE),))
    con.commit()
    con.close()
    return db, tmp_path / "proxy_calls.jsonl"


def _add(db, model, provider, sig, session="s1", real=1, rc="router_x"):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO routing_decisions (timestamp, final_model, final_provider, reason_code, "
                "inventory_sig, session_id, is_real) VALUES (?,?,?,?,?,?,?)",
                (TS, model, provider, rc, sig, session, real))
    con.commit()
    con.close()


def _run(ledgers, **kw):
    db, proxy = ledgers
    return ir.compute(days=1.0, until=NOW, db_path=db, proxy_path=proxy, **kw)


def test_no_planted_rows_is_zero_with_evaluated_rows(ledgers):
    db, _ = ledgers
    for i in range(4):
        _add(db, "ollama/qwen3", "ollama", "okok", session=f"s{i}")
    res = _run(ledgers)
    assert res["evaluated_n"] == 4 and res["ineligible"] == 0 and res["evaluable"]
    assert "0 of 4 evaluable" in ir.render_line(res)


def test_positive_control_planted_ineligible_routes_are_counted(ledgers):
    db, _ = ledgers
    for i in range(4):
        _add(db, "ollama/qwen3", "ollama", "okok", session=f"s{i}")
    _add(db, "ollama/qwen3", "ollama", "gone")            # model absent from its snapshot
    _add(db, "claude-sonnet", "anthropic", "gone")        # seat disconnected in its snapshot
    _add(db, "ollama/qwen3", "ollama", "gone", real=0)    # synthetic population
    res = _run(ledgers)
    assert res["ineligible"] == 3
    assert res["populations"]["untagged"]["ineligible"] == 2  # no kind resolvable: never organic
    assert res["populations"]["synthetic"]["ineligible"] == 1
    assert "3 of 7 evaluable" in ir.render_line(res)
    assert {e["why"].split(":")[0] for e in res["examples"]} >= {"model-level", "provider anthropic map-known, no connected row"}


def test_unstamped_rows_are_not_evaluable_never_eligible(ledgers):
    db, _ = ledgers
    for i in range(3):
        _add(db, "ollama/qwen3", "ollama", None, session=f"s{i}")      # no sig
    _add(db, "ollama/qwen3", "ollama", "deadbeef0000")                 # sig with no snapshot
    res = _run(ledgers)
    assert res["evaluable"] is False and res["evaluated_n"] == 0
    assert res["not_evaluable"] == 4 and res["ineligible"] == 0
    line = ir.render_line(res)
    assert "not evaluable" in line and "4 route(s)" in line and "0 ineligible" not in line


def test_empty_window_and_missing_ledgers_are_not_evaluable(tmp_path):
    res = ir.compute(days=1.0, until=NOW, db_path=tmp_path / "none.db", proxy_path=tmp_path / "none.jsonl")
    assert res["evaluable"] is False and res["rows_read"] == 0
    assert "not evaluable" in ir.render_line(res) and "0 ineligible" not in ir.render_line(res)


def test_cache_and_none_rows_are_not_routes(ledgers):
    db, _ = ledgers
    _add(db, "cache/hit", "cache", "okok")
    _add(db, "none", "none", "okok")
    res = _run(ledgers)
    assert res["evaluated_n"] == 0 and res["not_evaluable"] == 0 and res["evaluable"] is False


def test_proxy_rows_split_by_population_and_informative_bar(ledgers):
    db, proxy = ledgers
    t = NOW - 600
    rows = [{"ts": t, "served_model": "qwen3", "served_by": "ollama", "inventory_sig": "gone",
             "session_id": "coord", "session_kind": "research"},
            {"ts": t, "served_model": "qwen3", "served_by": "ollama", "inventory_sig": "okok",
             "session_id": "org", "session_kind": "organic"},
            {"ts": t, "served_model": "qwen3", "served_by": "ollama", "inventory_sig": None,
             "session_id": "org", "session_kind": "organic"}]
    proxy.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    res = _run(ledgers)
    assert res["populations"]["coordinator"]["ineligible"] == 1
    assert res["populations"]["organic"]["eligible"] == 1
    assert res["populations"]["organic"]["not_evaluable_no_sig"] == 1
    assert res["organic_ineligible"] == 0 and res["informative"] is False
    assert "not informative" in ir.render_line(res)


def test_reads_only_and_never_mutates_the_ledgers(ledgers):
    db, proxy = ledgers
    _add(db, "ollama/qwen3", "ollama", "gone")
    before = db.read_bytes()
    _run(ledgers)
    assert db.read_bytes() == before and not Path(str(db) + "-wal").exists()


def test_kpi_scorecard_carries_the_line(ledgers, monkeypatch):
    from llm_router.commands import kpi
    db, _ = ledgers
    _add(db, "ollama/qwen3", "ollama", "gone")
    monkeypatch.setattr("llm_router.config.get_config",
                        lambda: type("C", (), {"llm_router_db_path": db})())
    card = kpi.compute_scorecard(days=1)
    assert card["ineligible_routes"]["ineligible"] == 1
    assert "ineligible routes (" in kpi.render_scorecard(card)


def test_wal_db_without_shm_is_still_read(tmp_path):
    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE inventory_snapshots (sig TEXT PRIMARY KEY, first_seen_ts TEXT, body_json TEXT)")
    con.execute("INSERT INTO inventory_snapshots VALUES ('okok','x',?)", (json.dumps(SNAP_OK),))
    con.commit()
    con.close()  # a clean close checkpoints and removes -wal / -shm
    assert not Path(str(db) + "-shm").exists()
    snaps, rows, err = ir._read_db(db, NOW - 10, NOW)
    assert err is None and "okok" in snaps
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM inventory_snapshots").fetchone() == (1,)  # nothing written
    con.close()
