"""P35-4 (D-220): the session summary (``session_report``) and where it renders.

Evidence: ``v16/coord/P35-PLAN.md`` (Design, Session summary; PR row P35-4a/4b) and the rv-628
carry-forward. The fixture-home markers live in tests/test_p35_fixtures.py; this file holds the pure
oracles (targets, offload, quota saved, recommendation) and the n + window rule over the printed block.
P35-4b adds where the block renders (the session-end child and ``llm-router summary --session``).
"""
from __future__ import annotations

import json
import re

import pytest

from tests.test_p34a_ledger_view_identity import DAY, NOW, home  # noqa: F401  (home is a fixture)
from tests.test_p34a_six_surfaces import six  # noqa: F401  (six is a fixture)
from tests.test_p35_fixtures import _history, _jl, p35  # noqa: F401  (p35 is a fixture)
from tests.test_p35_fixtures import PP_PER_ACTIVE_HOUR, SID


def _act(cls: str, *, numerator: bool = False, sampled: bool = False, first: bool = False,
         door: str = "proxy", group: str = "code", redone: bool = False) -> dict:
    return {"ts": NOW - 60, "session_id": SID, "door": door, "task_group": group, "served_class": cls,
            "first": first, "redone": redone, "numerator": numerator, "sampled": sampled}


# -- targets and offload (pure) ---------------------------------------------------------------------

def test_targets_count_spread_in_n_never_in_the_numerator():
    from llm_router import session_report as sr

    acts = [_act("local"), _act("haiku"), _act("very_cheap"), _act("paid"), _act("spread"), _act("spread")]
    t = sr.targets_field(acts, "session")
    assert (t["local"], t["cheap"], t["frontier"], t["spread"], t["n"]) == (1, 2, 1, 2, 6), t
    assert t["offload_numerator"] == 3, t                      # spread (2) is not in it
    assert sr.targets_field(None, "session")["n"] is None       # no action ledger: unknown, not 0


def test_offload_is_the_s1_numerator_and_unknown_without_a_sampled_lane():
    from llm_router import session_report as sr

    unsampled = sr.offload_field([_act("local", numerator=False)] * 3, "session")
    assert unsampled["value"] is None and unsampled["n"] == 3 and unsampled["na_reason"] == "no quality-sampled lane"
    acts = [_act("local", numerator=True, sampled=True), _act("spread", sampled=True), _act("paid", sampled=True),
            _act("local", sampled=True, redone=True)]
    o = sr.offload_field(acts, "session")
    assert (o["k"], o["n"], o["value"]) == (1, 4, 0.25), o
    assert sr.offload_field([], "session")["na_reason"] == "no action"


# -- quota saved ~ (units) ----------------------------------------------------------------------------

def test_quota_saved_is_turns_off_claude_times_burn_with_both_ns(home, monkeypatch):  # noqa: F811
    """3 turns off Claude x 0.5 pp/turn = ~1.5% of window; / median 3.0 pp per active hour = ~0.5 h."""
    from llm_router import quota_samples
    from llm_router import session_report as sr

    _jl(home / "quota_history.jsonl", _history(PP_PER_ACTIVE_HOUR, NOW - 5 * DAY))
    monkeypatch.setattr(quota_samples, "quota_burn", lambda since, until, **kw: {
        "measured": {"weekly_per_turn": 0.5, "n_turns": 40, "n_sessions": 6}})
    acts = [_act("local", first=True), _act("very_cheap", first=True), _act("spread", first=True),
            _act("local", first=True, redone=True),               # redone: Claude answered it again
            _act("haiku", first=True),                            # Haiku is a Claude call
            _act("local", first=False),                           # a continuation, not a turn
            _act("local", first=True, door="mcp")]                # inside a Claude turn
    assert sr.turns_off_claude(acts) == 3
    q = sr.quota_saved_field(acts, "session", NOW)
    r = {"window": "session", "label": "session s-main", "as_of": "t", "targets": sr.targets_field([], "session"),
         "offload": sr.offload_field([], "session"), "escalations": {"n": 0, "of": 0, "na_reason": None},
         "cache_hit_rate": {"value": None, "n": None, "na_reason": "no lookup recorded"}, "quota_saved": q,
         "quality": None, "judge": None, "recommendation": "no recommendation (n/a: x)"}
    line = next(ln for ln in sr.render(r).splitlines() if "quota saved" in ln)
    n_rate = len(PP_PER_ACTIVE_HOUR)
    assert line.startswith(f"  quota saved ~1.5% of window (n=3, rate n=40, session) · "
                           f"~0.5 work-hours (n=3, rate n={n_rate}, session)"), line


