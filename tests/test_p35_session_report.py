"""P35-4 (D-220): the session summary (``session_report``) and where it renders.

Evidence: ``v16/coord/P35-PLAN.md`` (Design, Session summary; PR row P35-4a/4b) and the rv-628
carry-forward. The fixture-home markers live in tests/test_p35_fixtures.py; this file holds the pure
oracles (targets, offload, quota saved, recommendation), the n + window rule over the printed block, and
the session-end child (the Stop's own line never gains the block; the hook pair is v49).
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_p34a_ledger_view_identity import DAY, NOW, home  # noqa: F401  (home is a fixture)
from tests.test_p34a_six_surfaces import HOOKS, six  # noqa: F401  (six is a fixture)
from tests.test_p35_fixtures import _history, _jl, p35  # noqa: F401  (p35 is a fixture)
from tests.test_p35_fixtures import PP_PER_ACTIVE_HOUR, SID

ROOT = Path(__file__).resolve().parents[1]
HOOK = HOOKS / "session-end.py"


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


async def test_cli_summary_session_prints_the_block(p35):  # noqa: F811
    env = {**p35.env, "LLM_ROUTER_DB_PATH": str(p35.db), "LLM_ROUTER_NO_DETACHED_SPAWN": "1"}
    r = subprocess.run([sys.executable, "-m", "llm_router.cli", "summary", "--session", SID], env=env,
                       capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "session summary (P35-4)" in r.stdout and "escalations 2 (n=10 proxy calls, session" in r.stdout, r.stdout


# -- session-end: the child renders it, the Stop line does not change -------------------------------

def _hook():
    import importlib.util

    spec = importlib.util.spec_from_file_location("session_end_p354", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_the_child_appends_one_block_to_last_summary(p35, monkeypatch):  # noqa: F811
    from llm_router import session_report

    mod = _hook()
    monkeypatch.setattr(mod, "_stop_hook_mode", lambda: "condensed")
    mod._STOP_SESSION_ID = SID
    (p35.home / "last_summary.ansi").write_text("BOX\n")
    mod._render_session_report()
    mod._render_session_report()                               # a second child: still one block
    text = (p35.home / "last_summary.ansi").read_text()
    assert text.startswith("BOX\n\n" + session_report.MARKER) and text.count(session_report.MARKER) == 1, text
    assert "escalations 2 (n=10 proxy calls, session" in text
    monkeypatch.setattr(mod, "_stop_hook_mode", lambda: "full")
    (p35.home / "last_summary.ansi").write_text("FULL\n")
    mod._render_session_report()                               # full mode: the Stop owns the file
    assert (p35.home / "last_summary.ansi").read_text() == "FULL\n"


def test_the_block_is_a_child_step_and_not_on_the_stop_path():
    mod = _hook()
    steps = mod._STOP_BACKGROUND_STEPS
    assert steps[steps.index("_render_last_summary") + 1] == "_render_session_report"
    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    names = {n.id for n in ast.walk(main) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(main) if isinstance(n, ast.Attribute)} | \
        {a.name for n in ast.walk(main) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert names and "session_report" not in names and "_render_session_report" not in names


async def test_the_stop_line_does_not_carry_the_block(p35):  # noqa: F811
    env = {**p35.env, "LLM_ROUTER_DB_PATH": str(p35.db), "LLM_ROUTER_NO_DETACHED_SPAWN": "1",
           "LLM_ROUTER_STOP_HOOK_MODE": "condensed"}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": SID}), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    msg = json.loads(r.stdout)["systemMessage"]
    assert msg and "session summary" not in msg and "recommendation" not in msg, msg


def test_the_hook_pair_is_byte_identical_at_current_version():
    a, b = (ROOT / "hooks" / "session-end.py").read_bytes(), HOOK.read_bytes()
    assert a == b and b.splitlines()[1] == b"# llm_router-hook-version: 49"


@pytest.mark.parametrize("bad", [None, []])
def test_offload_and_targets_never_print_zero_for_unknown(bad):
    from llm_router import session_report as sr

    o, t = sr.offload_field(bad, "session"), sr.targets_field(bad, "session")
    assert o["value"] is None and (t["n"] is None) == (bad is None)


# -- F114: the compact money line, summary --session ---------------------------------------------

def _wt(**kw):
    from llm_router import ledger_view as lv

    return lv.LedgerWindow(as_of="T", source="usage", estimated=True, window="today", saved_usd=7.0, saved_n=6, unverified_usd=5.0, unverified_n=4,
                           verified_usd=2.0, verified_n=2, **kw)


def test_today_compact_gives_each_segment_its_own_n():
    mod = _hook()
    assert mod._today_compact(_wt()) == "~$5.00 est. saved (n=4, today) · $2.00 verified saved (n=2, today)"


def test_today_compact_without_a_split_is_na_never_the_merged_total():
    from dataclasses import replace

    mod = _hook()
    out = mod._today_compact(replace(_wt(), unverified_usd=None, unverified_n=None,
                                     verified_usd=None, verified_n=None))
    assert out == "est. saved n/a (today)" and "7.00" not in out
    half = mod._today_compact(replace(_wt(), verified_usd=None, verified_n=None))
    assert half == "~$5.00 est. saved (n=4, today) · verified saved n/a (today)"


def test_today_compact_with_no_savings_is_a_zero_estimate_with_n_zero():
    from dataclasses import replace

    mod = _hook()
    z = replace(_wt(), unverified_usd=0.0, unverified_n=0, verified_usd=0.0, verified_n=0, saved_usd=0.0, saved_n=0)
    assert mod._today_compact(z) == "~$0.00 est. saved (n=0, today)"


def test_session_id_is_refused_when_invalid_never_rewritten():
    """F116: a bad id is refused (ValueError), not mapped to '_': 'abc def' must not name 'abc_def'."""
    from llm_router import session_report as sr

    assert sr.sanitize_session_id("ab-1.2_x") == "ab-1.2_x"
    assert sr.sanitize_session_id(None) is None
    for bad in ("a\x1b[31mb\n", "abc def", "", "a/b"):
        with pytest.raises(ValueError):
            sr.sanitize_session_id(bad)
    with pytest.raises(ValueError):          # an empty id never falls back to the current session
        sr.build("")


@pytest.mark.parametrize("bad", ["nope\x1b[31m", "abc def", ""])
async def test_invalid_session_id_is_refused_exit_2(p35, bad):  # noqa: F811
    env = {**p35.env, "LLM_ROUTER_DB_PATH": str(p35.db), "LLM_ROUTER_NO_DETACHED_SPAWN": "1"}
    r = subprocess.run([sys.executable, "-m", "llm_router.cli", "summary", "--session", bad],
                       env=env, capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == ""
    assert "invalid session id" in r.stderr and "\x1b" not in r.stderr


async def test_unknown_valid_session_id_is_one_line(p35):  # noqa: F811
    env = {**p35.env, "LLM_ROUTER_DB_PATH": str(p35.db), "LLM_ROUTER_NO_DETACHED_SPAWN": "1"}
    r = subprocess.run([sys.executable, "-m", "llm_router.cli", "summary", "--session", "nope-1"],
                       env=env, capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "no rows for session nope-1\n", repr(r.stdout)


def test_targets_line_is_na_at_zero_actions():
    from llm_router import session_report as sr

    t = sr.targets_field([], "session")
    r = {"window": "session", "label": "L", "as_of": "T", "targets": t, "no_rows": False,
         "offload": sr.offload_field([], "session"), "escalations": {"n": None, "na_reason": "x"},
         "cache_hit_rate": {"value": None, "n": None, "na_reason": "x"}, "quota_saved": None,
         "quality": None, "judge": None, "recommendation": "r"}
    assert "  targets n/a (n=0 actions, session)" in sr.render(r)


def test_summary_watch_with_session_is_a_clear_error_not_ignored():
    r = subprocess.run([sys.executable, "-m", "llm_router.cli", "summary", "--session", "x", "--watch"],
                       capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 2 and "--watch cannot be combined with --session" in r.stderr, r.stderr
