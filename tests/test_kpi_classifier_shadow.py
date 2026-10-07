"""M1.7: ``llm-router kpi`` reads ``classifier_shadow.jsonl`` into ``classifier_shadow``.

A fixture log of 8 organic verdict rows (2 sessions), 2 organic drops, 3 research
records and 1 record older than the window gives hand-computed values. The line is
informational: it lives outside ``kpis``, so NS, D1, D2, ``--health`` and the
KPI order cannot move.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router import session_kind
from llm_router.commands import kpi
from llm_router.proxy import llm_shadow as ls
from tests import _o3_fixture as fx

NOW = time.time()
A, B, C = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002", \
    "cccccccc-0000-0000-0000-000000000003"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    session_kind._FOUND.clear()
    yield
    session_kind._FOUND.clear()


def _rec(sid, sha, rules, llm, source="llm", ms=1000.0, kind="organic", ts=None, step=None):
    ok = source in ("llm", "cache")
    return {"kind": ls.KIND, "ts": ts if ts is not None else NOW - 3600, "session_id": sid, "text_sha": sha,
            "session_kind": kind, "step_class": step,
            "rules": {"task_type": "code", "complexity": "simple", "tier": rules},
            "llm": {"tier": llm if ok else None, "task_type": "code" if ok else None, "margin": None,
                    "qa": False if ok else None, "needs_repo_context": False if ok else None,
                    "local_eligible": (llm == "local") if ok else None, "derivation": "direct"},
            "source": source, "ms": ms, "tier_reason_live": "policy", "tier_live": rules,
            "requested_tier": "claude-opus-5-5", "policy_version": "abc", "model": "llmr-classifier",
            "prompt_version": "v6"}


def _drop(sid, sha, kind="organic"):
    return {"kind": ls.KIND_DROP, "ts": NOW - 3600, "session_id": sid, "text_sha": sha, "session_kind": kind,
            "step_class": None}


def _fixture():
    return [
        _rec(A, "t1", "haiku", "local", ms=1000),            # local counts as Haiku: agrees with haiku
        _rec(A, "t2", "sonnet", "local", ms=1200),
        _rec(A, "t3", "opus", "opus", ms=1500),
        _rec(B, "t4", "sonnet", "sonnet", ms=1800),
        _rec(B, "t5", None, "haiku", ms=900),                 # the rules had no proposal: not compared
        _rec(B, "t6", "haiku", None, source="timeout", ms=2000),
        _rec(B, "t7", "haiku", None, source="cold", ms=50),
        _rec(A, "t1", "haiku", "sonnet", ms=1100),            # t1 asked again (an earlier call failed upstream of the cache)
        _drop(A, "t8"), _drop(B, "t9"),
        _rec(C, "r1", "haiku", "haiku", kind="research"),
        _rec(C, "r2", "sonnet", "opus", kind="research"),
        _drop(C, "r3", kind="research"),
        _rec(A, "old", "haiku", "haiku", ts=NOW - 60 * 86400),
    ]


def _write(recs):
    fx.write_jsonl(ls.shadow_path(), recs)


def _summary(days=7, **kw):
    allowed = kpi._allowed_kinds(False)
    return kpi._classifier_shadow_summary(days, allowed=allowed, index=session_kind.KindIndex([]), **kw)


def test_fixture_log_gives_the_hand_computed_values():
    _write(_fixture())
    s = _summary()
    assert s["n"] == 8 and s["n_sessions"] == 2
    assert (s["agree"], s["n_compared"]) == (3, 5)
    assert s["llm_tier_dist"] == {"haiku": 1, "local": 2, "opus": 1, "sonnet": 2}
    assert s["rules_tier_dist"] == {"haiku": 2, "none": 1, "opus": 1, "sonnet": 2}
    assert s["cheap_share_llm"] == pytest.approx(0.5)          # (haiku 2 + local 1) / 6 answered
    assert s["fallback_rate"] == pytest.approx(0.25)           # timeout + cold of 8 calls
    assert (s["p50_ms"], s["p95_ms"]) == (1100.0, 1800.0)       # the 6 answered calls: 900 .. 1800
    assert s["drops"] == 2 and s["drop_rate"] == pytest.approx(0.2)
    assert s["calls_per_turn"] == pytest.approx(8 / 7, abs=1e-4)          # 8 calls on 7 distinct (session, text) turns
    assert s["excluded_non_organic"] == 3
    json.dumps(s)


def test_research_sessions_count_only_when_asked():
    _write(_fixture())
    s = kpi._classifier_shadow_summary(7, allowed=kpi._allowed_kinds(True), index=session_kind.KindIndex([]))
    assert s["n"] == 10 and s["n_sessions"] == 3 and s["drops"] == 3 and s["excluded_non_organic"] == 0


def test_a_session_kind_from_the_ledger_beats_a_missing_stamp():
    rec = _rec(A, "t1", "haiku", "haiku", kind=None)
    _write([rec])
    idx = session_kind.KindIndex([{"session_id": A, "session_kind": "organic"}])
    s = kpi._classifier_shadow_summary(7, allowed=kpi._allowed_kinds(False), index=idx)
    assert s["n"] == 1
    # with nothing to say what kind it is, it is never counted as organic
    s0 = kpi._classifier_shadow_summary(7, allowed=kpi._allowed_kinds(False), index=session_kind.KindIndex([]))
    assert s0["n"] == 0 and s0["excluded_non_organic"] == 1


def test_an_absolute_window_cuts_both_ends():
    recs = [_rec(A, f"t{i}", "haiku", "haiku", ts=NOW - 10 * 86400 + i * 3600) for i in range(6)]
    _write(recs)
    win = kpi._Window(NOW - 10 * 86400 + 1 * 3600, NOW - 10 * 86400 + 3 * 3600)
    s = kpi._classifier_shadow_summary(win.days, win, allowed=kpi._allowed_kinds(False),
                                       index=session_kind.KindIndex([]))
    assert s["n"] == 3 and s["calls_per_turn"] == 1.0


def test_no_log_is_zeros_and_nulls_never_a_measured_zero():
    s = _summary()
    assert s["n"] == 0 and s["drops"] == 0
    for k in ("agree_rate", "cheap_share_llm", "fallback_rate", "p50_ms", "p95_ms", "calls_per_turn", "drop_rate"):
        assert s[k] is None
    assert kpi._classifier_shadow_line(s) is None


def test_cls_applied_is_counted_on_the_ledger_rows_it_is_read_from():
    _write(_fixture())
    rows = [{"cls_applied": False}] * 6 + [{"cls_applied": True}, {"x": 1}]
    s = _summary(ledger_rows=rows)
    assert (s["cls_applied_true"], s["ledger_rows"]) == (1, 7)         # the row without the field is not a row of this count
    assert _summary()["cls_applied_true"] is None


def test_the_line_names_n_and_says_too_few_below_50():
    _write(_fixture())
    line = kpi._classifier_shadow_line(_summary())
    assert line.startswith("classifier shadow (proxy): n=8 calls in 2 sessions")
    assert "agree 3/5" in line and "fallback 25.0%" in line and "p50 1100 ms" in line and "p95 1800 ms" in line
    assert "drops 2" in line and "1.14 calls/turn" in line and "too few to tell" in line
    assert "informational, never in NS, D1 or D2" in line


def test_the_scorecard_carries_it_outside_kpis_and_renders_it(monkeypatch):
    _write(_fixture())
    card = kpi.compute_scorecard(7)
    assert card["classifier_shadow"]["n"] == 8
    assert "classifier_shadow" not in card["kpis"] and "classifier_shadow" not in kpi._ORDER
    assert [k for k in card["kpis"]] == list(kpi._ORDER) or set(card["kpis"]) == set(kpi._ORDER)
    json.dumps(card)
    assert "classifier shadow (proxy): n=8" in kpi.render_scorecard(card)
    research = kpi.compute_scorecard(7, include_research=True)
    assert research["classifier_shadow"]["n"] == 10


def test_without_a_log_the_scorecard_has_the_key_and_no_line():
    card = kpi.compute_scorecard(7)
    assert card["classifier_shadow"]["n"] == 0
    assert "classifier shadow" not in kpi.render_scorecard(card)
