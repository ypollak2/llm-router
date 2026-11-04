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


def test_no_main_transcript_makes_the_claude_side_unknown_not_zero(env):
    home, _projects, _codex = env
    _routing_db(home, [(1, "2026-11-05 08:00:00", "ollama", "ollama/q", 1, "router_chain", SID, None)])
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


def _routing_db(home: Path, rows):
    con = sqlite3.connect(home / "usage.db")
    con.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, timestamp TEXT, final_provider TEXT, "
                "final_model TEXT, success INTEGER, reason_code TEXT, session_id TEXT, tool_use_id TEXT, "
                "provenance TEXT DEFAULT 'runtime')")
    con.executemany("INSERT INTO routing_decisions (id, timestamp, final_provider, final_model, success, reason_code, "
                    "session_id, tool_use_id) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


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
            lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 1}}}}))
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
    _write(projects / "-Users-x" / f"{SID}.jsonl", _assistant("m9", tool_ids=("tu_mcp_other_session",)))
    start = NOW - 400
    _rollout(codex, T1, start, ["r1", "r2", "r3"])
    _rollout(codex, T2, start + 5, ["r4"], parent=T1)
    ts = _utc
    _routing_db(home, [
        (1, ts(NOW - 50), "ollama", "ollama/qwen3.5", 1, "router_chain", SID, None),           # local, worked
        (2, ts(NOW - 49), "ollama", "ollama/qwen3.5", 0, "router_chain", SID, None),           # local, failed: an attempt
        (3, ts(NOW - 48), "ollama", "ollama/qwen3.5", 1, "error_cancelled", SID, None),        # cancelled: no call
        (4, ts(NOW - 47), "cache", "", 1, "cache_hit", SID, None),                             # cache hit: no call
        (5, ts(NOW - 46), "ollama", "ollama/q", 1, "router_chain", "unknown:mcp", "tu_mcp_other_session"),  # MCP, other session id
        (6, ts(NOW - 45), "ollama", "ollama/q", 1, "router_chain", "unknown:mcp", "tu_not_mine"),           # not this session
        (7, ts(NOW - 44), "gemini", "gemini-2.5", 1, "router_chain", SID, None),               # another non-Claude provider
        (8, ts(NOW - 43), "anthropic", "claude-sonnet-5-5", 1, "router_chain", SID, None),     # a Claude answer: Claude side
        (9, ts(NOW - 100), "codex", "gpt-6-astra", 1, "direct", SID, None),                    # the delegation's twin row
    ])
    _ns(home, {"ts": NOW - 99, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 300.0, "thread_id": T1},
        {"ts": NOW - 30, "outcome": "codex_failed", "model": "gpt-6-astra"},                    # failed, no thread: zero
        {"ts": NOW - 20, "outcome": "codex_blocked", "model": "gpt-6-astra", "duration_sec": 5.0, "thread_id": T3},  # no rollout: 1
        {"ts": NOW - 10, "outcome": "benched", "model": ""})                                    # not a run
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["complete"] is True
    assert r["codex"] == {"runs": 3, "responses": 5, "flagged_runs": 1}      # 4 + 0 + 1
    assert r["by_source"] == {"codex_runs": 5, "router_local": 2, "router_codex": 0, "router_other": 1,
                              "proxy": 0, "transcript_other": 0}
    assert r["claude"] == 6 + 1 + 1                                          # layout + m9 + the Claude router answer
    assert r["non_claude"] == 5 + 2 + 1 == 8
    assert r["total"] == 16
    assert r["local"] == {"attempts": 3, "ok": 2}                            # 1 ok, 1 failed, 1 MCP ok; cancelled/cache excluded
    # 16 calls is under the 50-call floor: n/a with its k/n, exactly as the 7-day row does
    assert tick.render_session_routing(r, now=NOW) == "routed n/a (8/16 calls, session) · local OK 67% (2/3, session)"
    r["total"] = 200                                                         # past the floor: the flagged run is named
    assert tick.render_session_routing(r, now=NOW) == \
        "routed 4.0% (8/200 calls, session, 1 Codex run counted as 1) · local OK 67% (2/3, session)"


def test_the_figure_is_shown_as_a_share_from_fifty_calls(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(96)])
    _routing_db(home, [(i, "2026-11-05 08:00:00", "ollama", "ollama/q", 1, "router_chain", SID, None) for i in range(1, 5)])
    r = sr.compute(SID, NOW, budget_s=30)
    assert (r["non_claude"], r["total"]) == (4, 100)
    assert tick.render_session_routing(r, now=NOW) == "routed 4.0% (4/100 calls, session) · local OK 100% (4/4, session)"


