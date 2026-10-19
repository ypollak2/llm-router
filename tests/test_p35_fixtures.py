"""P35-0 (D-220): fixture HOME, units oracle and strict xfails for the P3.5 acceptance items.

Evidence: ``v16/coord/P35-PLAN.md``. The plan was written at 79fa7f5e; since then ``llm-router why``
(P35-6, #603), the P3.4 one-ledger (``Figure``/``show``/session panels), the P3.3 daily rows and the
P3.1 judge cache merged. P35-6 is therefore DONE and has no marker here (tests/test_p35_why.py
owns it). Every other item is still missing and is one ``xfail(strict=True)`` whose ``reason``
names the PR that flips it; that PR deletes exactly its markers (strict: a fix that lands without
deleting the marker fails the suite, an XPASS is an error).

Each xfail asserts the missing BEHAVIOUR on a surface that exists today (``show(unit=)``,
``Summary.compact()``, the statusline shell, ``kpi``, ``codex-stop``, ``HOOK_BUDGETS_MS``, ``pi``).
Where the plan's module is genuinely new (``units`` table lives in ``show``; ``quota_rate``,
``statusline_snapshot``, ``session_report``, ``weekly_report``) the test imports it inside the test, so
the failure is the missing module, which IS the missing behaviour. The function names those tests call
are the plan's proposed seams; the owning PR may rename them while it deletes its marker.

Fixture HOME: the P3.4 six-surface home (usage.db, routing_decisions, proxy_calls.jsonl, seats,
frozen clock 2026-10-17 01:30 Pacific/Kiritimati) plus decisions.jsonl, judge_results.jsonl,
shadow verdicts, quota_history.jsonl and 14 daily-offload rows. Plan names and prices are config
values written here, never product constants (D-205): the fixture names no model and no plan price.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.test_p34a_ledger_view_identity import DAY, MIN, NOW, home  # noqa: F401  (home is a fixture)
from tests.test_p34a_six_surfaces import (  # noqa: F401  (six is a fixture)
    ANSI, HOOKS, REAL_TIME, six,
)

SID = "s-main"
SERVED = "fixture/served-model"          # newest proxy row: the statusline "router" model
ESCALATIONS = 2                          # proxy rows with tier_reason escalation in the session
VERDICTS = {"good": 8, "bad": 2, "cannot_judge": 1, "old": 3}   # in 7d: n=10 (cannot_judge excluded)
JUDGED = 5                               # judge_results.jsonl graded rows
DAILY_FIRST, DAILY_LAST = date(2026, 10, 2), date(2026, 10, 15)   # 14 closed UTC days
PP_PER_ACTIVE_HOUR = [1, 1, 2, 2, 2, 3, 3, 3, 3, 4, 4, 5]        # 12 active hours, median 3.0
MEDIAN_PP = 3.0
MIN_ACTIVE_HOURS = 10

#: The units table (plan section Design, Units): (resource_kind, quantity) -> printed text, plain
#: and with both n's (P35-1, carry-forward from rv-628). The figure is n=12 over 7d; an estimate
#: that converts through a rate (work-hours) also carries the rate's n (``n_basis``=10 active hours).
#: subscription = % of window and ~work-hours; api = $; estimate = ~. Hand-written;
#: ``show(fig, unit=kind, quantity=what)`` must reproduce it.
UNITS_ORACLE = [
    # unit, quantity, value, fig kind, n_basis, plain text, text with n(s) and window
    ("subscription", "pct", 37.5, "measured", None, "37.5% of window", "37.5% of window (n=12, 7d)"),
    ("subscription", "work_hours", 2.5, "estimated", 10, "~2.5 work-hours",
     "~2.5 work-hours (n=12, rate n=10, 7d)"),
    ("api", "usd", 1.5, "measured", None, "$1.50", "$1.50 (n=12, 7d)"),
    ("estimate", "usd", 1.5, "estimated", None, "~$1.50", "~$1.50 (n=12, 7d)"),
]


def _jl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _history(pps: list[int], start: float) -> list[dict]:
    """Two measured 5-minute rows per active hour; weekly_pct rises by pp inside the hour."""
    rows, w = [], 10.0
    for h, pp in enumerate(pps):
        t = start + h * 3600
        for dt, wk in ((0, w), (300, w + pp)):
            rows.append({"ts": t + dt, "five_hour_pct": 20.0, "weekly_pct": wk,
                         "updated_at": t + dt, "five_hour_resets_at": None, "source": "measured"})
        w += pp
    return rows


@pytest.fixture
async def p35(six):  # noqa: F811 -- pytest fixture name
    """The six-surface home plus every ledger P3.5 reads, all under a fake HOME (never the real one)."""
    h = six.home
    _jl(h / "proxy_calls.jsonl", [
        {"ts": NOW - 1 * MIN, "decision": "served", "session_id": SID, "served_model": SERVED,
         "tier_reason": "policy_table", "trace_id": "T-new"},
        *({"ts": NOW - (2 + i) * MIN, "decision": "forwarded", "session_id": SID,
           "tier_reason": "escalation", "trace_id": f"T-esc{i}"} for i in range(ESCALATIONS)),
    ])
    _jl(h / "decisions.jsonl", [{"ts": NOW - MIN, "trace_id": "T-new", "layer": "L1",
                                 "confidence": 0.9, "door": "proxy"}])
    _jl(h / "judge_results.jsonl", [
        {"ts": NOW - (10 + i) * MIN, "status": "graded", "score": 4, "door": "proxy", "session_id": SID,
         "judge_model": "fixture-judge", "answering_model": "fixture-answer"} for i in range(JUDGED)])
    v = [{"ts": NOW - (i + 1) * 3600, "task_id": f"t{i}", "acceptable": i < VERDICTS["good"],
          "frontier_acceptable": True, "cannot_judge": False} for i in range(VERDICTS["good"] + VERDICTS["bad"])]
    v.append({"ts": NOW - 3600, "task_id": "tx", "acceptable": None, "frontier_acceptable": None,
              "cannot_judge": True})
    v += [{"ts": NOW - (8 + i) * DAY, "task_id": f"old{i}", "acceptable": True, "frontier_acceptable": True,
           "cannot_judge": False} for i in range(VERDICTS["old"])]
    _jl(h / "shadow_frontier" / "verdicts.jsonl", v)
    _jl(h / "quota_history.jsonl", _history(PP_PER_ACTIVE_HOUR, NOW - 5 * DAY))
    from llm_router import daily_offload

    d = DAILY_FIRST
    while d <= DAILY_LAST:
        i = (d - DAILY_FIRST).days
        daily_offload.write_day({
            "schema": daily_offload.SCHEMA, "date": d.isoformat(), "partial": False, "run_by": "scheduler",
            "populations": {"organic": {"action": {"measured": True, "n": 40 + i, "rate": 0.5 + 0.01 * i}}},
        }, base=daily_offload.daily_dir())
        d += timedelta(days=1)
    return six


# -- anti-vacuity: the fixture seeds rows and the oracles are not empty ---------------------------

def _count(path: Path) -> int:
    return len([ln for ln in path.read_text().splitlines() if ln.strip()]) if path.is_file() else 0


async def test_fixture_seeds_every_ledger(p35):
    h = p35.home
    assert _count(h / "proxy_calls.jsonl") >= 10 + 1 + ESCALATIONS
    assert _count(h / "decisions.jsonl") == 1
    assert _count(h / "judge_results.jsonl") == JUDGED
    assert _count(h / "shadow_frontier" / "verdicts.jsonl") == sum(VERDICTS.values()) - VERDICTS["cannot_judge"] + 1
    assert _count(h / "quota_history.jsonl") == 2 * len(PP_PER_ACTIVE_HOUR)
    from llm_router import daily_offload

    assert len(list(daily_offload.daily_dir().glob("2026-10-*.json"))) == 14
    import sqlite3

    con = sqlite3.connect(p35.db)
    assert con.execute("SELECT COUNT(*) FROM routing_decisions WHERE session_id=?", (SID,)).fetchone()[0] > 0
    con.close()
    assert p35.env["LLM_ROUTER_HOME"] == str(h), "the fixture runs on its own home, not the real one"


def test_oracles_are_not_empty():
    assert len(UNITS_ORACLE) >= 4 and {k for k, *_ in UNITS_ORACLE} == {"subscription", "api", "estimate"}
    assert all(row[-1] and row[-2] for row in UNITS_ORACLE)
    assert len(PP_PER_ACTIVE_HOUR) >= MIN_ACTIVE_HOURS and sorted(PP_PER_ACTIVE_HOUR)[5:7] == [3, 3]
    assert (DAILY_LAST - DAILY_FIRST).days + 1 == 14


# -- P35-1 units ---------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,what,value,fig_kind,n_basis,plain,with_n", UNITS_ORACLE)
def test_units_table_oracle(kind, what, value, fig_kind, n_basis, plain, with_n):
    from llm_router.ledger_view import Figure, show

    fig = Figure(value, 12, "7d", "2026-10-17T00:00:00", "fixture", fig_kind, None, n_basis)
    assert show(fig, unit=kind, quantity=what) == plain
    assert show(fig, unit=kind, quantity=what, with_n=True) == with_n


def test_measured_is_apart_from_estimated():
    from llm_router.dashboard_data import Summary

    s = Summary(period="today", baseline_model="x", realized_usd=2.0, realized_n=3,
                unverified_usd=5.0, unverified_n=4, routed_n=9, unmeasured_n=0)
    text = s.compact()
    assert "$2.00 measured" in text and "~$5.00 est" in text, text   # two segments, never the merged $7.00
    assert "$7.00" not in text, text


async def test_quota_rate_median_per_active_hour(p35):
    from llm_router import quota_rate

    r = quota_rate.median_pp_per_active_hour(now=NOW, days=14)
    assert r.value == MEDIAN_PP and r.n == len(PP_PER_ACTIVE_HOUR) and r.window == "14d", r


async def test_quota_rate_is_na_below_ten_active_hours(home):  # noqa: F811
    from llm_router import quota_rate

    _jl(home / "quota_history.jsonl", _history(PP_PER_ACTIVE_HOUR[:6], NOW - 3 * DAY))
    r = quota_rate.median_pp_per_active_hour(now=NOW, days=14)
    assert r.value is None and "n/a (6 of 10 active hours)" in r.na_reason, r


async def test_work_hours_is_quota_pct_saved_over_median_pp_per_active_hour(p35):
    """7.5 pp saved at the fixture's median 3.0 pp per active hour = 2.5 work-hours, with both n's."""
    from llm_router import quota_rate
    from llm_router.ledger_view import show

    rate = quota_rate.median_pp_per_active_hour(now=NOW, days=14)
    assert quota_rate.work_hours(7.5, rate) == 7.5 / MEDIAN_PP == 2.5
    fig = quota_rate.work_hours_figure(7.5, 12, rate, window="7d", as_of="t", source="fixture")
    assert fig.kind == "estimated" and (fig.n, fig.n_basis) == (12, len(PP_PER_ACTIVE_HOUR))
    assert show(fig, unit="subscription", quantity="work_hours", with_n=True) == \
        f"~2.5 work-hours (n=12, rate n={len(PP_PER_ACTIVE_HOUR)}, 7d)"


async def test_work_hours_is_na_below_ten_active_hours_never_zero(home):  # noqa: F811
    from llm_router import quota_rate
    from llm_router.ledger_view import show

    _jl(home / "quota_history.jsonl", _history(PP_PER_ACTIVE_HOUR[:6], NOW - 3 * DAY))
    rate = quota_rate.median_pp_per_active_hour(now=NOW, days=14)
    assert quota_rate.work_hours(7.5, rate) is None
    fig = quota_rate.work_hours_figure(7.5, 12, rate, window="7d", as_of="t", source="fixture")
    assert fig.value is None and "6 of 10 active hours" in fig.na_reason
    assert show(fig, unit="subscription", quantity="work_hours") is None


# -- P35-2 statusline ----------------------------------------------------------------------------

#: Delegations the Codex window counts in the snapshot oracle (budget = the module default, so 20%).
CODEX_USED = 3
#: Measured quota rows (5-minute series) inside the last 5 hours: the n behind "Claude 5h".
CLAUDE_5H_SAMPLES = 3
#: Per-session quota samples: weekly pp 20 -> 24 over 2 Stop turns = 2.0 pp per Claude turn.
PP_PER_TURN = 2.0
#: Actions served off Claude since local midnight (NOW is 01:30 there): session s-main's proxy rows at
#: -1 and -5 min (local backend), its MCP rows at -10 min (local) and -20 min (spread). The MCP row at
#: -95 min is before midnight; paid rows never count. ~work-hours = 4 x 2.0 pp / 3.0 pp per hour.
OFF_CLAUDE_TODAY = 4


def seed_snapshot_extras(h: Path) -> None:
    """The inputs the snapshot's other fields read, on top of the P35 home: a tagged organic session,
    a Claude quota snapshot, three Codex delegations, per-session quota samples and 5-minute rows
    inside the last 5 hours (flat weekly %, so the active-hour rate of the fixture is unchanged)."""
    (h / f"session_kind_{SID}.json").write_text(json.dumps(
        {"session_id": SID, "kind": "organic", "cwd": "/fixture/proj", "ts": NOW - DAY}))
    (h / "usage.json").write_text(json.dumps({"session_pct": 37, "weekly_pct": 40, "updated_at": NOW - MIN}))
    (h / "codex_window.json").write_text(json.dumps({"delegations": [NOW - 10 * MIN, NOW - 5 * MIN, NOW - MIN]}))
    samples = [(NOW - 3 * 3600, "start", 20.0), (NOW - 2 * 3600, "stop", 22.0), (NOW - 3600, "stop", 24.0)]
    _jl(h / "quota_samples.jsonl", [
        {"session_id": SID, "kind": kind, "ts": ts, "five_hour_pct": 10.0, "weekly_pct": wk, "updated_at": ts,
         "source": "measured", "session_kind": "organic"} for ts, kind, wk in samples])
    last_wk = 10.0 + sum(PP_PER_ACTIVE_HOUR)
    _jl(h / "quota_history.jsonl", [
        {"ts": NOW - (i + 1) * 20 * MIN, "five_hour_pct": 37.0, "weekly_pct": last_wk, "updated_at": NOW,
         "five_hour_resets_at": None, "source": "measured"} for i in range(CLAUDE_5H_SAMPLES)])


def _snapshot_shell(p35) -> list[str]:
    """Seed via ``statusline_snapshot.refresh``, publish it in the kv cache as the refresher does, run the
    real shell and return its printed rows. ``snap_at`` is re-stamped to the wall clock (the shell
    compares it with the wall clock; the fixture's clock is frozen), the same way ``written`` is."""
    from llm_router import statusline_segments as ss
    from llm_router import statusline_snapshot as snap

    snap.refresh(host="claude_code", session_id=SID, now=NOW)
    state = str(p35.home)
    seg = ss.compute(state, "", "", {"LLM_ROUTER_CLAUDE_SUBSCRIPTION": "1"}, NOW, SID)
    seg.update(ss.snap_segment(state, SID, NOW, refresh=False))
    seg["written"] = seg["snap_at"] = str(int(REAL_TIME()))
    ss.write_cache(state, SID, seg)
    stdin = json.dumps({"session_id": SID, "cwd": "/fixture/proj", "model": {"id": "fixture"}})
    r = subprocess.run(["bash", str(HOOKS / "statusline-command.sh")], input=stdin, env=p35.env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return [ANSI.sub("", ln) for ln in r.stdout.strip().splitlines()]


async def test_statusline_prints_the_five_fields(p35):
    """Carry-forward from rv-628: seed via ``statusline_snapshot.refresh``, run the shell, and check all five
    fields, each with its n and window (hand values: the module constants above)."""
    seed_snapshot_extras(p35.home)
    rows = _snapshot_shell(p35)
    snap_rows = [r for r in rows if r.startswith("router:")]
    assert len(snap_rows) == 1, rows
    line = snap_rows[0]
    fields = line.split(" | ")
    assert len(fields) == 5, line
    router, offload, quota, quality, saved = fields
    assert router == f"router: {SERVED} (policy_table)", router
    assert offload.startswith("offload ") and re.search(r"\(n=\d+, session", offload), offload
    assert quota == (f"Claude 5h 37% (n={CLAUDE_5H_SAMPLES}, 5h) / Codex "
                     f"{100 * CODEX_USED / 15:.0f}% (n={CODEX_USED}, 5h)"), quota
    good, bad = VERDICTS["good"], VERDICTS["bad"]
    assert quality == f"quality {good / (good + bad):.0%} (n={good + bad}, 7d)", quality
    hours = OFF_CLAUDE_TODAY * PP_PER_TURN / MEDIAN_PP
    assert saved == f"saved ~{hours:.1f} work-hours (n={OFF_CLAUDE_TODAY}, rate n={len(PP_PER_ACTIVE_HOUR)}, today)", saved
    for f in fields[1:]:           # every figure names its n and its window (the router field is a name, not a figure)
        assert "(n=" in f and re.search(r"\b(session|5h|7d|today)\b", f), f


async def test_verdict_summary_oracle(p35):
    from llm_router import shadow_frontier

    s = shadow_frontier.verdict_summary(now=NOW, days=7)
    good, bad = VERDICTS["good"], VERDICTS["bad"]
    assert (s["n"], s["k"], s["window"]) == (good + bad, good, "7d"), s
    assert abs(s["rate"] - good / (good + bad)) < 1e-9 and s["wilson"][0] < s["rate"] < s["wilson"][1], s


async def test_snapshot_line_router_is_the_newest_proxy_turn(p35):
    from llm_router import statusline_snapshot as snap

    d = snap.refresh(host="claude_code", session_id=SID, now=NOW)
    assert d["line"].startswith(f"router: {SERVED} (policy_table) | offload "), d["line"]
    assert f"quality {VERDICTS['good'] / (VERDICTS['good'] + VERDICTS['bad']):.0%}" in d["line"]


async def test_snapshot_older_than_15_minutes_is_na(p35):
    from llm_router import statusline_snapshot as snap

    snap.refresh(host="claude_code", session_id=SID, now=NOW)
    assert snap.read_line("claude_code", SID, now=NOW + 5 * MIN).startswith("router:")
    assert snap.read_line("claude_code", SID, now=NOW + 16 * MIN) == "n/a"


# -- P35-3 end-of-turn ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="P35-3 end-of-turn: codex-stop has no HOOK_BUDGETS_MS entry")
def test_codex_stop_has_a_300ms_budget():
    from llm_router.hook_latency import HOOK_BUDGETS_MS

    assert HOOK_BUDGETS_MS.get("codex-stop") == 300


@pytest.mark.xfail(strict=True, reason="P35-3 end-of-turn: Pi has no summary.ts extension")
def test_pi_installs_a_summary_extension():
    from llm_router.commands import pi

    assert "summary.ts" in pi.EXTENSIONS
    assert (Path(__file__).resolve().parents[1] / "integrations" / "pi" / "extensions" / "summary.ts").is_file()


@pytest.mark.xfail(strict=True, reason="P35-3 end-of-turn: codex-stop prints its own ledger sum, not the snapshot line")
async def test_codex_stop_message_is_the_snapshot_line(p35):
    snaps = p35.home / "snapshots"
    snaps.mkdir()
    (snaps / f"codex-{SID}.json").write_text(json.dumps({"line": "router: fixture-line", "written_at": NOW}))
    r = subprocess.run([sys.executable, str(HOOKS / "codex-stop.py")], input=json.dumps({"session_id": SID}),
                       env=p35.env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["systemMessage"] == "router: fixture-line", r.stdout


# -- P35-4 session summary -----------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="P35-4 session summary: session_report.py does not exist")
async def test_session_report_escalations_come_from_tier_reason(p35):
    from llm_router import session_report

    r = session_report.build(SID)
    assert r["escalations"]["n"] == ESCALATIONS and r["escalations"]["window"] == "session", r


@pytest.mark.xfail(strict=True, reason="P35-4 session summary: session_report.py does not exist")
async def test_session_report_cache_hit_rate_is_na_below_min(p35):
    from llm_router import session_report

    assert session_report.build(SID)["cache_hit_rate"]["value"] is None


@pytest.mark.xfail(strict=True, reason="P35-4 session summary: session_report.py does not exist")
async def test_session_report_recommendation_is_computed_or_na(p35):
    from llm_router import session_report

    rec = session_report.build(SID)["recommendation"]
    assert rec.startswith("~") or rec.startswith("no recommendation (n/a: "), rec


@pytest.mark.xfail(strict=True, reason="P35-4 session summary: session_report.py does not exist")
async def test_session_report_spread_is_not_in_the_offload_numerator(p35):
    from llm_router import session_report

    t = session_report.build(SID)["targets"]
    assert set(t) >= {"local", "cheap", "frontier", "spread"}
    assert t["offload_numerator"] == t["local"] + t["cheap"], t


# -- P35-5 weekly --------------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="P35-5 weekly: weekly_report.py does not exist")
async def test_weekly_report_from_daily_rows(p35):
    from llm_router import weekly_report

    w = weekly_report.build_week("2026-W41")          # Mon 2026-10-05 .. Sun 10-11, all seeded
    assert w["days"] == 7 and w["offload"]["ci"] and w["breakdowns"], w
    assert w["delta_vs_previous_week"] is None, w     # W40 has 3 seeded days: below the floor, no delta


@pytest.mark.xfail(strict=True, reason="P35-5 weekly: kpi --maintenance does not exist")
async def test_kpi_maintenance_flag_is_accepted(p35):
    from llm_router.commands import kpi

    try:
        rc = kpi.cmd_kpi(["--maintenance"])
    except SystemExit as e:                            # argparse rejects the unknown flag today
        rc = e.code
    assert rc == 0
