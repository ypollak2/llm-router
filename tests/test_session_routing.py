"""The status line's two session numbers: routed share of calls, and local attempts that worked.

``llm_router.session_routing`` counts them (refresher only); ``statusline_tick`` formats them (stdlib only).
Every definition in the module docstring has a fixture here: the three transcript layouts, a Codex run's model
responses (its sub-agent threads included), the MCP call a transcript joins to a routing row, the cache hit and the
cancelled call that are no call, the delegation's twin routing row that must not be counted twice, the run with no
rollout that counts as one and is flagged, and the 0-attempt local figure that prints n/a and never 0%.

No real model, Codex or Ollama is started, and nothing outside tmp is read: HOME, LLM_ROUTER_HOME, CODEX_HOME and
CLAUDE_PROJECTS_DIR are all pointed at tmp dirs.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import textwrap
import time
from pathlib import Path

import pytest

from llm_router import codex_agent, codex_rollouts as cr, paths, session_routing as sr
from llm_router import statusline_snapshot as snap
from llm_router import statusline_tick as tick

SID = "11111111-2222-3333-4444-555555555555"
NOW = 1_793_780_000.0                    # 2026-11-05 ~08:53 UTC
T1 = "01a1a58b-2c03-7532-b50a-ef6cf42e84e5"
T2 = "01a1a58b-8d4c-7bf2-aa7a-dd3dfc08b288"       # sub-agent thread spawned by T1
T3 = "01a1a592-9c91-7d72-9cd6-4283cb4af094"       # a run with no rollout on disk
T4 = "01a1a593-2810-7fb3-878e-ee80ac8c1f8e"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    projects = tmp_path / "projects"
    codex = tmp_path / "codex"
    for d in (home, projects, codex):
        d.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return home, projects, codex


# ── fixtures: transcripts ────────────────────────────────────────────────────────────────────────────────

def _assistant(mid, model="claude-opus-5-5", tool_ids=(), extra=""):
    content = [{"type": "tool_use", "id": t, "name": "mcp__llm_router__llm", "input": {}} for t in tool_ids]
    msg = {"model": model, "content": content}
    if mid:
        msg["id"] = mid
    return json.dumps({"message": msg, "type": "assistant", "sessionId": SID}, separators=(",", ":")) + extra + "\n"


def _user(text="hi"):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, separators=(",", ":")) + "\n"


def _write(path: Path, *lines: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)
    return path


def _layout(projects: Path):
    """main + a sub-agent + a WORKFLOW agent, with one message id repeated in two files (a fork replays its parent)."""
    proj = projects / "-Users-x"
    main = _write(proj / f"{SID}.jsonl", _user(), _assistant("m1"), _assistant("m1"), _assistant("m2"),
                  _assistant("<>", model="<synthetic>"), _assistant(None))
    sub = _write(proj / SID / "subagents" / "agent-a1.jsonl", _user(), _assistant("m2"), _assistant("s1", "claude-sonnet-5-5"))
    wf = _write(proj / SID / "subagents" / "workflows" / "wf_abc" / "agent-w1.jsonl",
                _assistant("w1", "claude-sonnet-5-5"), _assistant("w2", "claude-haiku-5-5"))
    return main, sub, wf


def test_transcript_calls_are_unique_message_ids_across_all_three_layouts(env):
    _home, projects, _codex = env
    _layout(projects)
    t = sr.transcript_figures(SID, budget_s=30)
    # m1, m2 (also in the sub-agent file), s1, w1, w2, and the one record with no id = 6; the <synthetic> one is none
    assert t["claude"] == 6 and t["complete"] is True
    assert t["files"] == {"main": 1, "subagents": 1, "workflows": 1}     # the workflow file is read, not just the 1-level glob
    assert t["other_model"] == 0 and t["unclassified"] == 0


def test_a_non_claude_model_in_a_transcript_is_a_non_claude_call(env):
    _home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", _assistant("a", "claude-opus-5-5"), _assistant("b", "qwen3-coder:30b"),
           _assistant("c", None))
    t = sr.transcript_figures(SID, budget_s=30)
    assert (t["claude"], t["other_model"], t["unclassified"]) == (1, 1, 1)


def test_scan_is_incremental_and_a_partial_last_line_is_read_whole_next_time(env):
    _home, projects, _codex = env
    main = _write(projects / "-p" / f"{SID}.jsonl", _assistant("a"), _assistant("b"))
    assert sr.transcript_figures(SID, budget_s=30)["claude"] == 2
    with open(main, "a", encoding="utf-8") as fh:
        fh.write(_assistant("c")[:40])                                   # a half-written line
    assert sr.transcript_figures(SID, budget_s=30)["claude"] == 2
    with open(main, "a", encoding="utf-8") as fh:
        fh.write(_assistant("c")[40:])
        fh.write(_assistant("d"))
    idx = sr._load_index(SID)
    off_before = idx["files"][str(main)]["offset"]
    assert sr.transcript_figures(SID, budget_s=30)["claude"] == 4
    assert sr._load_index(SID)["files"][str(main)]["offset"] > off_before


def test_an_unfinished_scan_says_so_and_resumes(env, monkeypatch):
    _home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(5000)])
    first = sr.transcript_figures(SID, budget_s=-1.0)                    # no time at all
    assert first["complete"] is False
    rendered = tick.render_session_routing(
        {"v": 1, "computed_at": NOW, "complete": False, "non_claude": 5, "total": 100, "local": {"attempts": 0, "ok": 0}},
        now=NOW)
    assert rendered.startswith("routed n/a (counting transcripts, session)")   # never a share from a part of the transcripts
    done = sr.transcript_figures(SID, budget_s=60)
    assert done["complete"] is True and done["claude"] == 5000


def test_a_scan_interrupted_mid_file_resumes_without_losing_the_line_it_stopped_on(env, monkeypatch):
    """The budget is checked every 2048 lines. The line it stops on must not be consumed (the old order advanced the
    offset first, so the 2048th line of an interrupted scan was never read)."""
    _home, projects, _codex = env
    main = _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(5000)])
    models: list[str] = []
    entry, finished = sr._scan_file(main, None, models, deadline=-1.0)       # expires at the first check, line 2048
    assert finished is False and len(entry["msgs"]) == 2047 and "m2047" not in entry["msgs"]
    entry, finished = sr._scan_file(main, entry, models, deadline=time.monotonic() + 60)
    assert finished is True and len(entry["msgs"]) == 5000 and "m2047" in entry["msgs"]
    assert entry["offset"] == main.stat().st_size
    # the same through the public figure: a clock that runs out at the second 2048-line check (line 4096)
    sid2 = "22222222-2222-3333-4444-555555555555"
    _write(projects / "-p" / f"{sid2}.jsonl", *[_assistant(f"x{i}") for i in range(6000)])
    calls = {"n": 0}

    def clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] <= 3 else 10.0            # deadline, file check, line-2048 check pass; line 4096 fails

    with monkeypatch.context() as m:
        m.setattr(sr.time, "monotonic", clock)
        cut = sr.transcript_figures(sid2, budget_s=1.0)
    assert cut["complete"] is False and cut["claude"] == 4095
    done = sr.transcript_figures(sid2, budget_s=60)
    assert done["complete"] is True and done["claude"] == 6000


def test_no_main_transcript_makes_the_claude_side_unknown_not_zero(env):
    home, _projects, _codex = env
    _one_local_ok(home)
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["total"] is None and r["claude"] is None and r["non_claude"] is None
    assert tick.render_session_routing(r, now=NOW) == "routed n/a (session) · local OK 100% (1/1, session)"


def test_proxy_served_message_moves_to_the_non_claude_side(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", _assistant("a"), _assistant("b"))
    _write(home / "proxy_calls.jsonl",
           json.dumps({"ts": 1, "session_id": SID, "msg_id": "b", "decision": "served"}) + "\n",
           json.dumps({"ts": 2, "session_id": "other", "msg_id": "a", "decision": "served"}) + "\n",
           json.dumps({"ts": 3, "session_id": SID, "msg_id": "a", "decision": "forwarded"}) + "\n")
    t = sr.transcript_figures(SID, budget_s=30)
    assert (t["claude"], t["proxy_served"]) == (1, 1)


# ── fixtures: router database and Codex ──────────────────────────────────────────────────────────────────

def _utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(seconds))


def _routing_db(home: Path, routes=(), events=()):
    """The two router tables. ``routes``: ``(correlation_id, session_id, tool_use_id[, trace_id])``; ``events``:
    ``(correlation_id, ts, provider, model, outcome[, trace_id])``, outcome ``ok`` / ``error`` / ``rejected`` (an
    ``execution_events`` attempt, whose ``session_id`` column holds the route's correlation id, as in the real
    ledger). A route is logged just after its last attempt, as the router does; a route with trace id ``None`` has
    none, and an event with ``None`` none."""
    con = sqlite3.connect(home / "usage.db")
    con.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, timestamp TEXT, final_provider TEXT, "
                "final_model TEXT, success INTEGER, reason_code TEXT, correlation_id TEXT, trace_id TEXT, "
                "session_id TEXT, tool_use_id TEXT, provenance TEXT DEFAULT 'runtime')")
    con.execute("CREATE TABLE execution_events (event_id TEXT, ts REAL, session_id TEXT, trace_id TEXT, "
                "event_type TEXT, provider TEXT, model TEXT, accepted INTEGER)")
    kinds = {"ok": ("attempt_completed", 1), "error": ("attempt_failed", None), "rejected": ("attempt_rejected", None)}
    for route in routes:
        corr, sid, tool = route[:3]
        trace = route[3] if len(route) > 3 else f"trace-{corr}"
        last = max([e[1] for e in events if e[0] == corr], default=NOW - 101)
        con.execute("INSERT INTO routing_decisions (timestamp, final_provider, final_model, success, reason_code, "
                    "correlation_id, trace_id, session_id, tool_use_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (_utc(last + 1), "ollama", "ollama/q", 1, "router_unhinted", corr, trace, sid, tool))
    for i, ev in enumerate(events):
        corr, ts, provider, model, outcome = ev[:5]
        trace = ev[5] if len(ev) > 5 else None
        con.execute("INSERT INTO execution_events (event_id, ts, session_id, trace_id, event_type, provider, model, "
                    "accepted) VALUES (?,?,?,?,?,?,?,?)", (f"e{i}", ts, corr, trace, kinds[outcome][0], provider, model,
                                                           kinds[outcome][1]))
    con.commit()
    con.close()


def _one_local_ok(home: Path, n: int = 1) -> None:
    """``n`` routes of the session, each one local attempt that was used."""
    _routing_db(home, [(f"c{i}", SID, None) for i in range(n)],
                [(f"c{i}", NOW - 50 + i, "ollama", "ollama/qwen3.5:latest", "ok") for i in range(n)])


def _ns(home: Path, *rows: dict):
    _write(home / "north_star_units.jsonl", *[json.dumps({"lever": "agent_route_codex", "session_id": SID, **r}) + "\n"
                                               for r in rows])


def _rollout(codex: Path, thread: str, start: float, responses, *, parent: str | None = None, fork_of: list | None = None,
             token_count_only: bool = False) -> Path:
    t = time.localtime(start)
    name = f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', t)}-{thread}.jsonl"
    folder = codex / "sessions" / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}" / f"{t.tm_mday:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    source = ({"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}} if parent else "exec")
    lines = [json.dumps({"type": "session_meta", "payload": {"id": thread, "originator": "codex_exec", "source": source,
                                                              "timestamp": "x"}})]
    for rid in (fork_of or []) + list(responses):
        if token_count_only:
            lines.append(json.dumps({"timestamp": f"t-{rid}", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"last_token_usage": {"input_tokens": 1}}}}))
        else:       # as a real rollout: the record, then a token_count that carries the same usage
            lines.append(json.dumps({"type": "token_usage_record", "payload": {"thread_id": thread, "response_id": rid}}))
            lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 1}}}}))
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}}))
    path = folder / name
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_codex_run_counts_its_responses_and_its_subagent_threads_once(env):
    _home, _p, codex = env
    start = NOW - 200
    _rollout(codex, T1, start, ["r1", "r2", "r3"])
    # the child replays r3 (a fork copies its parent's history) and adds r4, r5
    _rollout(codex, T2, start + 20, ["r4", "r5"], parent=T1, fork_of=["r3"])
    idx = cr.RolloutIndex(start - 10, NOW)
    rc = cr.count_run(idx, T1, NOW - 100)
    assert (rc.responses, rc.threads, rc.how) == (5, 2, "rollout")


def test_a_cli_without_token_usage_records_is_counted_by_token_count_events(env):
    _home, _p, codex = env
    _rollout(codex, T1, NOW - 100, ["a", "b", "c"], token_count_only=True)
    rc = cr.count_run(cr.RolloutIndex(NOW - 200, NOW), T1, NOW)
    assert (rc.responses, rc.how) == (3, "token_count")


def test_a_missing_rollout_has_no_countable_responses(env):
    idx = cr.RolloutIndex(NOW - 200, NOW)
    assert cr.count_run(idx, T3, NOW).responses is None
    assert cr.count_run(idx, "not-a-uuid", NOW).responses is None


def test_a_run_recorded_without_a_thread_is_matched_by_time_only_when_unique(env):
    _home, _p, codex = env
    start = NOW - 300
    _rollout(codex, T1, start + 1, ["r1"])
    idx = cr.RolloutIndex(start - 60, NOW)
    assert cr.thread_for_run(idx, NOW, 300.0) == T1
    _rollout(codex, T3, start + 2, ["x"])                                # two runs that started together: no guess
    idx2 = cr.RolloutIndex(start - 60, NOW)
    assert cr.thread_for_run(idx2, NOW, 300.0) is None
    # a sub-agent thread is never a run of its own
    assert cr.thread_for_run(cr.RolloutIndex(start - 60, NOW), NOW, 10_000.0) is None


def test_codex_agent_records_the_thread_the_cli_opened(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))
    body = textwrap.dedent(f"""
        import json, sys
        sys.stdout.write(json.dumps({{"type": "thread.started", "thread_id": "{T1}"}}) + "\\n")
        sys.stdout.write(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "ok"}}}}) + "\\n")
    """)
    binary = tmp_path / "codex"
    binary.write_text("#!/usr/bin/env python3\n" + body)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    import asyncio
    monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: str(binary))
    res = asyncio.run(codex_agent.run_codex("q", model="m", working_dir=str(tmp_path), timeout=20))
    assert res.success and res.thread_id == T1
    assert codex_agent.CodexResult("x", "m", 0, 0.0).thread_id == ""      # absent stays empty, not a guess


def test_agent_route_writes_the_thread_into_the_unit_row():
    path = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "agent-route.py"
    spec = importlib.util.spec_from_file_location("agent_route_thread_meta", path)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert mod._thread_meta(codex_agent.CodexResult("x", "m", 0, 1.0, thread_id=T1)) == {"thread_id": T1}
    assert mod._thread_meta(codex_agent.CodexResult("x", "m", 0, 1.0)) == {}
    assert mod._thread_meta(None) == {}


# ── the session figure ───────────────────────────────────────────────────────────────────────────────────

def test_the_session_figure_from_every_source(env):
    home, projects, codex = env
    _layout(projects)                                                      # 6 Claude calls (see the first test)
    _write(projects / "-Users-x" / f"{SID}.jsonl", _assistant("m9", tool_ids=("tu_mine",)))
    start = NOW - 400
    _rollout(codex, T1, start, ["r1", "r2", "r3"])
    _rollout(codex, T2, start + 5, ["r4"], parent=T1)
    _rollout(codex, T4, start + 200, ["b1", "b2"])
    _routing_db(home, [
        ("c1", SID, None), ("c2", SID, None), ("c3", "unknown:mcp", "tu_mine"), ("c4", "unknown:mcp", "tu_not_mine"),
        ("c5", SID, None), ("c6", SID, None), ("c7", SID, None), ("c8", SID, None), ("c9", SID, None)], [
        # c1: the first local model is not installed, the second answers: 2 attempts, 1 used
        ("c1", NOW - 90, "ollama", "ollama/qwen3.8:latest", "error"),
        ("c1", NOW - 89, "ollama", "ollama/qwen3.5:latest", "ok"),
        # c2: the router completed it, the text job's acceptance check rejected it (below): not used
        ("c2", NOW - 80, "ollama", "ollama/qwen3.5:latest", "ok"),
        # c3: an MCP call logged under unknown:mcp, joined by its tool_use id: used
        ("c3", NOW - 70, "ollama", "ollama/qwen3.5:latest", "ok"),
        # c4: another session's MCP call
        ("c4", NOW - 60, "ollama", "ollama/qwen3.5:latest", "ok"),
        ("c5", NOW - 50, "gemini", "gemini-2.5", "ok"),                    # another non-Claude provider, used
        ("c6", NOW - 40, "anthropic", "claude-sonnet-5-5", "ok"),          # a Claude answer: the Claude side
        ("c7", NOW - 30, "cache", "", "ok"),                               # a cache hit: no attempt
        ("c7", NOW - 30, "test", "m", "ok"),                               # a test provider: no attempt
        ("c8", NOW - 20, "codex", "gpt-6-astra", "ok"),                    # Codex through the router chain: one call
        ("c9", NOW - 10, "ollama", "ollama/qwen3.5:latest", "rejected"),   # answered, the router rejected it
    ])
    _write(home / "text_job_local_attempts.jsonl",
           json.dumps({"ts": NOW - 79, "job": "commit_message", "model": "ollama/qwen3.5:latest", "outcome": "rejected",
                       "session_id": SID}) + "\n")
    _ns(home, {"ts": NOW - 99, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 300.0, "thread_id": T1},
        {"ts": NOW - 98, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 5.0, "thread_id": T3},   # no rollout: 1
        {"ts": NOW - 97, "outcome": "codex_blocked", "model": "gpt-6-astra", "duration_sec": 5.0, "thread_id": T4},
        {"ts": NOW - 30, "outcome": "codex_failed", "model": "gpt-6-astra"},
        {"ts": NOW - 10, "outcome": "benched", "model": ""})                                    # not a run
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["complete"] is True
    # n counts DELEGATED runs only: 4 responses + 1 flagged; the blocked run's 2 responses are discarded work
    assert r["codex"] == {"runs": 2, "responses": 5, "flagged_runs": 1, "discarded_runs": 2, "discarded_responses": 2}
    assert r["by_source"] == {"codex_runs": 5, "router_local": 2, "router_other": 2, "proxy": 0, "transcript_other": 0}
    assert r["claude"] == 6 + 1 + 1                                          # layout + m9 + the Claude router answer
    assert r["non_claude"] == 5 + 2 + 2 == 9 and r["total"] == 17
    # b = c1 x2, c2, c3, c9; a = c1's second and c3 (c2 was rejected by the text job, c9 by the router)
    assert r["local"] == {"attempts": 5, "ok": 2, "gate_rejected": 1}
    # 17 calls is under the 50-call floor: n/a with its k/n, exactly as the 7-day row does
    assert tick.render_session_routing(r, now=NOW) == "routed n/a (9/17 calls, session) · local OK 40% (2/5, session)"
    r["total"] = 200                                                         # past the floor: the estimated run is named
    assert tick.render_session_routing(r, now=NOW) == \
        "routed 4.5% (9/200 calls, session, 1 Codex run est.) · local OK 40% (2/5, session)"


def test_a_job_rejected_three_times_is_not_three_calls_and_not_three_local_successes(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    _routing_db(home, [(f"j{i}", SID, None) for i in range(3)],
                [(f"j{i}", NOW - 100 + 10 * i, "ollama", "ollama/qwen3.5:latest", "ok") for i in range(3)])
    _write(home / "text_job_local_attempts.jsonl", *[
        json.dumps({"ts": NOW - 100 + 10 * i + 1, "job": "commit_message", "model": "ollama/qwen3.5:latest",
                    "outcome": "rejected", "session_id": SID}) + "\n" for i in range(3)])
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["local"] == {"attempts": 3, "ok": 0, "gate_rejected": 3} and r["non_claude"] == 0
    assert tick.render_session_routing(r, now=NOW) == "routed 0.0% (0/60 calls, session) · local OK 0% (0/3, session)"


def test_the_figure_is_shown_as_a_share_from_fifty_calls(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(96)])
    _one_local_ok(home, 4)
    r = sr.compute(SID, NOW, budget_s=30)
    assert (r["non_claude"], r["total"]) == (4, 100)
    assert tick.render_session_routing(r, now=NOW) == "routed 4.0% (4/100 calls, session) · local OK 100% (4/4, session)"


def test_no_local_attempt_is_na_never_zero_percent(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    _routing_db(home)
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["local"] == {"attempts": 0, "ok": 0, "gate_rejected": 0} and r["non_claude"] == 0
    line = tick.render_session_routing(r, now=NOW)
    assert "local OK n/a (0 attempts, session)" in line and "0%" not in line.split("local OK")[1]
    assert line.startswith("routed 0.0% (0/60 calls, session)")              # a measured zero: n is shown beside it


def test_an_unreadable_router_database_is_unknown_not_zero(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    (home / "usage.db").write_text("not a database")
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] is None and r["local"] is None and r["total"] is None
    assert tick.render_session_routing(r, now=NOW) == "routed n/a (session) · local OK n/a (session)"


def test_a_fresh_result_is_reused_and_a_stale_one_recomputed(env):
    home, projects, _codex = env
    main = _write(projects / "-p" / f"{SID}.jsonl", _assistant("a"))
    _routing_db(home)
    first = sr.compute(SID, NOW, budget_s=30)
    with open(main, "a", encoding="utf-8") as fh:
        fh.write(_assistant("b"))
    assert sr.compute(SID, NOW + 5, budget_s=30)["claude"] == first["claude"] == 1       # inside FRESH_S: reused
    assert sr.compute(SID, NOW + 60, budget_s=30)["claude"] == 2


def test_an_unsafe_session_id_is_refused(env):
    assert sr.compute("../../etc", NOW) is None and sr.compute("", NOW) is None


# ── the snapshot and the renderers ───────────────────────────────────────────────────────────────────────

def test_the_snapshot_carries_the_routing_figure_and_its_row(env, monkeypatch):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(80)])
    _one_local_ok(home)
    data = snap.build(SID, NOW)
    assert data["routing"]["total"] == 81 and data["routing"]["non_claude"] == 1
    assert data["routing_line"] == "routed 1.2% (1/81 calls, session) · local OK 100% (1/1, session)"
    assert "routed" not in data["line"]                                      # the five-field line is unchanged


def test_a_failed_count_is_na_in_the_snapshot_and_does_not_blank_the_other_fields(env, monkeypatch):
    monkeypatch.setattr(sr, "compute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    data = snap.build(SID, NOW)
    assert data["routing"] is None
    assert data["routing_line"] == "routed n/a (session) · local OK n/a (session)"
    assert data["line"].startswith("router:")


def test_the_tick_prints_the_session_row_between_the_line_and_the_seven_day_row(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(80)])
    _one_local_ok(home)
    snap.refresh(host="claude_code", session_id=SID, now=NOW)
    routing = tick.read_session_routing(str(home), SID)
    cache = {"written_at": NOW, "call_mix": {"window": "7d", "population": "organic",
                                              "outside_claude": {"k": 6, "n": 600}, "below_opus": {"k": 0, "n": 594},
                                              "local_ok": {"k": 9, "n": 10}}}
    rows = tick.render_rows(cache, now=NOW, routing=routing, session=True)
    assert len(rows) == 3
    assert rows[1].startswith("routed 1.2% (1/81 calls, session)")
    assert rows[2] == ("off-Claude 1.0% (6/600 calls, 7d, organic) · <Opus 0.0% (0/594 Claude calls, 7d, organic)"
                       " · local OK 90% (9/10, 7d, organic)")
    assert len(tick.render_rows(cache, now=NOW)) == 2                         # no session in the JSON: no session row


def test_the_seven_day_label_says_when_codex_turns_are_counted_and_flags_estimated_runs():
    base = {"window": "7d", "population": "organic", "outside_claude": {"k": 105, "n": 13977},
            "below_opus": {"k": 0, "n": 13872}, "local_ok": {"k": 3, "n": 4}}
    cache = {"written_at": NOW, "call_mix": dict(base, codex_turns=True, flagged_codex_runs=0)}
    assert tick.render_progress(cache, now=NOW).startswith(
        "routed (incl. Codex turns and binds) 0.75% (105/13977 calls, 7d, organic) · <Opus")
    cache["call_mix"]["flagged_codex_runs"] = 2
    assert "(105/13977 calls, 7d, organic, 2 Codex runs est.)" in tick.render_progress(cache, now=NOW)
    cache["call_mix"]["flagged_codex_runs"] = 1
    assert "(105/13977 calls, 7d, organic, 1 Codex run est.)" in tick.render_progress(cache, now=NOW)
    # a cache from a refresher that did not count Codex turns keeps the name `llm-router kpi` prints
    assert tick.render_progress({"written_at": NOW, "call_mix": base}, now=NOW).startswith("off-Claude 0.75% (105/13977")
    for bad in (True, 1, "yes"):                                              # only the boolean True relabels
        cache["call_mix"]["codex_turns"] = bad
        if bad is not True:
            assert tick.render_progress(cache, now=NOW).startswith("off-Claude")


def test_the_seven_day_row_is_as_before_when_the_cache_has_no_local_ok(env):
    cache = {"written_at": NOW, "call_mix": {"window": "7d", "population": "organic",
                                              "outside_claude": {"k": 6, "n": 600}, "below_opus": {"k": 0, "n": 594}}}
    assert "local OK" not in tick.render_progress(cache, now=NOW)
    cache["call_mix"]["local_ok"] = {"k": 0, "n": 0}
    assert tick.render_progress(cache, now=NOW).endswith("local OK n/a (0 attempts, 7d, organic)")
    for bad in ({"k": 3, "n": 2}, {"k": -1, "n": 2}, {"k": True, "n": 1}, {"k": "x", "n": 1}, "str", None):
        cache["call_mix"]["local_ok"] = bad
        assert "local OK" not in tick.render_progress(cache, now=NOW)


def test_the_session_row_never_prints_cache_text_or_a_stale_figure():
    good = {"v": 1, "computed_at": NOW, "complete": True, "non_claude": 10, "total": 100,
            "local": {"attempts": 4, "ok": 2}, "codex": {"flagged_runs": 0}}
    assert tick.render_session_routing(good, now=NOW) == "routed 10.0% (10/100 calls, session) · local OK 50% (2/4, session)"
    assert tick.render_session_routing(good, now=NOW + tick.STALE_AFTER_S + 1) == \
        "routed n/a (session) · local OK n/a (session)"
    hostile = dict(good, non_claude="\x1b[31mred", total=100, local={"attempts": "\x1b[0m", "ok": 1},
                   codex={"flagged_runs": "\x1b[1m"})
    out = tick.render_session_routing(hostile, now=NOW)
    assert "\x1b" not in out and out == "routed n/a (session) · local OK n/a (session)"
    assert tick.render_session_routing(dict(good, v=2), now=NOW).startswith("routed n/a")
    # below the share floor: n/a with the same k/n, as the 7-day row
    assert tick.render_session_routing(dict(good, total=40), now=NOW).startswith("routed n/a (10/40 calls, session)")


def test_the_tick_reads_the_snapshot_by_its_file_name_characters(env):
    home, _p, _c = env
    d = home / "snapshots"
    d.mkdir()
    (d / "claude_code-a_b.json").write_text(json.dumps({"routing": {"v": 1}}))
    assert tick.read_session_routing(str(home), "a/b") == {"v": 1}
    assert tick.read_session_routing(str(home), None) is None
    assert tick.read_session_routing(str(home), "missing") is None


# ── the 7-day window ─────────────────────────────────────────────────────────────────────────────────────

def test_the_window_counts_codex_responses_and_local_ok_for_the_organic_population(env):
    home, _projects, codex = env
    _rollout(codex, T1, NOW - 400, ["r1", "r2", "r3", "r4"])
    ok, bad = "ok", "error"
    _routing_db(home, [
        ("o1", "organic-s", None), ("o2", "organic-s", None), ("r1", "research-s", None), ("n1", None, None),
        ("m1", "unknown:mcp", "tu_organic"), ("m2", "unknown:mcp", "tu_research"), ("m3", "unknown:mcp", "tu_nobody"),
        ("old", "organic-s", None)], [
        ("o1", NOW - 3600, "ollama", "ollama/q", bad), ("o1", NOW - 3599, "ollama", "ollama/q2", ok),
        ("o2", NOW - 3500, "ollama", "ollama/q2", ok),
        ("r1", NOW - 3400, "ollama", "ollama/q", ok),                          # not the population
        ("n1", NOW - 3300, "ollama", "ollama/q", ok),                          # NULL session, no tool id: unplaceable
        ("m1", NOW - 3200, "ollama", "ollama/q", ok),                          # unknown:mcp placed by tool_use id: organic
        ("m2", NOW - 3100, "ollama", "ollama/q", ok),                          # placed, research: out
        ("m3", NOW - 3000, "ollama", "ollama/q", ok),                          # unknown:mcp, no transcript: unplaceable
        ("old", NOW - 86400 * 30, "ollama", "ollama/q", ok),                   # outside the window
        ("o2", NOW - 2900, "gemini", "gemini-2.5", ok),                        # not local: not in the local figure
    ])
    _write(home / "text_job_local_attempts.jsonl",
           json.dumps({"ts": NOW - 3499, "job": "j", "model": "ollama/q2", "outcome": "rejected",
                       "session_id": "organic-s"}) + "\n")
    _ns(home, {"ts": NOW - 300, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 100.0, "thread_id": T1,
               "session_id": "organic-s", "session_kind": "organic"},
        {"ts": NOW - 250, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 5.0, "thread_id": T3,
         "session_id": "organic-s", "session_kind": "organic"},
        {"ts": NOW - 200, "outcome": "codex_failed", "model": "gpt-6-astra", "session_id": "organic-s",
         "session_kind": "organic"},
        {"ts": NOW - 150, "outcome": "codex_blocked", "model": "gpt-6-astra", "duration_sec": 5.0, "thread_id": T4,
         "session_id": "organic-s", "session_kind": "organic"},
        {"ts": NOW - 100, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 5.0, "thread_id": T3,
         "session_id": "research-s", "session_kind": "research"})

    def organic(sid, stamp):
        return sid == "organic-s" or sid == "sess-from-tool"

    x = sr.window_extras(NOW - 7 * 86400, NOW, NOW, organic, {"tu_organic": "sess-from-tool", "tu_research": "research-s"})
    # the unit list counts one unit per row: delegated 4 responses = +3, delegated with no rollout = +0 (flagged),
    # failed and blocked are discarded work and leave the count (-1 each); the research run is out
    assert x["codex_extra"] == 3 + 0 - 1 - 1 and x["runs"] == 2 and x["flagged_runs"] == 1
    assert x["by_session"] == {"organic-s": 3 - 1 - 1}
    # organic local attempts: o1 x2, o2 (rejected by the text job: not used), m1 = 4 attempts, 2 used
    assert x["local"] == {"attempts": 4, "ok": 2, "gate_rejected": 1}
    assert x["unattributed_local"] == 2                                       # n1 and m3


def test_call_mix_adds_the_extras_to_the_unit_counts():
    from llm_router.commands import kpi

    class _Index:
        def resolve(self, sid, stamp=None):
            return type("R", (), {"kind": "organic"})()

    units = [{"kind": "claude_main_call", "model": "claude-opus-5-5", "session_id": "s", "ts": None, "session_kind": "organic"}
             for _ in range(8)]
    units.append({"kind": "agent_route_codex", "model": "gpt-6-astra", "session_id": "s", "ts": None, "session_kind": "organic"})
    base = kpi._call_mix(units, frozenset({"organic"}), _Index())
    assert base["outside_claude"] == {"k": 1, "n": 9} and "local_ok" not in base
    extras = {"codex_extra": 4, "by_session": {"s": 4}, "runs": 1, "flagged_runs": 0,
              "local": {"ok": 3, "attempts": 5}, "unattributed_local": 2}
    cm = kpi._call_mix(units, frozenset({"organic"}), _Index(), sessions=True, extras=extras)
    assert cm["outside_claude"] == {"k": 5, "n": 13} and cm["sessions"] == {"s": 13}
    # a discarded Codex run leaves the count: the unit the list counted is taken back
    gone = kpi._call_mix(units, frozenset({"organic"}), _Index(), sessions=True,
                         extras=dict(extras, codex_extra=-1, by_session={"s": -1}))
    assert gone["outside_claude"] == {"k": 0, "n": 8} and gone["sessions"] == {"s": 8}
    assert cm["local_ok"] == {"k": 3, "n": 5, "unattributed_attempts": 2}
    assert cm["codex"] == {"runs": 1, "extra_responses": 4, "flagged_runs": 0, "bind_jobs": 0}


def test_the_refresher_caches_local_ok_and_the_flagged_runs():
    from llm_router import statusline_refresh as rf

    card = {"window_days": 7, "call_mix": {
        "population": "organic", "outside_claude": {"k": 5, "n": 100}, "below_opus": {"k": 0, "n": 95},
        "local_ok": {"k": 3, "n": 4, "unattributed_attempts": 7}, "codex": {"flagged_runs": 2}}}
    cm = rf._call_mix(card)
    assert cm["local_ok"] == {"k": 3, "n": 4} and cm["flagged_codex_runs"] == 2 and cm["codex_turns"] is True
    card["call_mix"]["local_ok"] = {"k": 5, "n": 4}                          # impossible: not cached, the row omits it
    assert "local_ok" not in rf._call_mix(card)
    del card["call_mix"]["local_ok"]                                         # an older scorecard: as before
    assert "local_ok" not in rf._call_mix(card)


def test_the_files_the_refresher_writes_are_private(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", _assistant("a"))
    _routing_db(home)
    sr.compute(SID, NOW, budget_s=30)
    assert stat.S_IMODE(os.stat(paths.state_path(sr.INDEX_DIR, f"{SID}.json")).st_mode) == 0o600


def test_the_tick_script_prints_the_session_row_only_once_the_session_has_a_snapshot(env, tmp_path):
    import subprocess
    import sys

    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(80)])
    _one_local_ok(home)
    script = Path(tick.__file__)
    run_env = {**os.environ, "NO_COLOR": "1", "LLM_ROUTER_STATUSLINE_REFRESH_CMD": "true"}

    def rows():
        out = subprocess.run([sys.executable, "-I", "-S", str(script)], input=json.dumps({"session_id": SID}),
                             env=run_env, capture_output=True, text=True, timeout=30).stdout
        return out.splitlines()

    assert len(rows()) == 1                                                   # cold session: the line only
    snap.refresh(host="claude_code", session_id=SID)                          # the detached refresher's job
    got = rows()
    assert len(got) == 2 and got[1] == "routed 1.2% (1/81 calls, session) · local OK 100% (1/1, session)"


# ── the full layout's third row, from a segment cache the test writes (no refresher, no installed package) ──

def _full_layout(tmp_path: Path, kv: dict[str, str], stdin_sid: str = SID) -> list[str]:
    import subprocess

    script = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "statusline-command.sh"
    state = tmp_path / ".llm-router"
    state.mkdir(exist_ok=True)
    (state / f"statusline_seg_{stdin_sid}.kv").write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    env = {"HOME": str(tmp_path), "LLM_ROUTER_HOME": str(state), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "NO_COLOR": "1", "LLM_ROUTER_STATUSLINE_REFRESH_CMD": "true"}
    out = subprocess.run(["bash", str(script)], input=json.dumps({"session_id": stdin_sid}), env=env,
                         capture_output=True, text=True, timeout=30).stdout
    return out.splitlines()


def test_the_full_layout_prints_the_routing_row_under_the_snapshot_row(tmp_path):
    now = int(time.time())
    row = "routed 0.74% (104/13976 calls, session) · local OK 40% (2/5, session)"
    lines = _full_layout(tmp_path, {"v": "1", "written": str(now), "snap_line": "router: m (r) | offload n/a",
                                    "snap_at": str(now), "route_line": row})
    assert len(lines) == 3, lines
    assert lines[1].startswith("router: ") and lines[2] == row


def test_a_stale_snapshot_turns_both_rows_to_na_and_a_control_character_never_reaches_the_terminal(tmp_path):
    now = int(time.time())
    lines = _full_layout(tmp_path, {"v": "1", "written": str(now), "snap_line": "router: m (r)",
                                    "snap_at": str(now - 3600), "route_line": "routed 1.0% (1/100 calls, session)"})
    assert lines[1:] == ["n/a", "routed n/a (session) · local OK n/a (session)"], lines
    hostile = _full_layout(tmp_path, {"v": "1", "written": str(now), "snap_line": "router: m",
                                      "snap_at": str(now), "route_line": "routed \x1b[31m1.0%\x1b[0m \u009b(1/100)"})
    assert "\x1b" not in hostile[2] and "\u009b" not in hostile[2], hostile


def test_a_segment_cache_without_a_routing_row_prints_none(tmp_path):
    now = int(time.time())
    lines = _full_layout(tmp_path, {"v": "1", "written": str(now), "snap_line": "router: m", "snap_at": str(now)})
    assert len(lines) == 2 and lines[1] == "router: m"


def test_two_runs_with_the_same_timestamp_are_two_runs_not_one_cache_entry(env):
    home, _projects, codex = env
    _rollout(codex, T1, NOW - 600, ["a1", "a2"])
    _rollout(codex, T4, NOW - 590, ["b1", "b2", "b3"])
    rows = [{"lever": "agent_route_codex", "session_id": SID, "ts": NOW - 500, "outcome": "delegated", "model": "m",
             "thread_id": T1, "tool_use_id": "tu1"},
            {"lever": "agent_route_codex", "session_id": SID, "ts": NOW - 500, "outcome": "delegated", "model": "m",
             "thread_id": T4, "tool_use_id": "tu2"}]
    assert [r["responses"] for r in sr.codex_run_responses(rows, NOW)] == [2, 3]
    assert [r["responses"] for r in sr.codex_run_responses(rows, NOW)] == [2, 3]      # and again from the cache
    assert sr.run_key(rows[0]) != sr.run_key(rows[1])


# ── delta review: identity, not model and time ──────────────────────────────────────────────────────────

SID_B = "bbbbbbbb-2222-3333-4444-555555555555"


def _rejection(ts: float, **identity) -> str:
    return json.dumps({"ts": ts, "job": "commit_message", "model": "ollama/qwen3.5:latest", "outcome": "rejected",
                       **identity}) + "\n"


def test_another_sessions_text_job_rejection_cannot_suppress_my_accepted_output(env):
    """M1: the rejection ledger is global; matching by model and time alone marked a neighbour's accepted attempt."""
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    _routing_db(home, [("a1", SID, None)], [("a1", NOW - 80, "ollama", "ollama/qwen3.5:latest", "ok")])
    _write(home / "text_job_local_attempts.jsonl", _rejection(NOW - 79, session_id=SID_B, tool_use_id="tu_of_b"))
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["local"] == {"attempts": 1, "ok": 1, "gate_rejected": 0}