def test_no_local_attempt_is_na_never_zero_percent(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(60)])
    _routing_db(home, [])
    r = sr.compute(SID, NOW, budget_s=30)
    assert r["local"] == {"attempts": 0, "ok": 0} and r["non_claude"] == 0
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
    _routing_db(home, [])
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
    _routing_db(home, [(1, "2026-11-05 08:00:00", "ollama", "ollama/q", 1, "router_chain", SID, None)])
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
    _routing_db(home, [(1, "2026-11-05 08:00:00", "ollama", "ollama/q", 1, "router_chain", SID, None)])
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
    ts = _utc
    _routing_db(home, [
        (1, ts(NOW - 3600), "ollama", "ollama/q", 1, "router_chain", "organic-s", None),
        (2, ts(NOW - 3500), "ollama", "ollama/q", 0, "router_chain", "organic-s", None),
        (3, ts(NOW - 3400), "ollama", "ollama/q", 1, "router_chain", "research-s", None),     # not the population
        (4, ts(NOW - 3300), "ollama", "ollama/q", 1, "router_chain", None, None),             # no session id: unplaceable
        (5, ts(NOW - 86400 * 30), "ollama", "ollama/q", 1, "router_chain", "organic-s", None),  # outside the window
    ])
    _ns(home, {"ts": NOW - 300, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 100.0, "thread_id": T1,
               "session_id": "organic-s", "session_kind": "organic"},
        {"ts": NOW - 200, "outcome": "codex_failed", "model": "gpt-6-astra", "session_id": "organic-s",
         "session_kind": "organic"},
        {"ts": NOW - 100, "outcome": "delegated", "model": "gpt-6-astra", "duration_sec": 5.0, "thread_id": T3,
         "session_id": "research-s", "session_kind": "research"})
    def organic(sid, stamp):
        return sid == "organic-s"

    x = sr.window_extras(NOW - 7 * 86400, NOW, NOW, organic)
    # delegated: 4 responses, +3 beyond the unit the list counts; failed with no response: -1; the research run is out
    assert x["codex_extra"] == 3 - 1 and x["runs"] == 2 and x["flagged_runs"] == 0
    assert x["by_session"] == {"organic-s": 2}
    assert x["local"] == {"attempts": 2, "ok": 1} and x["unattributed_local"] == 1


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
    assert cm["local_ok"] == {"k": 3, "n": 5, "unattributed_attempts": 2}
    assert cm["codex"] == {"runs": 1, "extra_responses": 4, "flagged_runs": 0}


def test_the_refresher_caches_local_ok_and_the_flagged_runs():
    from llm_router import statusline_refresh as rf

    card = {"window_days": 7, "call_mix": {
        "population": "organic", "outside_claude": {"k": 5, "n": 100}, "below_opus": {"k": 0, "n": 95},
        "local_ok": {"k": 3, "n": 4, "unattributed_attempts": 7}, "codex": {"flagged_runs": 2}}}
    cm = rf._call_mix(card)
    assert cm["local_ok"] == {"k": 3, "n": 4} and cm["flagged_codex_runs"] == 2
    card["call_mix"]["local_ok"] = {"k": 5, "n": 4}                          # impossible: not cached, the row omits it
    assert "local_ok" not in rf._call_mix(card)
    del card["call_mix"]["local_ok"]                                         # an older scorecard: as before
    assert "local_ok" not in rf._call_mix(card)


def test_the_files_the_refresher_writes_are_private(env):
    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", _assistant("a"))
    _routing_db(home, [])
    sr.compute(SID, NOW, budget_s=30)
    assert stat.S_IMODE(os.stat(paths.state_path(sr.INDEX_DIR, f"{SID}.json")).st_mode) == 0o600


def test_the_tick_script_prints_the_session_row_only_once_the_session_has_a_snapshot(env, tmp_path):
    import subprocess
    import sys

    home, projects, _codex = env
    _write(projects / "-p" / f"{SID}.jsonl", *[_assistant(f"m{i}") for i in range(80)])
    _routing_db(home, [(1, "2026-11-05 08:00:00", "ollama", "ollama/q", 1, "router_chain", SID, None)])
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
