"""W8 (owner decision D-364): NS stays STRICT, and reads BLIND while no verifier verdict reaches it.

The strict numerator (a non-Claude model served it AND a verifier passed it) reads a unit's ``verify``
record, and nothing attaches one: so "NS 0.0%" was a structural zero, not a measurement
(``docs/bugs/NSZERO-1.md``). A rate whose numerator cannot be observed is blind, with its 0/n beside the
word; every surface that reads the scorecard says the same.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router import northstar as ns
from llm_router import session_kind, statusline_refresh as sr, statusline_tick as tick
from llm_router.commands import kpi

LOCAL = "ollama/qwen3-coder:30b"
BLIND80 = "blind: strict 0/80 (no verifier verdict reaches NS)"


@pytest.fixture
def organic(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    yield "s-org"
    session_kind._FOUND.clear()


def _unit(**kw):
    base = dict(kind=ns.UNIT_ROUTED_MCP, session_id="s-org", ts=time.time(), model=LOCAL, task_type="code",
                outcome=ns.OUTCOME_UNKNOWN)
    base.update(kw)
    return ns.Unit(**base)


def _stream(monkeypatch, units):
    rows = []
    for u in units:
        row = u.to_dict()
        row.pop("session_kind"), row.pop("session_kind_source")
        rows.append(row)
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False, **_: iter(rows))


def _card(**kw):
    return kpi.compute_scorecard(days=7, **kw)


def _verdict(status):
    return {"verify_status": status}


def _eighty(*extra):
    return [_unit(outcome=ns.OUTCOME_USED) for _ in range(80 - len(extra))] + list(extra)


# ── the rule ─────────────────────────────────────────────────────────────────────────────────────

def test_ns_with_no_verdict_among_its_units_is_blind_and_keeps_its_zero_over_n(monkeypatch, organic):
    _stream(monkeypatch, _eighty())
    r = _card()["kpis"]["NS"]
    assert r["value"] == BLIND80
    assert (r["measurable"], r["blind"], r["numerator"], r["denominator"], r["n"]) == (False, True, 0, 80, 80)
    assert r["reason"] == ns.BLIND_REASON and "D-364" in r["reason"]
    assert "%" not in r["value"]                         # no rate is printed for a numerator nobody can see


def test_d2_has_the_same_strict_numerator_and_is_blind_for_the_same_reason(monkeypatch, organic):
    _stream(monkeypatch, _eighty())
    k = _card()["kpis"]
    assert k["D2"]["value"] == "blind: strict 0/80 (no verifier verdict reaches NS)"
    assert k["D1"]["value"] == "100.0% (n=80)"           # D1 reads no verdict: a measured rate stays a rate


@pytest.mark.parametrize("status", ["pass_f2p", "pass_f2p_model", "pass_p2p", "fail"])
def test_a_verdict_makes_ns_measured(monkeypatch, organic, status):
    """pass_p2p and fail are verdicts too: the verifier looked, and said this unit does not count."""
    _stream(monkeypatch, _eighty(_unit(verify=_verdict(status))))
    r = _card()["kpis"]["NS"]
    counted = 1 if status in ns.STRICT_VERIFY else 0
    assert r["measurable"] is True and "blind" not in r
    assert r["numerator"] == counted and r["denominator"] == 80
    assert r["value"] == f"{counted / 80 * 100:.1f}% (n=80)"


@pytest.mark.parametrize("status", ["unavailable", "not_applicable"])
def test_a_verifier_that_could_not_judge_is_no_verdict(monkeypatch, organic, status):
    _stream(monkeypatch, _eighty(_unit(verify=_verdict(status))))
    assert _card()["kpis"]["NS"]["value"] == BLIND80


def test_a_verdict_on_a_unit_outside_the_counted_population_does_not_unblind_ns(monkeypatch, organic):
    """The verdict must be among the units NS counts (organic): a research session's pass is not evidence here."""
    session_kind.tag_session("s-res", "/Users/someone/work/scratchpad/p1", env={})
    _stream(monkeypatch, _eighty() + [_unit(session_id="s-res", verify=_verdict("pass_f2p"))])
    assert _card()["kpis"]["NS"]["value"] == BLIND80
    assert _card(include_research=True)["kpis"]["NS"]["measurable"] is True


def test_too_few_and_not_measurable_keep_their_own_words(monkeypatch, organic):
    _stream(monkeypatch, [_unit() for _ in range(kpi.MIN_N - 1)])
    assert _card()["kpis"]["NS"]["value"] == f"too few to tell (n={kpi.MIN_N - 1})"
    _stream(monkeypatch, [])
    assert _card()["kpis"]["NS"]["value"] == "not measurable: no unit in window"


def test_health_marks_ns_blind_with_the_reason_and_its_n(monkeypatch, organic):
    _stream(monkeypatch, _eighty())
    health = kpi.compute_health(_card())
    assert health["kpis"]["NS"]["state"] == kpi.STATE_BLIND and health["kpis"]["NS"]["n"] == 80
    assert "no verifier verdict reaches" in health["kpis"]["NS"]["reason"] or "verifier" in health["kpis"]["NS"]["reason"]