def test_a_rejection_matches_by_job_then_by_session_and_never_without_an_identity(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    cases = {                                    # rejection identity -> is my attempt marked?
        "same session, no tool id": ({"session_id": SID}, True),
        "same tool id": ({"session_id": "unknown:mcp", "tool_use_id": "tu_mine"}, True),
        "same session, another job": ({"session_id": SID, "tool_use_id": "tu_other_job"}, False),
        "another session": ({"session_id": SID_B}, False),
        "no identity (a row written before the ledger carried one)": ({}, False),
    }
    for name, (identity, expect) in cases.items():
        (home / "usage.db").unlink(missing_ok=True)
        (home / "text_job_local_attempts.jsonl").unlink(missing_ok=True)
        shutil.rmtree(home / sr.INDEX_DIR, ignore_errors=True)
        _routing_db(home, [("a1", SID, "tu_mine")], [("a1", NOW - 80, "ollama", "ollama/qwen3.5:latest", "ok")])
        _write(home / "text_job_local_attempts.jsonl", _rejection(NOW - 79, **identity))
        r = sr.compute(SID, NOW, budget_s=30)
        assert (r["local"]["gate_rejected"] == 1) is expect, (name, r["local"])


def test_the_session_and_the_seven_day_population_apply_one_gate(env):
    """S4: the same attempts and the same rejections give the same marks in both populations."""
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    _routing_db(home, [("a1", SID, "tu1"), ("a2", SID, "tu2"), ("b1", SID_B, "tu3")], [
        ("a1", NOW - 80, "ollama", "ollama/qwen3.5:latest", "ok"),
        ("a2", NOW - 60, "ollama", "ollama/qwen3.5:latest", "ok"),
        ("b1", NOW - 79, "ollama", "ollama/qwen3.5:latest", "ok")])
    _write(home / "text_job_local_attempts.jsonl", _rejection(NOW - 79, session_id=SID, tool_use_id="tu1"))
    session = sr.compute(SID, NOW, budget_s=30)["local"]

    def mine(sid, stamp):
        return sid == SID

    window = sr.window_extras(NOW - 7 * 86400, NOW, NOW, mine, {})["local"]
    assert session == {"attempts": 2, "ok": 1, "gate_rejected": 1}
    assert {k: window[k] for k in ("attempts", "ok", "gate_rejected")} == session


def test_a_text_job_row_carries_its_session_and_tool_use_id(env, monkeypatch):
    from llm_router import call_identity, text_job_ledger as tj

    home, _p, _c = env
    monkeypatch.setattr(tj, "_resolve_session_id", lambda: SID)
    token = call_identity.bind("toolu_text_job")
    try:
        tj._append_local_attempt("commit_message", "ollama/qwen3.5:latest", tj.OUTCOME_REJECTED)
    finally:
        call_identity.reset(token)
    tj._append_local_attempt("commit_message", "ollama/qwen3.5:latest", tj.OUTCOME_OK)        # outside an MCP call
    rows = [json.loads(line) for line in (home / tj.LOCAL_ATTEMPTS_FILENAME).read_text().splitlines()]
    assert rows[0]["session_id"] == SID and rows[0]["tool_use_id"] == "toolu_text_job"
    assert rows[1]["session_id"] == SID and "tool_use_id" not in rows[1]


def test_a_colliding_correlation_id_cannot_import_another_sessions_attempts(env):
    """M2: the id is 8 hex characters. Session B's route shares it; its attempts must not be mine."""
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    _routing_db(home, [("abcd1234", SID, None, "trace-mine"), ("abcd1234", SID_B, None, "trace-theirs")], [
        ("abcd1234", NOW - 50, "ollama", "ollama/qwen3.5:latest", "ok", "trace-mine"),
        ("abcd1234", NOW - 40, "ollama", "ollama/qwen3.5:latest", "ok", "trace-theirs"),     # another route, same 8 hex
        ("abcd1234", NOW - 3 * 3600, "ollama", "ollama/qwen3.5:latest", "error", None),      # same id, hours earlier, no trace
        ("abcd1234", NOW - 45, "ollama", "ollama/qwen3.5:latest", "error", None),            # no trace, inside the window: mine
    ])
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["local"] == {"attempts": 2, "ok": 1, "gate_rejected": 0}


def test_two_routes_that_both_fit_a_colliding_id_make_the_attempt_unplaceable(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    _routing_db(home, [("abcd1234", SID, "tu1", None), ("abcd1234", SID, "tu2", None)],
                [("abcd1234", NOW - 50, "ollama", "ollama/qwen3.5:latest", "ok", None)])
    assert sr.compute(SID, NOW, budget_s=30)["local"] == {"attempts": 0, "ok": 0, "gate_rejected": 0}


def test_the_legacy_token_count_fallback_does_not_double_count_replayed_parent_history(env):
    """M3: a forked child replays its parent's lines; counted as a set of line digests across the run's files."""
    _home, _p, codex = env
    start = NOW - 200
    _rollout(codex, T1, start, ["r1", "r2", "r3"], token_count_only=True)
    _rollout(codex, T2, start + 20, ["r4", "r5"], parent=T1, fork_of=["r2", "r3"], token_count_only=True)
    rc = cr.count_run(cr.RolloutIndex(start - 10, NOW), T1, NOW - 100)
    assert (rc.responses, rc.threads, rc.how) == (5, 2, "token_count")


def test_unavailable_attempt_data_is_unknown_in_the_seven_day_figure_not_zero_attempts(env):
    """M4: no execution_events table means no figure, and the row says n/a, never `0 attempts`."""
    home, _projects, _codex = env
    con = sqlite3.connect(home / "usage.db")                                 # a database with no attempt ledger
    con.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, correlation_id TEXT, session_id TEXT, "
                "timestamp TEXT)")
    con.commit()
    con.close()

    def anyone(sid, stamp):
        return True

    x = sr.window_extras(NOW - 7 * 86400, NOW, NOW, anyone, {})
    assert x["local"] is None and x["unattributed_local"] is None
    from llm_router.commands import kpi

    class _Index:
        def resolve(self, sid, stamp=None):
            return type("R", (), {"kind": "organic"})()

    cm = kpi._call_mix([{"kind": "claude_main_call", "model": "claude-opus-5-5", "session_id": "s", "ts": None,
                         "session_kind": "organic"}] * 3, frozenset({"organic"}), _Index(), extras=x)
    assert "local_ok" not in cm and cm["codex"]["runs"] == 0
    from llm_router import statusline_refresh as rf

    cached = rf._call_mix({"window_days": 7, "call_mix": {**cm, "outside_claude": {"k": 0, "n": 3},
                                                            "below_opus": {"k": 0, "n": 3}, "population": "organic"}})
    assert cached["codex_turns"] is True and "local_ok" not in cached
    row = tick.render_progress({"written_at": NOW, "call_mix": cached}, now=NOW)
    assert row.endswith("local OK n/a (7d, organic)") and "0 attempts" not in row


# ── text-job-bind jobs: the hook runs outside MCP, so its router rows name no session ───────────────────────

LOCAL_MODEL = "ollama/qwen3.6:35b-a3b-coding"
JID = "063cddf43b57"


def _bind(home: Path, ts: float, *, session: str = SID, kind: str = "bound", outcome: str = "accepted",
          model: str = LOCAL_MODEL, latency_ms: int = 18791, jid: str | None = JID, tool: str = "toolu_bash") -> None:
    row = {"kind": kind, "job": "commit_message", "outcome": outcome, "session_id": session, "tool_use_id": tool,
           "latency_ms": latency_ms, "model": model, "attempts": 1, "ts": ts}
    if jid:
        row["jid"] = jid
    _write(home / "text_job_binds.jsonl", json.dumps(row) + "\n")


def _attempt_rows(home: Path, *rows: tuple) -> None:
    """``(ts, outcome, jid[, model])`` rows of ``text_job_local_attempts.jsonl``, as the bind hook wrote them."""
    _write(home / "text_job_local_attempts.jsonl", *[json.dumps({
        "ts": r[0], "job": "commit_message", "model": r[3] if len(r) > 3 else LOCAL_MODEL, "outcome": r[1],
        "jid": r[2], "try": 1}) + "\n" for r in rows])


def _bound_session(env, *, claude: int = 60):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(claude)])
    return home


