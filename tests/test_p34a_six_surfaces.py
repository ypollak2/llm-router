"""P3.4-B0 (D-218): the six surfaces that print ledger figures, checked against hand-written oracles.

Evidence: the P3.4 plan (v16/coord/P34-PLAN.md section 1) lists six surfaces. Part A migrated three
(dashboard summary, ``llm-router summary``'s ledger line, ``kpi``). The statusline, the status-bar
hook and the session-end Stop still compute their own windows, filters and provider classes. This
file renders all six over ONE seeded home (the frozen-clock Kiritimati home of
``test_p34a_ledger_view_identity``) and checks every printed figure against a number written here by
hand, never against ``ledger_view`` itself.

Decided windows (D-218): compact surfaces show the mix for the SESSION and money for TODAY, each
with a window suffix. Every figure a surface prints must carry an n and a window.

Surfaces that disagree today are ``xfail(strict=True)`` and name the PR that fixes them; that PR
deletes exactly its markers (strict: a fix that lands without deleting the marker fails the suite).
B1 classifier/contract, B2 statusline, B3 status-bar, B4 session-end + F73, B5 summary F70 panels.

Seed additions over the A home (counts are unchanged, so the A oracles still hold): proxy rows carry
``auth`` (forwarded=oauth, fallback=api_key), ``seats.json`` holds one Claude seat and no API key,
every ``routing_decisions`` row has NULL ``latency_ms`` (``usage.latency_ms`` is NOT NULL),
``model_tracking.jsonl`` holds three classifier rows with no latency.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_p34a_ledger_view_identity import (  # noqa: F401  (home is a fixture)
    EXPECTED_EXEC,
    MIN,
    NOW,
    TZNAME,
    _dashboard_line,
    _expected_line,
    _iso,
    _kpi_line,
    _saved,
    _seed,
    _summary_line,
    home,
)

REAL_TIME = time.time            # the shell and its cache age run on the wall clock
HOOKS = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Session "s-main" = every row stamped with that id, any time (ledger_view.session_scope):
# routing_decisions R1 R2 R3 R4 R10 R11 R12 R13 R14 R15, proxy P1 P2 P3 P_edge_in P_edge_out P_old
# (P_dup is R4's trace; the no-ts, refused and HTTP 529 rows carry no session or are not calls).
SESSION_EXEC = {"ollama": 3, "codex": 1, "gemini_cli": 1, "openai": 6, "anthropic": 5}
#
# HAND CLASSIFICATION (D-205 / P34-PLAN 2.4; written from the seed rows, not from the code).
# Fixture evidence: proxy auth forwarded=oauth, fallback=api_key; seats.json = one Claude seat,
# NO API keys; routing_decisions rows carry no auth. Rule order: row auth -> usage "subscription" ->
# LOCAL_PROVIDERS -> seat (anthropic/claude -> Claude seat) -> unclassified (never paid).
#   row         provider    evidence                         class
#   R1,R10      ollama      LOCAL_PROVIDERS                  local
#   P1          ollama      served = local                   local
#   R2          codex       no codex seat, no key            unclassified
#   R13         gemini_cli  no gemini seat, no key           unclassified
#   R3,R4       openai      no seat, no OPENAI_API_KEY       unclassified
#   R11,R12,R14,R15  openai same                             unclassified
#   P2,P_edge_in,P_edge_out,P_old  anthropic  auth oauth     subscription
#   P3          anthropic   auth api_key                     paid
# today  (R1 R2 R3 R4 P1 P2 P3):               local R1+P1=2, sub P2=1, paid P3=1, uncl R2+R3+R4=3  (7)
# 24h    (today + R10 R13):                    local 3, sub 1, paid 1, uncl 3+R13=4                 (9)
# session(R1-R4 R10-R15 P1 P2 P3 P_edge_in/out P_old):
#        local R1,R10,P1=3; sub P2,P_edge_in,P_edge_out,P_old=4; paid P3=1;
#        uncl R2,R13,R3,R4,R11,R12,R14,R15=8                                                      (16)
SESSION_MIX = (3, 4, 1)           # local / subscription / paid
SESSION_UNCLASSIFIED = 8
MIX_SIX = {  # window -> (local, subscription, paid, unclassified), by the table above
    "today": (2, 1, 1, 3), "24h": (3, 1, 1, 4),
}
FIGURE = re.compile(r"\$-?\s?\d|\d+L/\d+|\d+ executed|\d+ ms")
HAS_N = re.compile(r"\bn=\d|\bn \d|\d+ executed")
HAS_WINDOW = re.compile(r"\b(session|today|24h|7d|month|lifetime)\b")


#: B2 (rv-597 carry-forward): a dollar-scale saving between local midnight and now-24h. The A seed's
#: only such row (U8) is worth under a cent, so "money for today" and "money for 24h" printed the same
#: 2-decimal figure and a wrong money window passed. This row sits 2 h before NOW: before the local
#: midnight (NOW is 01:30 local, TZ Kiritimati), inside 24h and 7d, outside today.
EXTRA_SAVED_USD = 4.0
EXTRA_OFFSET_S = 120 * MIN


def _saved_six(window: str) -> tuple[float, int]:
    """``_saved`` plus the extra row where the window reaches back past local midnight."""
    usd, n = _saved(window)
    return (usd + EXTRA_SAVED_USD, n + 1) if window in ("24h", "7d") else (usd, n)


#: P3.1-7: judge_results.jsonl rows (offsets before NOW). Hand oracle, written from these rows:
#:   today (local midnight = NOW-90min): scores 4, 5, 3 -> n=3, mean 4.0; the 3-score row has no judge
#:        model -> self-judge 1; the failed row is ungraded and not in n.
#:   24h: + the 100-min row (score 2) -> n=4, mean 14/4 = 3.5.   7d / 14d: + the 26 h row (score 1) -> n=5, mean 3.0.
#:   every window: n < 50 -> not informative. Rows carry no session id, so a session-scoped window is n=0.
def _jrow(offset_min, status="graded", score=None, judge="fixture-judge"):
    r = {"ts": NOW - offset_min * MIN, "door": "mcp", "status": status, "answering_model": "fixture/model-a"}
    if score is not None:
        r["score"] = score
    if judge:
        r["judge_model"] = judge
    return r


JUDGE_ROWS = [_jrow(10, score=4), _jrow(30, score=5), _jrow(50, score=3, judge=None),
              _jrow(20, status="failed"), _jrow(100, score=2), _jrow(26 * 60, score=1)]
JUDGE_ORACLE = {  # window -> (n, mean, self_judge)
    "today": (3, 4.0, 1), "24h": (4, 3.5, 1), "7d": (5, 3.0, 1)}


async def _seed_six(home: Path) -> Path:  # noqa: F811 -- pytest fixture name
    db = await _seed(home)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, estimated_claude_cost_saved, "
        "external_cost, model_used, host, input_tokens, output_tokens, mode, is_simulated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_iso(NOW - EXTRA_OFFSET_S), "s-main", "query", EXTRA_SAVED_USD, 0.0, "gemini_cli",
         "claude_code", 10, 10, "block", 0))
    con.commit()
    # usage.latency_ms is NOT NULL (a recorded latency); routing_decisions.latency_ms is nullable and
    # the A seed never sets it, so every routing_decisions row is already an unrecorded latency.
    assert con.execute("SELECT COUNT(*) FROM routing_decisions WHERE latency_ms IS NULL").fetchone()[0] > 0
    con.close()
    proxy = home / "proxy_calls.jsonl"
    rows = [json.loads(ln) for ln in proxy.read_text().splitlines()]
    auth = {"forwarded": "oauth", "fallback": "api_key"}
    for r in rows:
        if r.get("decision") in auth:
            r["auth"] = auth[r["decision"]]
    proxy.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    from llm_router import seats

    claude = seats.Seat(kind="claude.ai", plan="fixture-plan")
    doc = seats.Seats(claude=claude, api_keys={}, detected_at=_iso(NOW - 3600)).to_dict()
    (home / "seats.json").write_text(json.dumps(doc))
    track = [{"timestamp": NOW - m * MIN, "selected_model": "fixture/model-a", "provider": "fixture",
              "task_type": "query", "complexity": "simple", "classification_method": "heuristic",
              "classification_confidence": 0.9, "cost_usd_estimate": 0.0} for m in (10, 40, 70)]
    (home / "model_tracking.jsonl").write_text("\n".join(json.dumps(r) for r in track) + "\n")
    return db


@pytest.fixture
async def six(home, tmp_path_factory):  # noqa: F811 -- pytest fixture name
    """Seeded home plus a fake HOME whose ``.llm-router`` is that home (the shell hardcodes it)."""
    db = await _seed_six(home)
    fake = tmp_path_factory.mktemp("fakehome")
    (fake / ".llm-router").symlink_to(home)
    import os

    env = {"HOME": str(fake), "LLM_ROUTER_HOME": str(home), "TZ": TZNAME, "NO_COLOR": "1",
           "PATH": os.environ.get("PATH", ""), "LLM_ROUTER_STATUS_MODE": "full"}
    return SimpleNamespace(home=home, db=db, env=env)


def _expected_line_six(window: str) -> str:
    """The identity line with this home's hand mix (the A home's mix does not apply: Claude-only seat)."""
    ex = EXPECTED_EXEC[window]
    loc, sub, paid, unc = MIX_SIX[window]
    assert loc + sub + paid + unc == sum(ex.values())
    usd, n = _saved_six(window)
    # D-247: verified is its own segment. S1 ($0.50) is verified everywhere; the extra B2 row
    # (gemini_cli, $4.00) is verified too and sits only in the 24h/7d windows.
    ver_usd, ver_n = (0.5 + EXTRA_SAVED_USD, 2) if window in ("24h", "7d") else (0.5, 1)
    by = " ".join(f"{p} {c}" for p, c in sorted(ex.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"ledger {window}: {sum(ex.values())} executed ({by}) · mix local {loc} / "
            f"subscription {sub} / paid {paid} / unclassified {unc} · est. saved ${usd - ver_usd:,.2f} (n={n - ver_n:,}) "
            f"· verified saved ${ver_usd:,.2f} (n={ver_n:,})")


def _ungrounded(text: str, sep: str) -> list[str]:
    """Fragments that print a ledger figure without BOTH an n and a window."""
    frags = [f.strip() for f in ANSI.sub("", text).split(sep)]
    return [f for f in frags if FIGURE.search(f) and not (HAS_N.search(f) and HAS_WINDOW.search(f))]


# -- surface 1: statusline (segments compute into the kv cache, then the shell renders) ---------

def _statusline(six) -> str:
    from llm_router import statusline_segments as ss

    state = str(six.home)
    seg = ss.compute(state, "", "", {"LLM_ROUTER_CLAUDE_SUBSCRIPTION": "1"}, NOW)
    seg["written"] = str(int(REAL_TIME()))
    ss.write_cache(state, "s-main", seg)
    stdin = json.dumps({"session_id": "s-main", "cwd": "/fixture/proj", "model": {"id": "fixture"}})
    r = subprocess.run(["bash", str(HOOKS / "statusline-command.sh")], input=stdin, env=six.env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return ANSI.sub("", r.stdout).strip()


#: P35-2 (D-220): the statusline prints the estimate and the verified part as two segments. The A seed's
#: only verified row today is the `block` savings row S1 ($0.50, host claude_code, after the gate date);
#: the other 3 rows behind the today total (U1, U2, S2) are unverified.
VERIFIED_TODAY_USD, VERIFIED_TODAY_N = 0.5, 1


def test_statusline_money_is_the_today_oracle(six):
    usd, n = _saved_six("today")
    line = _statusline(six)
    est, est_n = usd - VERIFIED_TODAY_USD, n - VERIFIED_TODAY_N
    assert f"~${est:,.2f} est. saved\u00b7today n={est_n}" in line, line
    assert f"${VERIFIED_TODAY_USD:,.2f} verified saved\u00b7today n={VERIFIED_TODAY_N}" in line, line
    assert f"~${usd:,.2f} est" not in line, line            # verified and estimated are never one figure
    # the extra row is outside today but inside 24h: a wrong money window prints another figure
    assert f"~${_saved_six('24h')[0] - VERIFIED_TODAY_USD:,.2f} est" not in line, line


def test_statusline_mix_is_the_session_oracle(six):
    loc, sub, paid = SESSION_MIX
    seg = next((f for f in _statusline(six).split(" · ") if f.startswith("\u2696")), "")
    assert f"{loc}L/{sub}S/{paid}P" in seg and "session" in seg, seg


def test_statusline_figures_carry_n_and_window(six):
    line = _statusline(six)
    assert _ungrounded(line, " · ") == [], line
    assert "\u2696" in line and "$" in line, line        # something to check: both figures present


# -- surface 2: status-bar hook (formatter in a subprocess, HOME = the fixture) ------------------

_SB_DRIVER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sb", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m._now = lambda: float(sys.argv[2])      # the hook's one clock seam (B3): windows are local to this instant
print(m._format_status())
"""