def test_the_scorecard_text_says_blind_and_stays_json_serialisable(monkeypatch, organic):
    _stream(monkeypatch, _eighty())
    card = _card()
    json.dumps(card, default=str)
    assert any(ln.startswith("  NS ") and "blind: strict 0/80" in ln for ln in kpi.render_scorecard(card).splitlines())


def test_ledger_view_ns_strict_returns_the_same_blind_result(monkeypatch, organic):
    from llm_router import ledger_view as lv

    _stream(monkeypatch, _eighty())
    assert lv.ns_strict(7)["value"] == BLIND80


# ── the seam ─────────────────────────────────────────────────────────────────────────────────────

def test_the_seam_is_off_until_a_verifier_is_wired():
    assert ns.NS_ATTACH_VERIFY_RECORDS is False


def test_flipping_the_seam_attaches_verify_records_and_ns_is_measured_from_them(tmp_path, monkeypatch):
    """The whole wiring, end to end through the real units() and the real ledger: with the seam on, a
    pass_f2p record on a Codex handoff reaches the strict numerator; with it off the same ledger is blind."""
    from tests.test_verify_record import _append, _codex, _scorecard, _setup, _units, _vrow

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude_projects"))
    bulk = [{"ts": 1_800_000_200.0 + i, "lever": "agent_route_codex", "model": "codex/gpt-5.5", "outcome": "delegated",
             "task_type": "code", "session_id": "aaaaaaaa-1111-2222-3333-444444444444", "session_kind": "organic"}
            for i in range(60)]
    p, proj = _setup(tmp_path, bulk)
    a, _ = _codex(_units(proj))[:2]
    _append(p, _vrow(a["unit_id"], "pass_f2p"))
    off = _scorecard()["kpis"]["NS"]
    assert off["blind"] is True and off["numerator"] == 0
    monkeypatch.setattr(ns, "NS_ATTACH_VERIFY_RECORDS", True)
    on = _scorecard()["kpis"]["NS"]
    assert on["measurable"] is True and on["numerator"] == 1 and "blind" not in on, on


# ── the surfaces agree ───────────────────────────────────────────────────────────────────────────

def test_the_cache_carries_the_blind_ns_and_the_tick_prints_it_not_as_zero_percent(monkeypatch, organic):
    _stream(monkeypatch, _eighty())
    data = sr.build(time.time())
    assert data["ns"] == {"pct": None, "n": 80, "window": "7d", "blind": True, "k": 0}
    line = tick.render(data, now=time.time())
    assert "NS strict 0/80 (blind) 7d units" in line
    assert "NS 0" not in line and "0.0%" not in line


@pytest.mark.parametrize("ns_cache,expect", [
    ({"pct": None, "n": 2097, "window": "7d", "blind": True, "k": 0}, "NS strict 0/2097 (blind) 7d units"),
    ({"pct": None, "n": 2097, "blind": True, "k": 0}, "NS strict 0/2097 (blind) units"),            # no window known
    ({"pct": None, "n": 2097, "window": "bad;rm", "blind": True, "k": 0}, "NS strict 0/2097 (blind) units"),
    ({"pct": None, "n": 0, "window": "7d", "blind": True, "k": 0}, "NS n/a"),                      # nothing counted
    ({"pct": None, "n": 12, "window": "7d", "blind": True, "k": 40}, "NS n/a n=12"),                # k > n is not a count
    ({"pct": 3.0, "n": 100, "window": "7d"}, "NS 3.0% n=100"),                                      # a measured NS, as before
    ({"pct": 0.0, "n": 3599}, "NS 0.0% n=3599"),                                                    # an old cache, as before
])
def test_the_tick_segment_for_each_ns_cache_shape(ns_cache, expect):
    cache = {"v": 1, "written_at": 1_791_234_000.0 - 5, "mode": "smart", "ns": ns_cache}
    vis = tick._ANSI.sub("", tick.render(cache, now=1_791_234_000.0))
    assert f" · {expect} · " in vis + " · ", vis


def test_the_cache_never_carries_a_share_for_a_blind_ns():
    """``pct`` stays None for a blind NS, so a tick that predates the blind marker (it reads ``pct`` and ``n``
    only) prints ``NS n/a n=2097``: honest, and never a 0.0% it cannot back."""
    card = {"window_days": 7, "kpis": {"NS": {"value": "blind", "n": 2097, "measurable": False, "blind": True,
                                              "numerator": 0, "denominator": 2097}}}
    cached = sr._ns(card)
    assert cached["pct"] is None and cached["k"] == 0 and cached["blind"] is True
    older = {"v": 1, "written_at": 1_791_234_000.0 - 5, "mode": "smart", "ns": {"pct": cached["pct"], "n": cached["n"]}}
    assert " · NS n/a n=2097 · " in tick.render(older, now=1_791_234_000.0) + " · "
