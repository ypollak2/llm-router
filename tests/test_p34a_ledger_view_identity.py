"""P3.4-A (D-194): one definition per ledger metric. The dashboard summary, ``llm-router
summary`` and ``llm-router kpi`` print identical {executed by provider, local/paid mix, saved, n,
window label} for the same window over the same seeded home.

Evidence: the P3.4 survey (2026-10-17, brief-p34.md) found the same question answered with a UTC
day here, a local day there, a rolling 6 h / 24 h elsewhere; with and without provenance, with and
without proxy rows, from a 5000-row lineage cap. This file seeds one home that contains a row of
every shape those definitions disagree on, then checks every surface against a HAND-WRITTEN oracle
(not against ``ledger_view`` itself: three surfaces that share one wrong function agree with each
other) and against each other.

Clock: ``time.time`` is frozen at 2026-10-17 01:30 in Pacific/Kiritimati (UTC+14), so the local day
(Oct 17) starts at Oct 16 10:00 UTC, and the UTC date of "now" is Oct 16. A UTC-day window and a
local-day window select different rows.

Seeded rows, minutes before NOW (local midnight is 90 min before). W = windows holding the row.

routing_decisions (provider, shape -> counted?)
  R1 ollama -10 | R2 codex -20 | R3 openai -30 | R4 openai -35 (trace T1)    today, 24h, 7d
  R10 ollama -95 | R13 gemini_cli -180                                       24h, 7d (before local midnight)
  R14 openai -25h                                                            7d
  R11 openai exactly -7d (inclusive edge)                                    7d
  R12 openai -7d-1s | R15 openai -30d                                        lifetime only
  provenance 'test' / NULL, cache_hit, breaker_open, error_*                 never
proxy_calls.jsonl
  P1 served -5 | P2 forwarded -15 | P3 fallback -25                          today, 24h, 7d
  P_dup forwarded -35, trace T1 (same call as R4)                            never (deduped)
  P_edge_in forwarded exactly -7d                                            7d
  P_edge_out -7d-1s | P_old -30d | no ts | refused prefix | HTTP 529         never in 7d
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

TZNAME = "Pacific/Kiritimati"
NOW = datetime(2026, 10, 17, 1, 30, tzinfo=ZoneInfo(TZNAME)).timestamp()
MIN = 60.0
DAY = 86400.0


def _ut(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# window -> {provider: n}, by hand (see the module docstring table)
EXPECTED_EXEC = {
    "today": {"ollama": 2, "codex": 1, "openai": 2, "anthropic": 2},
    "24h": {"ollama": 3, "codex": 1, "gemini_cli": 1, "openai": 2, "anthropic": 2},
    "7d": {"ollama": 3, "codex": 1, "gemini_cli": 1, "openai": 4, "anthropic": 3},
}
EXPECTED_MIX = {  # local / subscription / paid
    "today": (2, 1, 4), "24h": (3, 2, 4), "7d": (3, 2, 7),
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Seeded home (usage.db, proxy_calls.jsonl, session files) under a non-UTC TZ, time frozen."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    monkeypatch.setenv("TZ", TZNAME)
    time.tzset()
    monkeypatch.setattr(time, "time", lambda: NOW)
    yield tmp_path
    monkeypatch.undo()           # restores TZ, then the C library re-reads it
    time.tzset()


async def _seed(home: Path) -> Path:
    from llm_router import cost

    db = await cost._get_db()
    await db.close()
    path = home / "usage.db"
    con = sqlite3.connect(path)

    def usage(offset_s, provider, i, o, cost_usd, *, sim=0, reason=None, success=1, sid="s-main"):
        con.execute(
            "INSERT INTO usage (timestamp, model, provider, task_type, profile, input_tokens, "
            "output_tokens, cost_usd, latency_ms, success, reason, session_id, is_simulated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_ut(NOW - offset_s), "m", provider, "query", "balanced", i, o, cost_usd, 10.0,
             success, reason, sid, sim))

    usage(10 * MIN, "openai", 1000, 500, 0.01)                       # U1 paid
    usage(20 * MIN, "ollama", 2000, 1000, 0.0)                       # U2 free
    usage(25 * MIN, "subscription", 9000, 9000, 0.0)                 # U3 subscription: no token cost
    usage(26 * MIN, "cache", 0, 0, 0.0)                              # U4 cache row
    usage(27 * MIN, "openai", 0, 0, 0.0, reason="error_timeout")     # U5 error row (success=1 on purpose)
    usage(28 * MIN, "openai", 5000, 5000, 0.5, sim=1)                # U6 simulated
    usage(29 * MIN, "openai", 4000, 100, 0.1, sim=None)              # U7 provenance NULL: unknown
    usage(95 * MIN, "openai", 100, 100, 0.001)                       # U8 before local midnight
    usage(7 * DAY + 1, "openai", 700, 700, 0.07)                     # U9 outside 7d

    def savings(offset_s, mode, saved, sim=0):
        con.execute(
            "INSERT INTO savings_stats (timestamp, session_id, task_type, "
            "estimated_claude_cost_saved, external_cost, model_used, host, input_tokens, "
            "output_tokens, mode, is_simulated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_iso(NOW - offset_s), "s-main", "query", saved, 0.0, "gemini_cli", "claude_code",
             10, 10, mode, sim))

    savings(11 * MIN, "block", 0.5)                                  # verified
    savings(12 * MIN, "echo", 0.2)                                   # unverified
    savings(13 * MIN, "block", 9.0, sim=1)                           # simulated: out

    def decision(offset_s, provider, *, prov="runtime", reason=None, trace=None, sid="s-main"):
        con.execute(
            "INSERT INTO routing_decisions (timestamp, task_type, final_provider, success, "
            "reason_code, provenance, trace_id, session_id) VALUES (?,?,?,?,?,?,?,?)",
            (_ut(NOW - offset_s), "query", provider, 1, reason, prov, trace, sid))

    decision(10 * MIN, "ollama")                                     # R1
    decision(20 * MIN, "codex")                                      # R2
    decision(30 * MIN, "openai")                                     # R3
    decision(35 * MIN, "openai", trace="T1")                         # R4 (proxy row P_dup is the same call)
    decision(40 * MIN, "openai", prov="test")                        # R5
    decision(45 * MIN, "openai", prov=None)                          # R6
    decision(46 * MIN, "cache", reason="cache_hit")                  # R7
    decision(47 * MIN, "openai", reason="breaker_open")              # R8
    decision(48 * MIN, "openai", reason="error_timeout")             # R9
    decision(95 * MIN, "ollama")                                     # R10
    decision(7 * DAY, "openai")                                      # R11 inclusive edge
    decision(7 * DAY + 1, "openai")                                  # R12 just outside
    decision(180 * MIN, "gemini_cli")                                # R13
    decision(25 * 3600, "openai")                                    # R14
    decision(30 * DAY, "openai")                                     # R15
    con.commit()
    con.close()

    rows = [
        {"ts": NOW - 5 * MIN, "decision": "served", "session_id": "s-main"},                      # P1
        {"ts": NOW - 15 * MIN, "decision": "forwarded", "session_id": "s-main"},                  # P2
        {"ts": NOW - 25 * MIN, "decision": "fallback", "session_id": "s-main"},                   # P3
        {"ts": NOW - 35 * MIN, "decision": "forwarded", "trace_id": "T1", "session_id": "s-main"},  # P_dup
        {"ts": NOW - 7 * DAY, "decision": "forwarded", "session_id": "s-main"},                   # P_edge_in
        {"ts": NOW - 7 * DAY - 1, "decision": "forwarded", "session_id": "s-main"},               # P_edge_out
        {"ts": NOW - 30 * DAY, "decision": "forwarded", "session_id": "s-main"},                  # P_old
        {"decision": "forwarded", "session_id": "s-main"},                                        # no ts
        {"ts": NOW - 50 * MIN, "decision": "served", "prefix_outcome": "refused:x"},              # refused
        {"ts": NOW - 51 * MIN, "decision": "forwarded", "upstream_status": 529},                  # HTTP error
    ]
    (home / "proxy_calls.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (home / "session_start.txt").write_text(str(NOW - 2 * 3600))
    (home / "session_id.txt").write_text("s-main\n")
    return path


def _saved(window: str) -> tuple[float, int]:
    """Hand oracle for saved dollars and n. Rates come from pricing (the baseline policy)."""
    from llm_router import pricing

    base = pricing.savings_baseline_model()
    ri, ro = pricing.input_rate(base), pricing.output_rate(base)
    usd = (3000 * ri + 1500 * ro) / 1e6 - 0.01                    # U1 + U2 (paid cost netted)
    n = 2 + 2                                                       # U1, U2 + S1 (verified), S2
    if window in ("24h", "7d"):
        usd += (100 * ri + 100 * ro) / 1e6 - 0.001                # U8
        n += 1
    return usd + 0.5 + 0.2, n


def _expected_line(window: str) -> str:
    ex = EXPECTED_EXEC[window]
    loc, sub, paid = EXPECTED_MIX[window]
    usd, n = _saved(window)
    by = " ".join(f"{p} {c}" for p, c in sorted(ex.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"ledger {window}: {sum(ex.values())} executed ({by}) · mix local {loc} / "
            f"subscription {sub} / paid {paid} · est. saved ${usd:,.2f} (n={n:,})")


def _dashboard_line(window: str, db: Path) -> str:
    from llm_router import dashboard_data

    return dashboard_data.summary(window, db_path=db, now=NOW).ledger_line()


def _summary_line(hours: float) -> str:
    from llm_router.agents import SessionStore
    from llm_router.lineage import LineageStore
    from llm_router.observability import summary

    data = summary.collect(LineageStore(), SessionStore(), since_seconds=hours * 3600)
    md = summary.render_markdown(data)
    m = re.search(r"^- \*\*(ledger [^*]+)\*\*$", md, re.M)
    assert m, md
    return m.group(1)


def _kpi_line(days: int) -> str:
    from llm_router.commands import kpi

    card = kpi.compute_scorecard(days=days)
    assert f"  {card['ledger']['line']}" in kpi.render_scorecard(card).split("\n")
    return card["ledger"]["line"]


async def test_three_surfaces_print_the_oracle_for_7d(home):
    db = await _seed(home)
    want = _expected_line("7d")
    assert _dashboard_line("7d", db) == want
    assert _summary_line(168.0) == want
    assert _kpi_line(7) == want


async def test_dashboard_summary_and_kpi_agree_on_the_local_day(home):
    """Local midnight in UTC+14 is Oct 16 10:00 UTC: R10 (-95 min) belongs to yesterday."""
    db = await _seed(home)
    want = _expected_line("today")
    assert _dashboard_line("today", db) == want
    assert _kpi_line(1) == want


async def test_dashboard_summary_and_summary_command_agree_on_24h(home):
    db = await _seed(home)
    want = _expected_line("24h")
    assert _dashboard_line("24h", db) == want
    assert _summary_line(24.0) == want


async def test_week_alias_is_the_rolling_7d_window(home):
    db = await _seed(home)
    from llm_router import dashboard_data

    s = dashboard_data.summary("week", db_path=db, now=NOW)
    assert s.ledger.window == "7d"
    assert s.ledger_line() == _expected_line("7d")


async def test_executed_routes_matches_the_ledger(home):
    """``routing_report.executed_routes`` delegates (mutant: its old inline SQL, no provenance)."""
    from llm_router.routing_report import executed_routes

    await _seed(home)
    res = executed_routes(24.0)
    assert res["by_provider"] == EXPECTED_EXEC["24h"]
    assert res["total"] == sum(EXPECTED_EXEC["24h"].values())
    # F23: the two runtime ollama rows (R1, R10) have no skip list -> "unknown"; proxy rows carry none.
    assert res["local_by_cause"]["unknown"] == 2 and sum(res["local_by_cause"].values()) == 2
    assert set(res["local_by_cause"]) == {"bench", "last_resort", "policy", "other_skip", "unknown"}


async def test_get_savings_by_period_uses_the_same_rows_as_the_ledger(home):
    """Same production rows and calls as ledger_view for today and the 7-day week (mutant: its old
    own ``is_simulated`` clause, which the NULL-provenance row U7 tells apart)."""
    from llm_router import cost, ledger_view

    await _seed(home)
    res = await cost.get_savings_by_period()
    for key, window in (("today", "today"), ("week", "7d")):
        usage = ledger_view.money_window(ledger_view.resolve(window)).by_source["usage"]
        assert res[key]["calls"] == usage["production_calls"], (key, res[key])
        assert res[key]["saved_usd"] == pytest.approx(usage["production_saved_usd"], abs=1e-4)
    assert res["today"]["calls"] == 2          # U1, U2: not U3 sub, U4 cache, U5 error, U6 sim, U7 NULL
    assert res["week"]["calls"] == 3           # + U8
    assert res["week"]["subscription_calls"] == 1


async def test_session_scope_prefers_session_id_and_labels_the_fallback(home):
    from llm_router import ledger_view as lv

    await _seed(home)
    by_id = lv.window_summary("session", session_id="s-main")
    assert by_id.scope == "session_id" and by_id.window.startswith("session s-main")
    assert by_id.n > 0
    other = lv.window_summary("session", session_id="s-other")
    assert other.n == 0 and other.saved_n == 0
    fallback = lv.window_summary("session", session_start=lv.read_session_start())
    assert fallback.scope == "timestamp" and "by timestamp" in fallback.window
    assert lv.read_session_id() == "s-main"


async def test_window_edges_are_closed_and_exact(home):
    """R11 and P_edge_in sit exactly on the 7-day edge (counted); one second earlier is out."""
    from llm_router import ledger_view as lv

    await _seed(home)
    seven = lv.executed_by_provider(lv.resolve("7d"))
    life = lv.executed_by_provider(lv.resolve("lifetime"))
    assert seven["openai"] == 4 and seven["anthropic"] == 3
    assert life["openai"] == 4 + 2 and life["anthropic"] == 3 + 2   # R12, R15 / P_edge_out, P_old


async def test_empty_home_reports_no_data_not_zero(home):
    from llm_router import ledger_view as lv

    s = lv.window_summary("24h")
    assert s.has_data is False and "no execution data" in s.line()


def test_ns_strict_delegates_to_the_kpi_unit_stream(home, monkeypatch):
    from llm_router import ledger_view as lv
    from llm_router.commands import kpi

    seen = {}

    def fake(days, allowed, index, win=None, units=None):
        seen["days"] = days
        return ({"value": "ns"}, {}, {}, {})

    monkeypatch.setattr(kpi, "_ns_d1_d2", fake)
    assert lv.ns_strict(3) == {"value": "ns"} and seen["days"] == 3
