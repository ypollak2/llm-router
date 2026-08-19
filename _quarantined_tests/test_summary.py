"""Tests for llm_router.observability.summary.

Offline: builds a synthetic usage.db and asserts the collected model + the
Markdown render. No rich required (render_markdown is pure text).
"""

from __future__ import annotations

import sqlite3

import pytest

from llm_router.observability import summary as sm


def _make_usage_db(path, rows):
    """rows: list of (timestamp, model, provider, task_type, cost, latency, success,
    potential, saved)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp, model, "
        "provider, task_type, cost_usd, latency_ms, success, potential_cost_usd, saved_usd)"
    )
    conn.executemany(
        "INSERT INTO usage (timestamp, model, provider, task_type, cost_usd, latency_ms, "
        "success, potential_cost_usd, saved_usd) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def usage_db(tmp_path):
    db = tmp_path / "usage.db"
    _make_usage_db(db, [
        # ts, model, provider, task, cost, latency, success, potential, saved
        (1000.0, "hermes3:8b", "ollama", "code", 0.0, 800, 1, 0.05, 0.05),
        (1010.0, "qwen2.5-coder:7b", "ollama", "query", 0.0, 400, 1, 0.02, 0.02),
        (1020.0, "gemini-2.5-flash", "gemini", "generate", 0.001, 1200, 1, 0.03, 0.029),
        (1030.0, "gpt-4o", "openai", "analyze", 0.02, 2500, 1, 0.06, 0.04),
        (1040.0, "claude-opus", "anthropic", "code", 0.15, 3000, 0, 0.15, 0.0),
    ])
    return db


def test_empty_db_is_soft(tmp_path):
    data = sm.collect(db_path=str(tmp_path / "nope.db"))
    assert data.total_decisions == 0
    md = sm.render_markdown(data)
    assert "No routing activity" in md


def test_collect_aggregates(usage_db):
    d = sm.collect(db_path=str(usage_db))
    assert d.total_decisions == 5
    assert d.total_cost_usd == pytest.approx(0.171)
    assert d.savings_usd == pytest.approx(0.139)
    assert d.baseline_cost_usd == pytest.approx(0.31)
    assert d.success_count == 4 and d.fail_count == 1
    # tiers: 2 local (ollama), 1 cheap (gemini), 1 mid (openai), 1 premium (anthropic)
    assert d.tier_counts["local"] == 2
    assert d.tier_counts["cheap"] == 1
    assert d.tier_counts["mid"] == 1
    assert d.tier_counts["premium"] == 1
    # duration spans first→last timestamp
    assert d.duration_seconds == pytest.approx(40.0)
    # latency percentiles populated
    assert d.latency_p50_ms > 0 and d.latency_p95_ms >= d.latency_p50_ms


def test_savings_pct(usage_db):
    d = sm.collect(db_path=str(usage_db))
    # (0.31 - 0.171) / 0.31 ≈ 44.8%
    assert d.savings_pct == pytest.approx(44.8, abs=0.2)


def test_render_markdown_sections(usage_db):
    d = sm.collect(db_path=str(usage_db))
    md = sm.render_markdown(d)
    assert "session summary" in md
    assert "Tier mix" in md
    assert "Top routes" in md
    assert "Providers" in md
    assert "Latency" in md
    assert "ollama" in md
    assert "chuzom" not in md.lower()   # no brand leak
    # health glyph present
    assert any(g in md for g in ("🟢", "🟡", "🔴"))


def test_top_routes_sorted(usage_db):
    d = sm.collect(db_path=str(usage_db))
    # 'code' task appears twice (hermes3, claude-opus) but as distinct (task,model)
    counts = [n for _, _, n in d.top_routes]
    assert counts == sorted(counts, reverse=True)


def test_render_does_not_raise(usage_db, capsys):
    """render() must work whether or not rich is installed (falls back to markdown)."""
    d = sm.collect(db_path=str(usage_db))
    sm.render(d)  # must not raise
    out = capsys.readouterr().out
    assert "llm-router" in out


def test_since_seconds_filter(usage_db, monkeypatch):
    import time as _t
    # freeze now so only the last rows survive a tiny window
    monkeypatch.setattr(_t, "time", lambda: 1040.0)
    d = sm.collect(db_path=str(usage_db), since_seconds=15)  # keep ts >= 1025
    assert d.total_decisions == 2  # ts 1030, 1040
