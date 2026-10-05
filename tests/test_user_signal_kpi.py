"""Owner rule (2026-10-05): a KEEP press is never "used".

"Used" in the North Star requires a passing test. So the receipt band's keep
(``user_kept``) is shown on its own line and moves nothing: not NS, D1, D2, and
not D3's denominator. A redo (``user_redone``) is a clear negative and joins D3
as a decided redo event. These tests fail if keep ever raises NS, D1 or D2.
"""

from __future__ import annotations

import time

import pytest

from llm_router import northstar as ns
from llm_router import session_kind, usage_outcome, user_signal
from llm_router.commands import kpi


@pytest.fixture(autouse=True)
def _isolated_readers(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    yield
    session_kind._FOUND.clear()


def _units(monkeypatch, used: int, redo: int, not_routed: int):
    attempted = sorted(ns.ATTEMPTED_KINDS)[0]
    now = time.time()
    rows = ([{"session_id": "s-org", "kind": attempted, "outcome": ns.OUTCOME_USED, "lever": None, "ts": now}] * used
            + [{"session_id": "s-org", "kind": attempted, "outcome": ns.OUTCOME_REDO, "lever": None, "ts": now}] * redo
            + [{"session_id": "s-org", "kind": "claude_only", "outcome": ns.OUTCOME_NOT_ROUTED, "lever": None,
                "ts": now}] * not_routed)
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))


def _d3_rows(monkeypatch, used: int, redone: int):
    rows = ([{"outcome": usage_outcome.OUTCOME_USED, "session_kind": "organic"}] * used
            + [{"outcome": usage_outcome.OUTCOME_REDONE, "session_kind": "organic"}] * redone)
    monkeypatch.setattr(usage_outcome, "judge_recent", lambda days=7, root=None: rows)


def _card():
    return kpi.compute_scorecard(days=7)


def _press(n: int, signal: str, prefix: str):
    for i in range(n):
        user_signal.record(f"{prefix}{i}", signal, "terminal")


def test_keep_never_raises_ns_d1_d2(monkeypatch):
    _units(monkeypatch, used=20, redo=20, not_routed=40)
    _d3_rows(monkeypatch, used=45, redone=15)
    before = _card()["kpis"]
    assert before["NS"]["value"] == "25.0% (n=80)"          # the fixture is measurable
    _press(500, "kept", "k")
    after = _card()["kpis"]
    for key in ("NS", "D1", "D2"):
        assert after[key] == before[key], key
    # keep is not in D3's denominator either
    assert after["D3"]["value"] == before["D3"]["value"] == "25.0% (n=60)"
    assert after["D3"]["user_kept"] == 500


def test_redo_reaches_d3_as_decided_events(monkeypatch):
    _units(monkeypatch, used=20, redo=20, not_routed=40)
    _d3_rows(monkeypatch, used=45, redone=15)
    _press(20, "redone", "r")
    _press(100, "kept", "k")                                 # keeps never join the denominator
    d3 = _card()["kpis"]["D3"]
    assert d3["value"] == "43.8% (n=80)"                     # (15 + 20) / (45 + 15 + 20)
    assert d3["user_redone"] == 20 and d3["redone"] == 35 and d3["used"] == 45


def test_redo_alone_makes_d3_countable(monkeypatch):
    _d3_rows(monkeypatch, used=0, redone=0)
    assert _card()["kpis"]["D3"]["value"].startswith("not measurable")
    _press(kpi.MIN_N, "redone", "r")
    assert _card()["kpis"]["D3"]["value"] == f"100.0% (n={kpi.MIN_N})"


def test_kpi_shows_user_kept_and_user_redone_lines_with_n(monkeypatch):
    _d3_rows(monkeypatch, used=45, redone=15)
    _press(3, "kept", "k")
    _press(2, "redone", "r")
    text = kpi.render_scorecard(_card())
    assert "user_kept n=3 " in text and "never counted as used" in text
    assert "user_redone n=2 " in text and "counted in D3" in text


def test_lines_present_with_no_presses_at_all(monkeypatch):
    _d3_rows(monkeypatch, used=0, redone=0)
    text = kpi.render_scorecard(_card())
    assert "user_kept n=0 " in text and "user_redone n=0 " in text