def _local(r: dict) -> tuple:
    return (r["local"]["attempts"], r["local"]["ok"], r["local"]["gate_rejected"])


def test_an_accepted_bind_job_is_one_call_and_its_attempts_are_found_by_its_jid(env):
    """The 2026-11-04 10:39Z commit: accepted, written by the local model, router rows under `unknown:non_mcp`."""
    home = _bound_session(env)
    t = NOW - 100
    _routing_db(home, [("4b11e0a0", "unknown:non_mcp", None)], [("4b11e0a0", t - 0.01, "ollama", LOCAL_MODEL, "ok")])
    before = sr.compute(SID, NOW, budget_s=30)
    assert (before["non_claude"], before["local"]["attempts"]) == (0, 0)     # the router rows are not the session's
    _bind(home, t)
    _attempt_rows(home, (t - 20, "rejected", JID), (t - 0.005, "ok", JID))
    shutil.rmtree(home / sr.INDEX_DIR)
    r = sr.compute(SID, NOW, budget_s=30)
    assert (r["non_claude"], r["total"]) == (1, 61) and r["by_source"]["router_local"] == 1
    assert _local(r) == (2, 1, 1)                                             # the rejected try and the accepted one
    assert tick.render_session_routing(r, now=NOW) == "routed 1.6% (1/61 calls, session) · local OK 50% (1/2, session)"