def _status_bar(six) -> str:
    r = subprocess.run([sys.executable, "-c", _SB_DRIVER, str(HOOKS / "status-bar.py"), str(NOW)],
                       env=six.env, cwd=str(six.home), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return ANSI.sub("", r.stdout).strip()


def test_status_bar_day_is_the_today_oracle(six):
    assert f"D:${_saved_six('today')[0]:.2f}" in _status_bar(six)


def test_status_bar_week_is_the_7d_oracle(six):
    assert f"W:${_saved_six('7d')[0]:.2f}" in _status_bar(six)


def test_status_bar_session_calls_are_the_session_mix_oracle(six):
    loc, sub, paid = SESSION_MIX
    assert f"sub:{sub} free:{loc} paid:{paid}" in _status_bar(six)


def test_status_bar_figures_carry_n_and_window(six):
    line = _status_bar(six)
    assert _ungrounded(line, "\u2502") == [], line
    assert "D:$" in line and "sub:" in line, line         # something to check: money and calls present
    assert re.search(r"D:\$[\d.]+\u00b7today n=\d+", line), line
    assert re.search(r"W:\$[\d.]+\u00b77d n=\d+", line), line
    assert re.search(r"M:\$[\d.]+\u00b7month n=\d+", line), line


def test_status_bar_baseline_label_is_the_priced_model_not_zero(six):
    """rv-597: the old private baseline resolved to $0 in the subprocess ("vs Opus:$0")."""
    from llm_router import pricing

    line = _status_bar(six)
    assert f"(vs {pricing.savings_baseline_model()})" in line, line
    assert "vs Opus" not in line and "$0)" not in line, line


# -- surfaces 3, 4, 5: dashboard summary, `llm-router summary`, kpi (aligned since part A) -------

def test_dashboard_summary_and_kpi_print_the_today_oracle(six):
    want = _expected_line_six("today")
    assert _dashboard_line("today", six.db) == want
    assert _kpi_line(1) == want
    assert _ungrounded(want, "\n") == []


def test_dashboard_summary_and_summary_command_print_the_24h_oracle(six):
    want = _expected_line_six("24h")
    assert _dashboard_line("24h", six.db) == want
    assert _summary_line(24.0) == want


def test_session_window_is_the_hand_counted_oracle(six):
    from llm_router import ledger_view as lv

    w = lv.window_summary("session", session_id="s-main")
    assert dict(w.executed) == SESSION_EXEC and w.n == sum(SESSION_EXEC.values())
    assert (w.local_n, w.subscription_n, w.paid_n) == SESSION_MIX
    assert w.unclassified_n == SESSION_UNCLASSIFIED
    assert sum(SESSION_MIX) + SESSION_UNCLASSIFIED == w.n


def _summary_markdown() -> str:
    from llm_router.agents import SessionStore
    from llm_router.lineage import LineageStore
    from llm_router.observability import summary

    return summary.render_markdown(summary.collect(LineageStore(), SessionStore(), since_seconds=86400.0))


def test_summary_does_not_print_unrecorded_latency_as_zero(six):
    md = _summary_markdown()
    assert "Classified prompts (hints):** 3" in md        # the three rows without latency are in
    assert "Avg latency" not in md and "0 ms" not in md
    assert "**Latency:** n/a (no latency recorded (0 of 6 rows), 24h)" in md   # 6 = hand-counted 24h rows


def test_summary_headline_figures_carry_n_and_window(six):
    head = _summary_markdown().split("## Spend pattern")[0]
    assert _ungrounded(head, "\n") == [], head
    assert "ledger 24h" in head                           # something to check


# -- surface 6: session-end Stop (aggregates the child computes + the cheap box) -----------------

def _session_end():
    spec = importlib.util.spec_from_file_location("session_end_p34b0", HOOKS / "session-end.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_end_box_prints_the_24h_executed_oracle_proxy_rows_included(six):
    mod = _session_end()
    agg = mod._compute_ledger_aggregates()
    total = sum(EXPECTED_EXEC["24h"].values())
    assert total == 9 and agg["executed_24h"] == total      # 6 without the 3 proxy rows
    mod._STOP_LEDGER = agg
    assert "ROUTING  24h  9 executed" in mod._cheap_summary_box(None)


def test_session_end_today_money_is_the_oracle(six):
    usd, n = _saved_six("today")
    agg = _session_end()._compute_ledger_aggregates()
    # P35-1 (D-220): measured apart from estimated. Hand oracle: S1 is the one verified (measured)
    # row, 0.50; the rest of the merged figure is the estimate.
    measured = 0.5
    assert agg["today_compact"] == f"~${usd - measured:,.2f} est. saved · ${measured:,.2f} verified saved (n={n:,}, today)"
    # rv-597 carry-forward: the 4-dollar row 2 h before NOW is inside 24h and outside today, so a
    # 24h money window (or the -2h mutant) prints another 2-decimal figure
    usd24, n24 = _saved_six("24h")
    assert f"{usd24:,.2f}" != f"{usd:,.2f}" and f"~${usd24:,.2f}" not in agg["today_compact"]


def test_session_end_embeds_the_ledger_windows_it_prints_from(six):
    """The cache carries LedgerWindow.to_json() for 24h and today: the Stop's figures ARE the ledger's."""
    from llm_router import ledger_view as lv

    agg = _session_end()._compute_ledger_aggregates()
    assert agg["v"] == 2
    for window in ("24h", "today"):
        lw = lv.LedgerWindow.from_json(agg[f"window_{window}"])
        assert lw.window == window and lw.line() == _expected_line_six(window), lw.line()
    assert agg["executed_24h"] == lv.LedgerWindow.from_json(agg["window_24h"]).n == 9


def test_session_end_money_carries_n_and_window(six):
    compact = _session_end()._compute_ledger_aggregates()["today_compact"]
    assert FIGURE.search(compact)                          # something to check
    assert _ungrounded(compact, "\n") == []


def test_session_end_stale_cache_is_marked_as_of(six):
    mod = _session_end()
    stale = {"v": mod._LEDGER_CACHE_VERSION, "ts": NOW - 3 * 3600, "day": mod._local_day(),
             "executed_24h": 7}
    (six.home / mod._LEDGER_CACHE_FILENAME).write_text(json.dumps(stale))
    mod._STOP_LEDGER = mod._read_ledger_cache(NOW)         # rejected for age: {}
    assert mod._STOP_LEDGER == {}
    box = mod._cheap_summary_box(None)
    assert re.search(r"7 executed \(as of \d\d:\d\d\)", box), box
    assert "ROUTING  24h  7 executed\n" not in box + "\n"  # never printed as live


def test_session_end_other_day_cache_is_na(six):
    mod = _session_end()
    other = {"v": mod._LEDGER_CACHE_VERSION, "ts": NOW - 3 * 3600, "day": "1999-01-01",
             "executed_24h": 7}
    (six.home / mod._LEDGER_CACHE_FILENAME).write_text(json.dumps(other))
    mod._STOP_LEDGER = mod._read_ledger_cache(NOW)
    box = mod._cheap_summary_box(None)
    assert "ROUTING  24h  n/a" in box and "7" not in box, box


# -- contract: classification by evidence (B1) -----------------------------------------------------

def test_seats_fixture_has_a_claude_seat_and_no_api_key(six):
    from llm_router import seats

    s = seats.load_seats()
    assert s is not None and s.claude.present and not any(s.api_keys.values())


def test_a_seat_covered_provider_is_subscription(six):
    """provider_class stays pure (hook-safe): the caller hands it the discovered seats."""
    from llm_router import ledger_view as lv, seats

    s = seats.load_seats()
    assert lv.provider_class("anthropic", seats=s, now=NOW) == "subscription"
    assert lv.provider_class("openai", seats=s, now=NOW) == "unclassified"     # no seat, no key: never paid
    assert lv.provider_class("anthropic") == "unclassified"           # no seats given: no evidence


def test_proxy_auth_decides_subscription_vs_paid(tmp_path):
    from llm_router import ledger_view as lv

    rows = [{"ts": NOW - 5 * MIN, "decision": "served"},
            {"ts": NOW - 6 * MIN, "decision": "forwarded", "auth": "oauth"},
            {"ts": NOW - 7 * MIN, "decision": "fallback", "auth": "api_key"}]
    proxy = tmp_path / "p.jsonl"
    proxy.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    w = lv.window_summary("24h", now=NOW, db_path=tmp_path / "absent.db", proxy_path=proxy)
    assert (w.local_n, w.subscription_n, w.paid_n) == (1, 1, 1)


# -- P3.4-B5 (F70): what `llm-router summary` itself prints ---------------------------------------
# Evidence: F70 showed "Avg latency: 0 ms" for rows that recorded none (`latency_ms or 0`), a tier
# "unknown", `Spent $0.00` for subscription traffic and a headline with no window or source.

def _set_latency(six, value) -> None:
    con = sqlite3.connect(six.db)
    con.execute("UPDATE routing_decisions SET latency_ms = ?", (value,))
    con.commit()
    con.close()


def test_latency_percentiles_come_from_recorded_rows_with_their_n(six):
    _set_latency(six, 250.0)
    md = _summary_markdown()
    # 6 = the hand-counted routing_decisions rows of the 24h window (the 3 proxy rows carry no latency)
    assert "**Latency:** p50 250 ms · p95 250 ms · p99 250 ms (n=6 of 6 rows, 24h)" in md, md


def test_a_zero_latency_is_not_a_recorded_latency(six):
    _set_latency(six, 0.0)       # the writers' `latency_ms or 0.0` default for "not measured"
    md = _summary_markdown()
    assert "**Latency:** n/a" in md and "0 ms" not in md, md


def test_tier_and_provider_panels_name_unclassified_with_n(six):
    md = _summary_markdown()
    tiers = md.split("## Tier distribution")[1].split("## Providers")[0]
    assert "`unclassified (3)` | 3 |" in tiers and "unknown" not in tiers.replace("(n=3", ""), tiers
    assert "(n=3, 24h)" in md.split("## Tier distribution")[1].splitlines()[0]
    from llm_router.agents import SessionStore
    from llm_router.observability import summary

    class _Rows:                      # a row whose model cannot name a provider
        def recent(self, limit=5000):
            return [{"timestamp": time.time(), "model_chosen": "", "model_tier": None, "host": "h",
                     "cost_usd": 0.0}]

    data = summary.collect(_Rows(), SessionStore(), since_seconds=86400.0)
    providers = md_providers = summary.render_markdown(data).split("## Providers")[1].split("## Routing")[0]
    assert "`unclassified (1)` | 1 |" in providers, md_providers


def test_spent_carries_n_and_window_or_is_na(six):
    md = _summary_markdown()
    line = next(ln for ln in md.splitlines() if ln.startswith("- **Spent:**"))
    assert " (n=" in line and line.endswith(", 24h)"), line
    six.db.unlink()                    # no usage database: unknown, never $0.00
    md = _summary_markdown()
    assert "- **Spent:** n/a (no usage database)" in md, md


def test_lineage_counterfactual_names_its_source_n_and_window(six):
    md = _summary_markdown()
    line = next(ln for ln in md.splitlines() if "always-premium" in ln)
    assert "lineage.db estimate" in line and "n=3" in line and "24h" in line, line


def test_a_negative_saving_renders_signed_not_clamped(six):
    """rv-613: a loss prints with its sign on the summary surface (markdown and terminal)."""
    from rich.console import Console

    from llm_router.agents import SessionStore
    from llm_router.observability import summary

    assert summary._fmt_cost(-2.5) == "-$2.50" and summary._fmt_cost(0.0) == "$0.00"

    class _Loss:                       # 100 tokens priced far above the always-premium baseline
        def recent(self, limit=5000):
            return [{"timestamp": time.time(), "model_chosen": "x/y", "model_tier": "premium",
                     "host": "h", "cost_usd": 5.0, "input_tokens": 100, "output_tokens": 100}]

    data = summary.collect(_Loss(), SessionStore(), since_seconds=86400.0)
    assert data.savings_usd < 0
    md = summary.render_markdown(data)
    line = next(ln for ln in md.splitlines() if "always-premium" in ln)
    assert "**-$" in line and "(-" in line, line          # signed amount and signed percentage
    console = Console(record=True, width=200)
    summary.render(data, console=console)
    assert "≈-$" in console.export_text()


# -- P3.1-7: judge figures ride on the ledger window; every surface that prints them agrees ----------

@pytest.fixture
async def six_judge(six):
    """The six-surface home plus judge_results.jsonl (kept out of ``six`` so the P35 fixtures that build
    on it keep their hand counts)."""
    (six.home / "judge_results.jsonl").write_text("\n".join(json.dumps(r) for r in JUDGE_ROWS) + "\n")
    return six


@pytest.mark.parametrize("window", sorted(JUDGE_ORACLE))
def test_ledger_window_carries_the_judge_oracle(six_judge, window):
    from llm_router import ledger_view as lv

    w = lv.window_summary(window, now=NOW, judge=True)
    n, mean, self_judge = JUDGE_ORACLE[window]
    assert (w.judge_n, w.judge_mean, w.judge_self_judge) == (n, mean, self_judge)
    assert w.judge_not_informative is True                       # n < 50
    assert w.judge_line() == f"judge: mean {mean:.2f} (n={n}, {window}, not informative n<50)"
    fig = w.figures()["judge"]
    assert (fig.value, fig.n, fig.window) == (mean, n, window)


def test_judge_does_not_change_the_ledger_line(six_judge):
    from llm_router import ledger_view as lv

    assert lv.window_summary("24h", now=NOW, judge=True).line() == _expected_line_six("24h")


def test_unknown_judge_is_na_never_zero(six_judge):
    from llm_router import ledger_view as lv

    # rows carry no session id, so a session window matches none: n=0 -> mean None -> n/a
    w = lv.window_summary("session", session_id="s-main", now=NOW, judge=True)
    assert (w.judge_n, w.judge_mean) == (0, None)
    assert w.judge_line() == "judge: n/a (no judged calls, session s-main)", w.judge_line()
    assert w.figures()["judge"].value is None
    (six_judge.home / "judge_results.jsonl").unlink()              # no judge file at all: unknown, not 0
    w = lv.window_summary("24h", now=NOW, judge=True)
    assert (w.judge_n, w.judge_mean, w.judge_self_judge) == (None, None, None)
    assert w.judge_not_informative is True                    # judge_view: n unknown is not informative
    assert w.judge_line() == "judge: n/a (no judged calls, 24h)"


def test_old_cache_without_judge_fields_loads_as_unknown(six_judge):
    from llm_router import ledger_view as lv

    d = json.loads(lv.window_summary("24h", now=NOW, judge=True).to_json())
    assert d["v"] == 2 and d["judge_n"] == 4                  # additive: the cache version is unchanged
    for k in [k for k in d if k.startswith("judge_")]:
        del d[k]
    old = lv.LedgerWindow.from_json(json.dumps(d))
    assert (old.judge_n, old.judge_mean) == (None, None) and old.judge_line().startswith("judge: n/a")


def test_session_end_embedded_window_carries_the_judge_figures(six_judge):
    from llm_router import ledger_view as lv

    agg = _session_end()._compute_ledger_aggregates()
    lw = lv.LedgerWindow.from_json(agg["window_24h"])
    assert (lw.judge_n, lw.judge_mean) == JUDGE_ORACLE["24h"][:2]


def test_kpi_statusline_and_stop_judge_figures_equal_the_ledger_window(six_judge):
    """kpi section, statusline snapshot field and the Stop's cached line print the 14d ledger-window figures."""
    from llm_router import judge_view as jv, ledger_view as lv, statusline_snapshot as snap
    from llm_router.commands import kpi
    from tests.test_p35_statusline import UNKNOWN_FIELDS

    lw = lv.window_summary("14d", now=NOW, judge=True)
    n, mean = lw.judge_n, lw.judge_mean
    assert (n, mean, lw.window) == (5, 3.0, "14d")           # hand oracle: 4+5+3+2+1 over 5 rows
    sec = kpi._judge_quality(14, NOW, None)
    assert (sec["n"], sec["mean"], sec["self_judge"]) == (n, mean, lw.judge_self_judge)
    assert sec["window"]["label"] == lw.window and sec["line"] == lw.judge_line()
    jv.refresh_cache(now=NOW)                                # what the Stop's child writes
    field = snap._judge_field()                              # the statusline snapshot reads that cache
    assert field == {"mean": mean, "n": n, "window": "14d"}
    assert f"judge {mean:.2f} (n={n}, 14d)" in snap.render_line({**UNKNOWN_FIELDS, "judge": field})
    assert _session_end()._cached_judge_line(NOW) == lw.judge_line()
    assert f"n={n}" in lw.judge_line() and "14d" in lw.judge_line()   # something to check: n and window shown


# -- P3.1-7 repair: the judge read is opt-in (O(file)); hot paths never pay it ---------------------

def _count_judge_reads(monkeypatch):
    from llm_router import judge_view as jv

    calls: list[tuple] = []
    real = jv.judge_summary

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(jv, "judge_summary", spy)
    return calls


def test_window_summary_default_does_not_read_the_judge_file(six_judge, monkeypatch):
    from llm_router import ledger_view as lv

    calls = _count_judge_reads(monkeypatch)
    w = lv.window_summary("24h", now=NOW)
    assert calls == [] and w.judge_n is None and w.judge_mean is None
    assert w.line() == _expected_line_six("24h")                   # the ledger line is unchanged
    lv.window_summary("24h", now=NOW, judge=True)
    assert len(calls) == 1                                          # opt-in reads it once


def test_dashboard_data_summary_does_not_call_judge_view(six_judge, monkeypatch):
    """The synchronous Codex Stop hook calls summary('today') and summary('lifetime')."""
    from llm_router import dashboard_data as dd

    calls = _count_judge_reads(monkeypatch)
    for period in ("today", "lifetime"):
        dd.summary(period, now=NOW)
    assert calls == []


def test_kpi_judge_section_parses_the_judge_file_once(six_judge, monkeypatch):
    from llm_router.commands import kpi

    calls = _count_judge_reads(monkeypatch)
    sec = kpi._judge_quality(14, NOW, None)
    assert len(calls) == 1 and sec["n"] == 5