def test_quota_saved_with_no_turn_off_claude_is_na_never_zero(home, monkeypatch):  # noqa: F811
    from llm_router import quota_samples
    from llm_router import session_report as sr

    monkeypatch.setattr(quota_samples, "quota_burn", lambda since, until, **kw: {
        "measured": {"weekly_per_turn": 0.5, "n_turns": 40, "n_sessions": 6}})
    q = sr.quota_saved_field([_act("paid", first=True)], "session", NOW)
    assert q["pct"]["value"] is None and q["pct"]["na_reason"] == "n=0 turns off Claude" and q["turns"] == 0


# -- recommendation: computed from the fields, never a constant -----------------------------------

def _quality(k: int, n: int, fk: int, fn: int) -> dict:
    return {"value": k / n, "k": k, "n": n, "frontier_k": fk, "frontier_n": fn, "window": "7d"}


def test_recommendation_follows_the_eligibility_rule_on_the_fields():
    from llm_router import session_report as sr
    from llm_router.lane_quality import PAIRS_MIN_N

    acts = [_act("local", group="code")] * 3 + [_act("paid", group="qa")]
    ok = sr.recommend(acts, _quality(61, 62, 62, 62))            # 1.6 pts behind Frontier, n=62: eligible
    bad = sr.recommend(acts, _quality(40, 62, 62, 62))           # 35.5 pts behind: failed
    assert ok == ("~offload more code work (75% of this session's actions, n=4): the cheap answer is within "
                  "1.6 pts of Frontier (n=62, 7d)"), ok
    assert bad == ("~keep code work on Frontier (75% of this session's actions, n=4): the cheap answer trails "
                   "Frontier by 35.5 pts (n=62, 7d)"), bad
    qa = sr.recommend([_act("paid", group="qa")] * 2 + acts[:1], _quality(61, 62, 62, 62))
    assert qa.startswith("~offload more Q&A work (67% of this session's actions, n=3)"), qa
    assert sr.recommend(acts, _quality(9, 10, 10, 10)) == \
        f"no recommendation (n/a: quality vs Frontier n=10 < {PAIRS_MIN_N}, 7d)"
    assert sr.recommend([], _quality(61, 62, 62, 62)) == "no recommendation (n/a: no action in the session)"
    assert sr.recommend(acts, None) == "no recommendation (n/a: quality vs Frontier, n=0, 7d)"


async def test_build_recommends_from_seeded_verdicts(p35):  # noqa: F811
    """End to end: 62 decided verdicts, 61 acceptable, Frontier 62: the build computes a recommendation."""
    from llm_router import session_report

    rows = [{"ts": NOW - (i + 1) * 600, "task_id": f"v{i}", "acceptable": i > 0, "frontier_acceptable": True,
             "cannot_judge": False} for i in range(62)]
    (p35.home / "shadow_frontier" / "verdicts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    rec = session_report.build(SID)["recommendation"]
    assert rec.startswith("~offload more ") and "within 1.6 pts of Frontier (n=62, 7d)" in rec, rec


# -- the printed block: n + window on every figure ----------------------------------------------------

async def test_every_printed_figure_carries_n_and_window(p35):  # noqa: F811
    from llm_router import session_report

    text = session_report.render(session_report.build(SID))
    lines = text.splitlines()
    assert lines[0].startswith(session_report.MARKER) and len(lines) == 9, text
    for ln in lines[1:-1]:                                     # every figure line (not header, not the advice)
        assert re.search(r"\bn=(\d|n/a)", ln), ln
        assert re.search(r"\b(session|7d|14d)\b", ln), ln
    assert "escalations 2 (n=10 proxy calls, session" in text, text
    assert "judge 4.00 (n=5, 14d)" in text, text               # judge_results.jsonl, no cache yet: computed
    assert "quota saved n/a (n=0 turns off Claude, session)" in text, text
    assert " 0%" not in text                                   # unknown is n/a, never a 0 rate


async def test_scope_window_reads_the_window_not_the_session(p35):  # noqa: F811
    from llm_router import session_report

    r = session_report.build(scope="24h")
    assert r["window"] == "24h" and r["session_id"] is None
    assert r["escalations"]["n"] == 2 and r["escalations"]["window"] == "24h", r["escalations"]


@pytest.mark.parametrize("bad", [None, []])
def test_offload_and_targets_never_print_zero_for_unknown(bad):
    from llm_router import session_report as sr

    o, t = sr.offload_field(bad, "session"), sr.targets_field(bad, "session")
    assert o["value"] is None and (t["n"] is None) == (bad is None)