def test_a_bind_row_without_a_jid_is_one_call_and_borrows_no_attempts(env):
    """A row written before the hook recorded the jid: its own row proves the call and one used attempt, nothing
    more, even though exactly one attempt group fits it in time."""
    home = _bound_session(env)
    t = NOW - 100
    _routing_db(home)
    _bind(home, t, jid=None)
    _attempt_rows(home, (t - 20, "rejected", "jOther"), (t - 0.005, "ok", "jOther"))       # time-adjacent, not its jid
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] == 1 and _local(r) == (1, 1, 0)


def test_a_codex_served_bind_never_inherits_an_unrelated_local_ok(env):
    home = _bound_session(env)
    t = NOW - 100
    _routing_db(home)
    _bind(home, t, model="codex/gpt-6-astra", jid=None)
    _attempt_rows(home, (t - 0.005, "ok", "jOther"))                          # another job's attempt, a heartbeat away
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] == 1 and r["by_source"]["router_other"] == 1 and r["by_source"]["router_local"] == 0
    assert _local(r) == (0, 0, 0)
    # with its own jid the job's failed local tries are its own and are counted
    _bind(home, t - 50, model="codex/gpt-6-astra", jid="jOwn", tool="toolu_other")
    _attempt_rows(home, (t - 50.005, "error", "jOwn"), (t - 50.004, "infra", "jOwn"))
    shutil.rmtree(home / sr.INDEX_DIR)
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] == 2 and _local(r) == (2, 0, 0)


