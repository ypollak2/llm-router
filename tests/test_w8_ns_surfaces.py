"""W8 follow-up (owner decision D-364): the last two structural zeros.

The Stop line's ``north star (strict) N%`` and the ``llm-router northstar`` CLI read ``northstar.report()``,
whose strict numerator has the same unobservable source as the status line's NS (``docs/bugs/NSZERO-2.md``).
They now follow the status line's rule: a session whose units carry no verifier verdict is BLIND, never 0%.
The Stop line is a hot path, so it says blind without an n and never reads a sub-agent transcript; the CLI is
not, so it prints the n with its unit and window and counts sub-agent calls like ``llm-router kpi`` does.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router import northstar as ns
from tests.test_northstar import _bulk_user_prompts, _iso, _project, _user, _write_jsonl
from tests.test_p09_session_end_bg import _load, _run_main, _seed_session, _trap_slow_steps, _write_usage

SID = "aaaaaaaa-1111-2222-3333-444444444444"
SID2 = "bbbbbbbb-1111-2222-3333-444444444444"
SONNET = "claude-sonnet-5-5"
BLIND = "north star (strict): blind (no verifier connected)"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude_projects"))
    monkeypatch.setattr("llm_router.quality_breaker.open_classes", lambda: [])


def _session(tmp_path, sid=SID, prompts=60):
    """A transcript with ``prompts`` real user prompts, minutes old (inside every window the surfaces read)."""
    proj = _project(tmp_path)
    t0 = time.time() - 900
    _write_jsonl(proj / f"{sid}.jsonl", [_user(sid, "please look at the first task in this repo", t0),
                                         *_bulk_user_prompts(sid, prompts - 1, start_ts=t0 + 1)])
    return proj


def _subagent(proj, sid=SID, calls=30, name="agent-a1.jsonl"):
    t0 = time.time() - 800
    recs = [_user(sid, "Investigate the failing test and report back with the cause.", t0)]
    for i in range(calls):
        recs.append({"type": "assistant", "isSidechain": True, "timestamp": _iso(t0 + 1 + i), "sessionId": sid,
                     "message": {"id": f"msg_sub_{name}_{i}", "model": SONNET, "role": "assistant",
                                 "content": [{"type": "text", "text": "step"}]}})
    _write_jsonl(proj / sid / "subagents" / name, recs)


def _unit(sid: str, i: int, *, verified: bool) -> dict:
    return {"session_id": sid, "kind": "routed_mcp", "lever": "mcp", "outcome": "used", "model": "qwen3-coder:30b",
            "task_type": "code", "ts": time.time() - 100 + i * 0.001,
            "verify": {"verify_status": "pass_f2p"} if verified else None}


def _stream(monkeypatch, units):
    monkeypatch.setattr(ns, "units", lambda **kw: iter(list(units)))


# ── report(): the strict figure is blind, not zero ───────────────────────────────────────────────

def test_a_session_with_no_verdict_is_blind_and_states_no_share(tmp_path):
    proj = _session(tmp_path)
    data = ns.report(days=None, session_id=SID, root=proj.parent)
    row, agg = data["sessions"][0], data["aggregate"]
    assert row["units"] >= ns.MIN_UNITS and row["strict_blind"] is True
    assert row["strict_share"] is None and row["strict_used"] == 0        # the COUNT is a true 0; the share cannot be said
    assert agg["strict_blind"] is True and agg["too_few"] is False
    assert (agg["strict_median"], agg["strict_p25"], agg["strict_max"]) == (None, None, None)


def test_a_verdict_makes_the_session_and_the_aggregate_measured(monkeypatch):
    _stream(monkeypatch, [_unit(SID, i, verified=i < 10) for i in range(ns.MIN_UNITS)])
    data = ns.report(days=7)
    row, agg = data["sessions"][0], data["aggregate"]
    assert row["strict_blind"] is False and row["strict_share"] == pytest.approx(0.2)
    assert agg["strict_blind"] is False and agg["strict_median"] == pytest.approx(0.2)


def test_the_aggregate_leaves_a_blind_session_out_instead_of_counting_it_as_zero(monkeypatch):
    units = [_unit(SID, i, verified=i < 10) for i in range(ns.MIN_UNITS)] + [_unit(SID2, i, verified=False) for i in range(ns.MIN_UNITS)]
    _stream(monkeypatch, units)
    data = ns.report(days=7)
    rows = {r["session_id"]: r for r in data["sessions"]}
    assert rows[SID]["strict_blind"] is False and rows[SID2]["strict_blind"] is True and rows[SID2]["strict_share"] is None
    assert data["aggregate"]["strict_median"] == pytest.approx(0.2)        # not the median of 0.2 and a made-up 0.0
    assert data["aggregate"]["strict_blind"] is False


def test_below_the_minimum_units_it_is_too_few_not_blind(tmp_path):
    proj = _session(tmp_path, prompts=5)
    data = ns.report(days=None, session_id=SID, root=proj.parent)
    assert data["aggregate"]["too_few"] is True and data["aggregate"]["strict_blind"] is False
    assert data["sessions"][0]["share"] is None and data["sessions"][0]["strict_share"] is None


def test_report_asks_units_for_the_seam_and_not_for_subagents_unless_told(monkeypatch):
    seen = []

    def spy(**kw):
        seen.append(kw)
        return iter([])

    monkeypatch.setattr(ns, "units", spy)
    ns.report(days=7)
    ns.report(days=7, subagents=True)
    monkeypatch.setattr(ns, "NS_ATTACH_VERIFY_RECORDS", True)
    ns.report(days=7)
    assert [(k["verify_records"], k["subagents"]) for k in seen] == [(False, False), (False, True), (True, False)]


# ── the Stop line (a hot path) ───────────────────────────────────────────────────────────────────

def test_the_stop_line_is_blind_with_no_n_and_never_a_percent(tmp_path):
    proj = _session(tmp_path)
    line = ns.current_session_line(SID, root=proj.parent)
    assert line == BLIND == ns.STOP_LINE_BLIND
    assert "%" not in line and "n=" not in line


def test_the_stop_line_keeps_its_other_states(tmp_path, monkeypatch):
    proj = _session(tmp_path, prompts=5)
    assert ns.current_session_line(SID, root=proj.parent).startswith("north star: too few to tell (n=")
    assert ns.current_session_line("no-such-session", root=proj.parent) == "north star: no data"
    _stream(monkeypatch, [_unit(SID, i, verified=i < 10) for i in range(ns.MIN_UNITS)])
    assert ns.current_session_line(SID) == "north star (strict) 20% (n=50)"       # a verdict: measured, as before


def test_the_stop_line_reads_no_subagent_transcript(tmp_path, monkeypatch):
    proj = _session(tmp_path)
    _subagent(proj, calls=40)
    read = []
    real = ns._iter_jsonl
    monkeypatch.setattr(ns, "_iter_jsonl", lambda path: read.append(str(path)) or real(path))
    assert ns.current_session_line(SID, root=proj.parent) == BLIND
    ns.report(days=2, session_id=SID, root=proj.parent)
    assert read and not any("subagents" in p for p in read), read          # main files only, however many sub-agent files exist
    read.clear()
    ns.report(days=2, session_id=SID, root=proj.parent, subagents=True)
    assert any("subagents" in p for p in read)


def test_the_stop_child_caches_the_blind_line_and_the_next_stop_shows_it(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.setenv("LLM_ROUTER_STOP_HOOK", "full")
    hook = _load(name="session_end_w8_ns")
    _session(tmp_path)
    _seed_session(state, SID)
    _write_usage(state, time.time())
    real = ns.current_session_line
    _trap_slow_steps(hook, monkeypatch, tmp_path)
    monkeypatch.setattr(ns, "current_session_line", real)                   # the REAL line, the other steps stay trapped
    monkeypatch.setattr(hook, "_spawn_background_stop_work", lambda: None)
    hook._refresh_northstar_line()                                          # the child step
    assert json.loads((state / "northstar_line.json").read_text())["line"] == BLIND
    assert hook._cached_northstar_line(SID) == BLIND
    _e, out = _run_main(hook, monkeypatch, {"session_id": SID, "hook_event_name": "Stop"})
    message = json.loads(out)["systemMessage"]
    assert BLIND in message and "north star (strict) 0%" not in message
    monkeypatch.setenv("LLM_ROUTER_STOP_HOOK", "condensed")
    _e, out = _run_main(hook, monkeypatch, {"session_id": SID, "hook_event_name": "Stop"})
    assert BLIND in json.loads(out)["systemMessage"]                        # the condensed Stop extracts the same text


# ── llm-router northstar (not a hot path) ────────────────────────────────────────────────────────

def _cli(args, capsys):
    from llm_router.commands import northstar as cli

    assert cli.cmd_northstar(args) == 0
    return capsys.readouterr().out


def test_the_cli_says_blind_with_its_n_unit_and_window_never_zero_percent(tmp_path, capsys):
    _session(tmp_path)
    out = _cli(["--days", "7"], capsys)
    row = next(ln for ln in out.splitlines() if ln.startswith("  aaaaaaaa"))
    assert "verified offload blind (no verifier connected)  (n=60 units, 7d)" in row
    verified_part = row.split("attempted=")[0]
    assert "%" not in verified_part and "0.0" not in verified_part
    assert "verified offload over 1 session(s) with n>=50: blind (no verifier connected)  (n=60 units, 7d)" in out
    assert "median=0.0%" not in out.split("diagnostic")[0]
    assert "diagnostic (heuristic routed-and-used, not the NS)" in out        # the labelled diagnostic is still there


def test_the_cli_counts_sub_agent_calls_so_its_n_is_the_n_of_kpi(tmp_path, capsys):
    proj = _session(tmp_path)
    _subagent(proj, calls=40)
    out = _cli(["--days", "7"], capsys)
    assert "(n=100 units, 7d)" in out                                         # 60 prompts + 40 unique sub-agent messages


def test_a_single_session_run_reads_only_that_sessions_sub_agents(tmp_path, monkeypatch, capsys):
    proj = _session(tmp_path)
    _subagent(proj, calls=40)
    _session(tmp_path, sid=SID2)
    _subagent(proj, sid=SID2, calls=25)
    read = []
    real = ns._iter_jsonl
    monkeypatch.setattr(ns, "_iter_jsonl", lambda path: read.append(str(path)) or real(path))
    out = _cli(["--session", SID], capsys)
    sub_reads = [p for p in read if "subagents" in p]
    assert sub_reads and all(f"/{SID}/subagents/" in p for p in sub_reads), sub_reads
    assert "(n=100 units, all time)" in out and "single session" in out


def test_the_cli_json_carries_the_blind_flag_and_a_null_share(tmp_path, capsys):
    _session(tmp_path)
    data = json.loads(_cli(["--days", "7", "--json"], capsys))
    row = data["sessions"][0]
    assert row["strict_blind"] is True and row["strict_share"] is None
    assert data["aggregate"]["strict_blind"] is True and data["aggregate"]["strict_median"] is None


def test_the_cli_with_a_verdict_keeps_the_measured_line_and_labels_its_n(monkeypatch, capsys):
    _stream(monkeypatch, [_unit("sess-cli", i, verified=i < 10) for i in range(ns.MIN_UNITS)])
    out = _cli(["--days", "7"], capsys)
    row = next(ln for ln in out.splitlines() if ln.startswith("  sess-cli"))
    assert "verified offload 20.0%  (n=50 units, 7d)" in row and "blind" not in row
    assert "median=20.0%" in out and "(n=50 units, 7d)" in out and "blind" not in out
