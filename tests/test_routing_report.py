"""Part 2 observability — the routing report must summarize the ledger correctly.

Deterministic (builds its own tmp usage.db); no network. Validates the joins that
power `llm_router routing-report`, so a logic regression in the report is caught in CI.
"""
import sqlite3

import pytest

import llm_router.routing_report as R


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HOME", tmp_path)
    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE usage (id INTEGER PRIMARY KEY, model TEXT, provider TEXT, "
        "input_tokens INT, output_tokens INT, latency_ms REAL, saved_usd REAL)"
    )
    con.executemany(
        "INSERT INTO usage (model,provider,input_tokens,output_tokens,latency_ms,saved_usd) VALUES (?,?,?,?,?,?)",
        [
            ("hermes3:8b", "ollama", 100, 50, 6000, 0.01),
            ("hermes3:8b", "ollama", 200, 80, 21000, 0.02),  # >15s slow call
            ("gemini-2.5-flash", "gemini", 120, 20, 1500, 0.001),
        ],
    )
    con.commit()
    con.close()
    # a tiny debug log for the outcome matrix
    (tmp_path / "auto-route-debug.log").write_text(
        "DIRECT SUCCESS x2\nDIRECT SUCCESS\nDIRECT SKIP\nDIRECT FAILED\n")
    (tmp_path / "enforcement.log").write_text("VIOLATION a\nVIOLATION b\n")
    return tmp_path


def test_pctl_basic():
    assert R._pctl([], 50) == 0.0
    assert R._pctl([10, 20, 30], 50) == 20


def test_report_totals_and_tables(fake_home):
    rep = R.generate_report()
    # token totals: 100+200+120 in, 50+80+20 out
    assert "420 in" in rep and "150 out" in rep
    assert "**Routed calls:** 3" in rep
    # saved sum 0.031
    assert "$0.0310" in rep
    # per-model table present
    assert "hermes3:8b" in rep and "gemini-2.5-flash" in rep
    # outcome matrix counts DIRECT SUCCESS occurrences (3) + skip(1)+failed(1)
    assert "DIRECT SUCCESS" in rep
    # slow-call latency note (one call > 15s)
    assert "1 call(s) > 15s" in rep


def test_report_handles_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "HOME", tmp_path)
    assert "nothing has routed" in R.generate_report()