def test_a_job_that_was_handed_back_adds_its_attempts_to_b_and_nothing_to_n(env):
    """Claude then writes the text itself: the local tries were still made."""
    home = _bound_session(env)
    t = NOW - 100
    _routing_db(home)
    _bind(home, t, kind="denied", outcome="rejected", jid="jRej")
    _attempt_rows(home, (t - 30, "rejected", "jRej"), (t - 20, "ok", "jRej"), (t - 10, "error", "jRej"))
    _bind(home, t + 10, kind="denied", outcome="fallback", jid="jFb", tool="toolu_2")
    _attempt_rows(home, (t + 9, "infra", "jFb"))
    _bind(home, t + 20, kind="denied", outcome="unsupported", jid=None, tool="toolu_3")     # ran nothing, names nothing
    _bind(home, t + 30, kind="handwritten", outcome="none", jid=None, tool="toolu_4")
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] == 0 and _local(r) == (4, 0, 1)                    # a = 0: nothing of a handed-back job is used
    assert r["by_source"]["router_local"] == 0


def test_only_this_sessions_jobs_count_and_rows_naming_another_session_are_not_linked(env):
    home = _bound_session(env)
    t = NOW - 100
    _routing_db(home)
    _bind(home, t - 50, session=SID_B, jid="jB")                              # another session's job
    _attempt_rows(home, (t - 50.005, "ok", "jB"))
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] == 0 and _local(r) == (0, 0, 0)
    _bind(home, t - 10, model="codex/gpt-6-astra", jid="jMine", tool="toolu_m")
    _write(home / "text_job_local_attempts.jsonl", json.dumps({
        "ts": t - 10.005, "job": "j", "model": LOCAL_MODEL, "outcome": "error", "jid": "jMine", "session_id": SID_B}) + "\n")
    shutil.rmtree(home / sr.INDEX_DIR)
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] == 1 and _local(r) == (0, 0, 0)                    # rows that name another session: not its rows


def test_an_unrelated_same_model_call_in_the_jobs_window_is_not_removed(env):
    """M1: the window and the model are not an identity. Another MCP call of the session on the same model, and a
    second bind job whose window overlaps the first, both stay."""
    home = _bound_session(env)
    t = NOW - 100
    _routing_db(home, [("c1", SID, "tu_mcp"), ("c2", SID, "toolu_bash")],
                [("c1", t - 5, "ollama", LOCAL_MODEL, "ok"),                      # an MCP call, same model, inside the window
                 ("c2", t - 0.01, "ollama", LOCAL_MODEL, "ok")])                  # a route that carries THIS job's tool_use id
    _bind(home, t, jid="jA", tool="toolu_bash")
    _attempt_rows(home, (t - 0.005, "ok", "jA"))
    _bind(home, t + 4, jid="jB", tool="toolu_other")                            # overlaps the first job's window
    _attempt_rows(home, (t + 3.995, "ok", "jB"))
    r = sr.compute(SID, NOW, budget_s=30)
    # kept: the MCP call, job A's attempt, job B's attempt. Removed: only the route carrying A's tool_use id (same work).
    assert _local(r) == (3, 3, 0) and r["non_claude"] == 3


def test_a_missing_bind_ledger_is_nothing_recorded_and_an_unreadable_one_is_unknown(env):
    home = _bound_session(env)
    _routing_db(home, [("c1", SID, None)], [("c1", NOW - 50, "ollama", LOCAL_MODEL, "ok")])
    r = sr.compute(SID, NOW, budget_s=30)                                     # no text_job_binds.jsonl at all
    assert r["non_claude"] == 1 and _local(r) == (1, 1, 0)
    (home / "text_job_binds.jsonl").mkdir()                                   # exists, and cannot be read as a file
    shutil.rmtree(home / sr.INDEX_DIR)
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["non_claude"] is None and r["total"] is None and r["local"] is None
    assert tick.render_session_routing(r, now=NOW) == "routed n/a (session) · local OK n/a (session)"


def test_an_unreadable_attempt_ledger_is_unknown_not_an_empty_one(env):
    home = _bound_session(env)
    t = NOW - 100
    _routing_db(home)
    _bind(home, t)
    (home / "text_job_local_attempts.jsonl").mkdir()
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["local"] is None and r["non_claude"] is None                      # not "1 call, 1 of 1": the rows are unknown
    x = sr.window_extras(NOW - 7 * 86400, NOW, NOW, lambda sid, stamp: sid == SID, {})
    assert x["local"] is None and x["bind_extra"] == 1                         # the call count is the bind row's own
    (home / "text_job_binds.jsonl").unlink()
    (home / "text_job_binds.jsonl").mkdir()
    with pytest.raises(sr.LedgerUnreadable):
        sr.window_extras(NOW - 7 * 86400, NOW, NOW, lambda sid, stamp: sid == SID, {})


def test_the_seven_day_population_counts_the_same_jobs(env):
    home, _projects, _codex = env
    t = NOW - 100
    _routing_db(home, [("4b11e0a0", "unknown:non_mcp", None)], [("4b11e0a0", t - 0.01, "ollama", LOCAL_MODEL, "ok")])
    _bind(home, t)                                                            # organic, accepted
    _attempt_rows(home, (t - 0.005, "ok", JID))
    _bind(home, t - 50, session=SID_B, jid="jB")                              # not the population
    _attempt_rows(home, (t - 49.995, "ok", "jB"))
    _bind(home, t - 30, kind="denied", outcome="rejected", jid="jR", tool="toolu_r")   # organic, handed back
    _attempt_rows(home, (t - 30.005, "rejected", "jR"))

    def organic(sid, stamp):
        return sid == SID

    x = sr.window_extras(NOW - 7 * 86400, NOW, NOW, organic, {})
    assert x["bind_extra"] == 1 and x["by_session"] == {SID: 1}              # the handed-back job is no call
    assert x["local"] == {"attempts": 2, "ok": 1, "gate_rejected": 1}
    assert x["unattributed_local"] == 1                                       # the hook's unplaced router event stays unplaced
    from llm_router.commands import kpi

    class _Index:
        def resolve(self, sid, stamp=None):
            return type("R", (), {"kind": "organic"})()

    units = [{"kind": "claude_main_call", "model": "claude-opus-5-5", "session_id": SID, "ts": None,
              "session_kind": "organic"} for _ in range(9)]
    cm = kpi._call_mix(units, frozenset({"organic"}), _Index(), sessions=True, extras=x)
    assert cm["outside_claude"] == {"k": 1, "n": 10} and cm["sessions"] == {SID: 10} and cm["codex"]["bind_jobs"] == 1
