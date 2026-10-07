"""Local answers: the session id is stamped, and "accepted" is measurable.

Two gaps closed (live usage.db 2026-10-06: 0 of 544 runtime routing_decisions rows had a
session id, so O3 printed "local share unknown" and no local answer could be judged):

* the MCP path (``llm()`` / ``llm_edit`` through ``router.py``'s finalizer) and the hook's
  DIRECT path now stamp ``session_id``; the MCP path also stamps the calling ``tool_use``
  id, taken from Claude Code's ``_meta["claudecode/toolUseId"]`` (``call_identity``);
* O3 reports "local answers: n served, n accepted, accept rate" (``offload_share.
  local_answers``): accepted = not redone (O3's redo definition) and either 2 human turns
  of the same session followed or the last receipt-band press was ``kept``.

Definition: docs/repo_goals/KPIS.md (O3, "Local answers accepted").
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import call_identity, offload_share as osh, paths, session_kind, user_signal
from llm_router.commands import kpi

from tests import _o3_fixture as fx
from tests._o3_fixture import NOW, proxy_row

ORG = frozenset({"organic"})
GOLDEN = Path(__file__).parent / "golden"
PROMPT = "Explain the difference between a mutex and a semaphore in two sentences."
ANSWER = "A mutex admits one holder at a time; a semaphore admits up to N holders."


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    from llm_router import failopen

    failopen.reset_unpersisted()
    failopen.reset_cache()
    session_kind._FOUND.clear()
    for sid in ("s-org", "sess-env", "sess-new"):
        session_kind.tag_session(sid, "/Users/someone/Projects/app", env={})
    yield
    session_kind._FOUND.clear()
    failopen.reset_unpersisted()
    failopen.reset_cache()


# ── A. the MCP path stamps the session id and the tool_use id ────────────────

def _probe_server(tool):
    server = call_identity.IdentityMCPServer("probe")
    server.tool()(tool)
    return server


def _text(result) -> str:
    return result.content[0].text


@pytest.mark.asyncio
async def test_tool_use_id_from_the_wire_meta_is_bound_for_the_call_only():
    from mcp.client import Client

    async def which() -> str:
        return str(call_identity.tool_use_id())

    async with Client(_probe_server(which)) as client:
        sent = await client.call_tool("which", {}, meta={"claudecode/toolUseId": "toolu_01ABCdef"})
        bare = await client.call_tool("which", {})
        text = await client.call_tool("which", {}, meta={"claudecode/toolUseId": "redo this; rm -rf /"})
    assert (_text(sent), _text(bare), _text(text)) == ("toolu_01ABCdef", "None", "None")
    assert call_identity.tool_use_id() is None  # reset after the call


def test_the_router_mcp_server_is_the_identity_server():
    from llm_router import server

    assert isinstance(server.mcp, call_identity.IdentityMCPServer)


def _rows(db_path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM routing_decisions").fetchall()
    finally:
        conn.close()


async def _fake_call_text(model, *args, **kwargs):
    from llm_router.types import LLMResponse

    return LLMResponse(content=ANSWER, model=model, input_tokens=40, output_tokens=12,
                       cost_usd=0.0, latency_ms=300.0, provider="ollama")


async def _route_through_mcp(meta: dict | None) -> str:
    """One `tools/call` over the MCP protocol into a tool that routes exactly like
    `llm()` does (route_and_call, classification_data omitted)."""
    from mcp.client import Client

    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    async def ask(q: str) -> str:
        return (await route_and_call(TaskType.QUERY, q)).content

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._call_text", _fake_call_text):
        async with Client(_probe_server(ask)) as client:
            return _text(await client.call_tool("ask", {"q": PROMPT}, meta=meta))


@pytest.fixture
def _router_db(mock_env, temp_db, monkeypatch):
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    return temp_db


@pytest.mark.asyncio
async def test_mcp_path_stamps_session_id_and_tool_use_id(_router_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-env")
    assert await _route_through_mcp({"claudecode/toolUseId": "toolu_mcp1"}) == ANSWER
    rows = _rows(_router_db)
    assert len(rows) == 1, rows
    assert rows[0]["final_provider"] == "ollama"
    assert (rows[0]["session_id"], rows[0]["tool_use_id"]) == ("sess-env", "toolu_mcp1")


@pytest.mark.asyncio
async def test_no_session_env_is_null_never_the_machine_wide_pointer(_router_db, monkeypatch):
    """The hook-written pointer is last-writer-wins across sessions: stamping from it would
    attribute this answer to whichever session prompted last. NULL (unknown) instead."""
    from llm_router import session_store

    session_store.write_pointer("sess-someone-else")
    assert session_store.resolve_session_id() == "sess-someone-else"  # premise: a fresh pointer
    await _route_through_mcp(None)
    rows = _rows(_router_db)
    assert len(rows) == 1 and rows[0]["session_id"] is None and rows[0]["tool_use_id"] is None


def test_direct_path_stamps_the_hooks_session_id(temp_db):
    from llm_router.hooks.direct_executor import DirectResult, ModelSpec
    from llm_router.hooks.savings_logger import log_direct_to_db

    result = DirectResult(text=ANSWER, model=ModelSpec("ollama", "qwen3.5:latest"), latency_ms=900,
                          input_tokens=30, output_tokens=12)
    log_direct_to_db(result, prompt=PROMPT, task_type="query", complexity="simple",
                     classifier_type="heuristic", session_id="sess-hook")
    log_direct_to_db(result, prompt=PROMPT, task_type="query", complexity="simple",
                     classifier_type="heuristic")
    assert [r["session_id"] for r in _rows(temp_db)] == ["sess-hook", None]


def test_ids_that_are_not_ids_are_refused(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "a session id with spaces")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "")
    assert call_identity.mcp_session_id() is None
    monkeypatch.setenv("CLAUDE_SESSION_ID", "fallback-id-1")
    assert call_identity.mcp_session_id() == "fallback-id-1"
    tok = call_identity.bind("x" * 129)
    try:
        assert call_identity.tool_use_id() is None
    finally:
        call_identity.reset(tok)


# ── C. privacy: ids, codes and numbers only ──────────────────────────────────

@pytest.mark.asyncio
async def test_no_prompt_or_answer_text_reaches_the_ledger_row(_router_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-env")
    await _route_through_mcp({"claudecode/toolUseId": "toolu_priv1"})
    row = _rows(_router_db)[0]
    words = {w for w in (PROMPT + " " + ANSWER).lower().replace(";", " ").replace(".", " ").split()
             if len(w) > 4}
    assert words  # premise: there is text that could leak
    for col in row.keys():
        val = row[col]
        if isinstance(val, str):
            assert not any(w in val.lower() for w in words), (col, val)
    # the only columns this change writes are ids
    assert row["session_id"] == "sess-env" and row["tool_use_id"] == "toolu_priv1"


# ── B. the accepted definition, on fixtures ──────────────────────────────────

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _local(ts, sid="s-org", tool=None, success=1):
    return {"session_id": sid, "ts": _iso(ts), "kind": "local_shadow", "tool_use_id": tool,
            "success": success}


# The reason usage_outcome gives each outcome when none is named (judge_transcript always sets one).
_REASON = {"used": "not_redone", "unknown": "window_open", "redone": "re_asked"}


def _outcome(tool, *, outcome="used", turns_after=3, sid="s-org", ts=None, reason=None, so_far=None):
    reason = reason or _REASON[outcome]
    return {"event_id": tool, "session_id": sid, "ts": ts, "outcome": outcome, "reason": reason,
            "so_far": so_far or reason, "turns_after": turns_after, "tool": "llm", "kind": "answer"}


def _build(rows=(), local=(), outcomes=(), band=frozenset(), kept=frozenset()):
    built = osh.build_units(list(rows), list(local), now=NOW, days=7, allowed=ORG,
                            kind_of=lambda sid, stamp: stamp if stamp else "organic",
                            band_redone=band, outcome_redos=outcomes, band_kept=kept)
    # What the local answers line reads: local turns plus the local MCP answers (M0.3a keeps
    # the latter out of O3's `units`, in `local_assist`).
    built["la"] = built["units"] + built["local_assist"]
    return built


def _answers(**kw) -> dict:
    return osh.local_answers(_build(**kw)["la"])


def _turns(sid, t0, n, *, escalate_at=None):
    """``n`` later human turns of ``sid`` (one proxy row each); turn ``escalate_at`` is a
    `claude:` escalation."""
    return [proxy_row(100 + k, sid=sid, tier="opus" if k == escalate_at else "sonnet",
                      reason="escalation" if k == escalate_at else "policy",
                      ts=t0 + 100 * k, kind="organic") for k in range(1, n + 1)]


def test_transcript_turns_after_decide_a_joined_local_answer():
    t = NOW - 5000
    cases = {"toolu_a": _outcome("toolu_a", turns_after=3),
             "toolu_b": _outcome("toolu_b", outcome="unknown", turns_after=2),   # 2 turns: closed
             "toolu_c": _outcome("toolu_c", outcome="unknown", turns_after=1),   # still open
             "toolu_d": _outcome("toolu_d", outcome="redone", turns_after=1)}    # redo wins
    local = [_local(t + i, tool=k) for i, k in enumerate(cases)]
    units = _build(local=local, outcomes=list(cases.values()))["la"]
    assert [(u["redone"], u["window_open"]) for u in units] == [
        (False, False), (False, False), (False, True), (True, False)]
    assert osh.local_answers(units) == {"served": 4, "accepted": 2, "redone": 1, "unjudged": 0,
                                        "pending": 1, "kept": 0, "joined": 4}


def test_proxy_turns_decide_a_local_answer_in_the_same_session():
    t = NOW - 5000
    base = [proxy_row(0, sid="s-org", tier="sonnet", ts=t - 10, kind="organic")]
    closed = _answers(rows=base + _turns("s-org", t, 3), local=[_local(t)])
    redo_turn2 = _answers(rows=base + _turns("s-org", t, 3, escalate_at=2), local=[_local(t)])
    redo_turn3 = _answers(rows=base + _turns("s-org", t, 3, escalate_at=3), local=[_local(t)])
    open_ = _answers(rows=base + _turns("s-org", t, 1), local=[_local(t)])
    other = _answers(rows=base + _turns("s-other", t, 3, escalate_at=1), local=[_local(t)])
    assert (closed["accepted"], redo_turn2["redone"], redo_turn3["accepted"]) == (1, 1, 1)
    assert (open_["pending"], open_["accepted"]) == (1, 0)
    assert other["pending"] == 1 and other["redone"] == 0  # another session's redo is not this one's


def test_a_keep_press_accepts_a_served_answer_but_loses_to_a_redo():
    t = NOW - 5000
    served = proxy_row(0, sid="s-org", decision="served", tier=None, model="local", ts=t, kind="organic")
    open_kept = _answers(rows=[served], kept={served["msg_id"]})
    open_bare = _answers(rows=[served])
    redone_kept = _answers(rows=[served] + _turns("s-org", t, 2, escalate_at=1), kept={served["msg_id"]})
    band_redo = _answers(rows=[served], band={served["msg_id"]})
    assert (open_kept["accepted"], open_kept["kept"]) == (1, 1)
    assert (open_bare["pending"], open_bare["accepted"]) == (1, 0)
    assert (redone_kept["redone"], redone_kept["accepted"]) == (1, 0)
    assert band_redo["redone"] == 1


def test_the_transcript_session_replaces_a_stale_stamp():
    """After /clear the MCP server's environment can still name the old session: the
    transcript the call sits in is the ground truth."""
    t = NOW - 5000
    units = _build(local=[_local(t, sid="sess-old", tool="toolu_x")],
                   outcomes=[_outcome("toolu_x", sid="sess-new")])["la"]
    assert [u["session_id"] for u in units] == ["sess-new"]


def test_an_exactly_joined_verdict_is_not_reused_by_the_time_join():
    t = NOW - 5000
    local = [_local(t, tool="toolu_r"), _local(t + 5)]  # second: no tool_use_id, 5 s later
    units = _build(local=local, outcomes=[_outcome("toolu_r", outcome="redone", ts=t + 1)])["la"]
    assert [u["why"] for u in units] == ["usage_outcome", None]


# ── legacy NULL rows stay unknown ─────────────────────────────────────────────

def _legacy_db(path: Path, n: int) -> None:
    """The live schema before this change: no tool_use_id column, session_id NULL."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
                 "task_type TEXT, complexity TEXT, final_model TEXT, final_provider TEXT, latency_ms REAL, "
                 "provenance TEXT, session_id TEXT, shadow_tier TEXT)")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3600))
    conn.executemany("INSERT INTO routing_decisions (timestamp, task_type, complexity, final_model, "
                     "final_provider, latency_ms, provenance, session_id) VALUES (?,?,?,?,?,?,?,NULL)",
                     [(stamp, "query", "simple", "ollama/qwen3.5", "ollama", 900.0, "runtime")] * n)
    conn.commit()
    conn.close()


def test_legacy_rows_without_a_session_stay_unknown(monkeypatch, tmp_path):
    from llm_router import northstar as ns

    db = tmp_path / "legacy.db"
    _legacy_db(db, 7)
    legacy = list(ns.local_shadow_units(days=7, db_path=db))
    assert len(legacy) == 7 and all(u["session_id"] is None and u["tool_use_id"] is None for u in legacy)
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), fx.baseline_rows())
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(legacy))
    o3 = kpi.compute_scorecard(days=7)["o3"]
    la = o3["local_answers"]
    assert la["breakdown"]["no_session"] == 7 and la["breakdown"]["served"] == 0
    assert not la["measurable"] and la["value"].endswith("accept rate not measurable: "
                                                          "no decided local answer in window")
    assert "%" not in la["value"] and "7 local answer(s) with no session id: unknown" in la["lines"][0]
    lines = kpi._o3_render_lines(o3)
    at = next(i for i, ln in enumerate(lines) if "local answers (accepted; never in NS)" in ln)
    assert all("%" not in ln for ln in lines[at:at + 2])  # the line and its detail


# ── the number, through the scorecard ────────────────────────────────────────

def _served_population(n_served, n_redone, *, t0=NOW - 86400):
    """Each conversation: one locally served turn, then 3 Sonnet human turns (a closed
    window); the first ``n_redone`` have a `claude:` escalation as their next turn."""
    rows = []
    for j in range(n_served):
        sid, t = f"l{j}", t0 + j * 1000
        rows.append(proxy_row(0, sid=sid, decision="served", tier=None, model="local", ts=t, kind="organic"))
        rows += [proxy_row(k, sid=sid, tier="opus" if (j < n_redone and k == 1) else "sonnet",
                           reason="escalation" if (j < n_redone and k == 1) else "policy",
                           ts=t + 100 * k, kind="organic") for k in (1, 2, 3)]
    return rows


def test_accept_rate_on_a_fixture_with_a_known_answer():
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), _served_population(60, 6))
    la = kpi.compute_scorecard(days=7, now=NOW)["o3"]["local_answers"]
    assert la["measurable"] and la["breakdown"]["decided"] == 60
    assert la["value"] == "60 served, 54 accepted, accept rate 90.0% (n=60)"


def test_below_fifty_decided_is_too_few_to_tell_never_a_percentage():
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), _served_population(49, 49))
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    la = o3["local_answers"]
    assert la["breakdown"]["redone"] == 49 and not la["measurable"]  # premise: 49 decided, all redone
    assert la["value"] == "49 served, 0 accepted, accept rate too few to tell (n=49)"
    assert all("%" not in ln for ln in kpi._o3_render_lines(o3) if "local answers" in ln)


def test_the_join_end_to_end_mcp_row_to_transcript_verdict(_router_db, monkeypatch, tmp_path):
    """Write through the MCP path, read back through local_shadow_units, judge against a
    transcript that holds the same tool_use id with 2 human turns after it: accepted."""
    from llm_router import cost
    from llm_router import northstar as ns

    monkeypatch.setattr(cost, "_write_provenance", lambda: "runtime")  # as in production
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-env")
    import asyncio

    asyncio.run(_route_through_mcp({"claudecode/toolUseId": "toolu_e2e"}))
    proj = Path(tmp_path / "claude-projects" / "-Users-someone-Projects-app")
    proj.mkdir(parents=True)
    tool = "mcp__llm_router__llm"
    recs = [{"type": "user", "message": {"role": "user", "content": "explain locks"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_e2e",
                                                           "name": tool, "input": {"prompt": "q"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_e2e",
                                                      "content": "a"}]}},
            {"type": "user", "message": {"role": "user", "content": "thanks, next topic"}},
            {"type": "user", "message": {"role": "user", "content": "and another"}}]
    for i, r in enumerate(recs):
        r["timestamp"] = f"2026-10-06T10:00:{i:02d}Z"
    (proj / "sess-env.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    orig = ns.local_shadow_units
    monkeypatch.setattr(ns, "local_shadow_units",
                        lambda days=30, db_path=None: orig(days=days, db_path=_router_db))
    la = kpi.compute_scorecard(days=7)["o3"]["local_answers"]
    b = la["breakdown"]
    assert (b["served"], b["accepted"], b["joined"], b["no_session"]) == (1, 1, 1, 0)
    assert la["value"] == "1 served, 1 accepted, accept rate too few to tell (n=1)"


# ── NS, D1-D5, O1, O2 (and the rest) stay byte-identical ─────────────────────

def test_other_kpis_are_byte_identical_with_attributable_accepted_local_answers(monkeypatch):
    from llm_router import northstar as ns

    rows = fx.baseline_rows()
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)
    # Local answers in the baseline's own session, early enough that later proxy turns close
    # their windows: attributable AND accepted, the data this change creates.
    early = min(r["ts"] for r in rows) + 0.5
    local = [_local(early, tool=f"toolu_{i}") for i in range(3)]
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(local))
    card = kpi.compute_scorecard(days=7, now=NOW)
    assert card["o3"]["local_answers"]["breakdown"]["accepted"] == 3  # premise
    assert json.dumps(card["kpis"], indent=1, sort_keys=True, default=str) == \
        (GOLDEN / "kpi_pre_o3_kpis.json").read_text()
    o3_lines = set(kpi._o3_render_lines(card["o3"]))
    full = kpi.render_scorecard(card).split("\n")
    # `local (shadow): n=...` is #284's informational count of local units (not this change).
    rest = [ln for ln in full if ln not in o3_lines and not ln.startswith("local (shadow): ")]
    assert "\n".join(rest) == (GOLDEN / "kpi_pre_o3_scorecard.txt").read_text()


def test_mcp_local_answers_are_on_the_answers_line_and_never_o3_turns(monkeypatch):
    """The two O3 changes meet here (M0.3 #295 + M0.4 #288). #295: a local MCP answer is inside a
    Claude turn, so it is not an O3 turn (local share and n do not move). #288: it is still a
    local answer, so the `local answers` line counts it. Dropping either side of the merge turns
    this red: MCP units counted as turns again moves n and local_n; the line losing them reads
    0 served."""
    from llm_router import northstar as ns

    rows = fx.baseline_rows()
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)
    before = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    early = min(r["ts"] for r in rows) + 0.5
    local = [_local(early, tool=f"toolu_{i}") for i in range(3)]
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(local))
    after = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    assert after["breakdown"]["n"] == before["breakdown"]["n"]
    assert after["breakdown"]["local_n"] == before["breakdown"]["local_n"]
    assert after["breakdown"]["local_assist_n"] == 3
    assert after["local_answers"]["breakdown"]["served"] == before["local_answers"]["breakdown"]["served"] + 3


def test_a_keep_press_never_reaches_ns_d1_d2_d3(monkeypatch):
    rows = _served_population(3, 0)
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)
    before = kpi.compute_scorecard(days=7, now=NOW)
    user_signal.record(rows[0]["msg_id"], "kept", "terminal", now=NOW - 10)
    after = kpi.compute_scorecard(days=7, now=NOW)
    assert after["o3"]["local_answers"]["breakdown"]["kept"] == 1  # premise: the press was read
    for key in ("NS", "D1", "D2"):
        assert after["kpis"][key] == before["kpis"][key], key
    assert after["kpis"]["D3"]["value"] == before["kpis"]["D3"]["value"]


# ── review follow-ups on #288 (each test was red on 3ccf972) ─────────────────

_UNJUDGED = ("no_result", "no_pairs", "not_applied_seen", "partly_applied")
_EDIT = "mcp__llm_router__llm_edit"


def _transcript(*recs) -> list[dict]:
    for i, r in enumerate(recs):
        r["timestamp"] = f"2026-10-06T10:00:{i:02d}Z"
    return list(recs)


def _human(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _call(tid, name, inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name,
                                                           "input": inp}]}}


def _result(tid, text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                     "content": text}]}}


def test_an_unknown_verdict_is_never_rounded_to_accepted():
    """usage_outcome never rounds an unknown verdict to either side. Three turns after it, or
    proxy turns that close O3's window, must not make it accepted: it is unjudged."""
    t = NOW - 5000
    base = [proxy_row(0, sid="s-org", tier="sonnet", ts=t - 10, kind="organic")]
    outcomes = [_outcome(f"toolu_{r}", outcome="unknown", reason=r, turns_after=3) for r in _UNJUDGED]
    local = [_local(t + i, tool=o["event_id"]) for i, o in enumerate(outcomes)]
    la = _answers(rows=base + _turns("s-org", t, 3), local=local, outcomes=outcomes)
    assert la["accepted"] == 0, la
    assert (la["served"], la["unjudged"], la["pending"], la["redone"]) == (4, 4, 0, 0)
    res = kpi._local_answers_result(la, 0)
    assert res["value"] == ("4 served, 0 accepted, accept rate not measurable: "
                            "no decided local answer in window")
    assert "4 with a usage verdict of unknown" in res["lines"][0]


def test_a_failed_local_edit_redone_by_hand_is_not_accepted():
    """The reviewer's case through the real judge: the local model failed every attempt
    ('No edits to apply.'), Claude then edited the file itself, 3 human turns followed."""
    from llm_router import usage_outcome as uo

    recs = _transcript(
        _human("fix a.py"),
        _call("toolu_FAIL", _EDIT, {"task": "fix", "files": ["src/a.py"]}),
        _result("toolu_FAIL", "applied=False, attempts=3\nNo edits to apply."),
        _call("x1", "Edit", {"file_path": "/repo/src/a.py", "old_string": "return 1",
                             "new_string": "return 2"}),
        _result("x1", "ok"),
        _human("next"), _human("and next"), _human("and the last"))
    verdicts = uo.judge_transcript(recs, session_id="s-org")
    assert [(v["outcome"], v["reason"], v["turns_after"]) for v in verdicts] == [("unknown", "no_pairs", 3)]
    la = _answers(local=[_local(NOW - 5000, tool="toolu_FAIL")], outcomes=verdicts)
    assert la["accepted"] == 0, la
    assert la["unjudged"] == 1


def test_an_edit_not_applied_so_far_is_pending_at_two_turns_not_accepted():
    """usage_outcome reports an unapplied edit as window_open until its 3-turn window closes.
    Two turns after it O3's own window closes, but nothing shows the edit was used: pending.
    ``so_far`` carries what the evidence says now."""
    from llm_router import usage_outcome as uo

    pair = json.dumps([{"file": "src/a.py", "old_string": "def f():\n    return 1",
                        "new_string": "def f():\n    return 2", "description": ""}])
    recs = _transcript(
        _human("fix a.py"),
        _call("toolu_NA", _EDIT, {"task": "fix", "files": ["src/a.py"]}),
        _result("toolu_NA", "**edits**\n---\n```json\n" + pair + "\n```"),
        _human("next"), _human("and next"))
    verdicts = uo.judge_transcript(recs, session_id="s-org")
    assert [(v["outcome"], v["reason"], v.get("so_far"), v["turns_after"]) for v in verdicts] == [
        ("unknown", "window_open", "not_applied_seen", 2)]
    used = _outcome("toolu_U", outcome="unknown", reason="window_open", so_far="not_redone", turns_after=2)
    la = _answers(local=[_local(NOW - 5000, tool="toolu_NA"), _local(NOW - 4999, tool="toolu_U")],
                  outcomes=verdicts + [used])
    assert (la["accepted"], la["pending"]) == (1, 1), la


def test_one_mcp_call_is_one_local_answer_however_many_rows_it_wrote():
    """llm_edit retries route_and_call up to 3 times inside one tools/call, and every attempt
    writes a row with the same tool_use id. One call, one answer; rows with no id stay per row."""
    t = NOW - 5000
    local = [_local(t + i, tool="toolu_EDIT1") for i in range(3)] + [_local(t + 10), _local(t + 11)]
    built = _build(local=local, outcomes=[_outcome("toolu_EDIT1", turns_after=3)])
    la = osh.local_answers(built["la"])
    assert (la["served"], la["accepted"], la["pending"]) == (3, 1, 2), la
    assert osh.summarize(osh.turn_units(built["units"]))["local"]["n"] == 0  # M0.3a: an MCP local answer is not an O3 turn
    assert len(built["local_assist"]) == 3


@pytest.mark.asyncio
async def test_three_router_calls_in_one_tools_call_read_back_as_one_answer(_router_db, monkeypatch):
    """The production writer: three route_and_call inside one MCP call (llm_edit's retry loop)
    write three rows with one tool_use id; local_shadow_units + build_units count one answer."""
    from mcp.client import Client

    from llm_router import cost
    from llm_router import northstar as ns
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    monkeypatch.setattr(cost, "_write_provenance", lambda: "runtime")  # as in production
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-org")

    async def retry(q: str) -> str:
        for _ in range(3):
            out = (await route_and_call(TaskType.QUERY, q)).content
        return out

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._call_text", _fake_call_text):
        async with Client(_probe_server(retry)) as client:
            await client.call_tool("retry", {"q": PROMPT}, meta={"claudecode/toolUseId": "toolu_R3"})
    local = list(ns.local_shadow_units(days=1, db_path=_router_db))
    assert [u["tool_use_id"] for u in local] == ["toolu_R3"] * 3  # premise: 3 rows, one call
    built = osh.build_units([], local, now=time.time() + 60, days=1, allowed=ORG,
                            kind_of=lambda sid, stamp: "organic")
    assert osh.local_answers(built["units"] + built["local_assist"])["served"] == 1


def _rows_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
                 "task_type TEXT, complexity TEXT, final_model TEXT, final_provider TEXT, latency_ms REAL, "
                 "provenance TEXT, session_id TEXT, shadow_tier TEXT, success INTEGER, tool_use_id TEXT)")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3600))
    conn.executemany("INSERT INTO routing_decisions (timestamp, task_type, complexity, final_model, "
                     "final_provider, latency_ms, provenance, session_id, success, tool_use_id) "
                     "VALUES (?,'query','simple','qwen','ollama',900.0,'runtime','s-org',?,?)",
                     [(stamp, ok, tool) for ok, tool in rows])
    conn.commit()
    conn.close()


def test_a_row_the_router_flagged_failed_is_not_an_accepted_local_answer(tmp_path):
    """success=0 is the router's own verdict (quality_degraded, a failure finish reason,
    unusable output). Such an answer is listed as failed, never accepted. A call whose
    failed attempt was retried into a good one is one good answer."""
    from llm_router import northstar as ns

    db = tmp_path / "u.db"
    _rows_db(db, [(0, None), (1, None), (0, "toolu_retry"), (1, "toolu_retry"), (0, "toolu_dead")])
    local = list(ns.local_shadow_units(days=1, db_path=db))
    assert [u["success"] for u in local] == [0, 1, 0, 1, 0]
    t0 = min(osh._iso_ts(u["ts"]) for u in local)
    rows = [proxy_row(0, sid="s-org", tier="sonnet", ts=t0 - 10, kind="organic")] + \
        _turns("s-org", t0 + 1, 3)
    built = osh.build_units(rows, local, now=t0 + 3600, days=1, allowed=ORG,
                            kind_of=lambda sid, stamp: stamp if stamp else "organic")
    la = osh.local_answers(built["units"] + built["local_assist"])
    assert (la["served"], la["accepted"]) == (2, 2), la
    assert built["local_failed"] == 2
    res = kpi._local_answers_result(la, 0, local_failed=built["local_failed"])
    assert "2 flagged failed by the router" in res["lines"][0]
    from llm_router import northstar as ns_mod

    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), fx.baseline_rows())
    with patch.object(ns_mod, "local_shadow_units", lambda days=30, db_path=None: iter(local)):
        o3 = kpi.compute_scorecard(days=7)["o3"]
    assert o3["excluded"]["local_failed"] == 2  # O3's headline drops them too, and says so
    assert any("2 local call(s) the router flagged failed" in ln for ln in o3["lines"])


def test_a_forked_transcript_copy_never_decides_the_verdict_by_file_order():
    """claude --fork-session copies the history, tool_use ids included, so one id can sit in
    two transcripts. The copy in the stamped session wins; with no stamped copy a redone
    verdict in any copy wins; the result never depends on which file sorts last."""
    t = NOW - 5000
    a = _outcome("toolu_F", outcome="redone", sid="sess-A", turns_after=1)
    b = _outcome("toolu_F", outcome="used", sid="sess-B", turns_after=3)
    got = {}
    for name, order in (("ab", [a, b]), ("ba", [b, a])):
        stamped = _build(local=[_local(t, sid="sess-A", tool="toolu_F")], outcomes=order)["la"]
        stale = _build(local=[_local(t, sid="sess-old", tool="toolu_F")], outcomes=order)["la"]
        got[name] = [(u["session_id"], u["why"]) for u in stamped + stale]
    assert got["ab"] == got["ba"] == [("sess-A", "usage_outcome"), ("sess-A", "usage_outcome")], got
    # stamped in B: B's copy (used, 3 turns after) wins even though A's copy is redone
    units = _build(local=[_local(t, sid="sess-B", tool="toolu_F")], outcomes=[b, a])["la"]
    assert [(u["session_id"], u["redone"]) for u in units] == [("sess-B", False)]


def _pending_population(n, *, t0):
    """``n`` locally served turns, each followed by 1 human turn only: window open."""
    rows = []
    for j in range(n):
        sid, t = f"p{j}", t0 + j * 1000
        rows.append(proxy_row(0, sid=sid, decision="served", tier=None, model="local", ts=t, kind="organic"))
        rows.append(proxy_row(1, sid=sid, tier="sonnet", ts=t + 100, kind="organic"))
    return rows


def test_pending_answers_stay_outside_the_accept_rate_on_the_scorecard():
    """The rule 'pending is never counted on either side', at the scorecard: 50 accepted,
    5 redone, 10 pending is 50/55, never 50/65."""
    rows = _served_population(55, 5) + _pending_population(10, t0=NOW - 86400 + 60 * 1000)
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), rows)
    la = kpi.compute_scorecard(days=7, now=NOW)["o3"]["local_answers"]
    b = la["breakdown"]
    assert (b["served"], b["accepted"], b["redone"], b["pending"], b["decided"]) == (65, 50, 5, 10, 55)
    assert la["value"] == "65 served, 50 accepted, accept rate 90.9% (n=55)"


def test_direct_path_stores_only_an_id_never_text_or_a_placeholder(temp_db):
    """log_direct_to_db takes session ids from several writers: the SDK passes 'sdk',
    agent-route used to fall back to 'unknown'. Only a well-formed id is stored; a value
    that is not a string must not cost the routing row."""
    from llm_router.hooks.direct_executor import DirectResult, ModelSpec
    from llm_router.hooks.savings_logger import log_direct_to_db

    result = DirectResult(text=ANSWER, model=ModelSpec("ollama", "qwen3.5:latest"), latency_ms=900,
                          input_tokens=30, output_tokens=12)
    real = "2f1c6a0e-8d4b-4c47-9b1e-0a6f3d2c9e51"
    sent = ["please fix the login bug in auth.py QZTEXT", "sdk", "unknown", ["not", "a", "str"], real]
    for sid in sent:
        log_direct_to_db(result, prompt=PROMPT, task_type="query", complexity="simple",
                         classifier_type="heuristic", session_id=sid)
    assert [r["session_id"] for r in _rows(temp_db)] == [None, None, None, None, real]


def test_agent_route_stamps_the_payload_session_not_the_machine_wide_file(tmp_path, monkeypatch):
    """With no CLAUDE_CODE_SESSION_ID in the hook's env, agent-route's own session id falls
    back to session_id.txt (one file per machine, last SessionStart wins). The ledger takes the
    hook payload's session id instead."""
    import io
    import sys

    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de
    from llm_router.hooks import savings_logger

    from tests.test_agent_route_hook import _load_hook_module

    home = tmp_path / ".llm-router"
    home.mkdir()
    (home / "session_id.txt").write_text("sess-pointer")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    for k, v in (("LLM_ROUTER_SUBAGENT_DIRECT", "on"), ("LLM_ROUTER_AGENT_ROUTE_CODEX", "off"),
                 ("LLM_ROUTER_ALLOW_SUBAGENTS", "off"), ("LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "off")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cb, "build_chain", lambda *a, **k: ["fake-model"])
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: de.DirectResult(
        text="a routed answer", model=de.ModelSpec("ollama", "fake"), latency_ms=1,
        input_tokens=1, output_tokens=1))
    seen = []
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: seen.append(kw["session_id"]))
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    mod = _load_hook_module()
    monkeypatch.setattr(mod, "_is_headless_entrypoint", lambda e: False)
    monkeypatch.setattr(mod, "_govern_run", lambda *a, **k: None)
    assert mod._get_session_id() == "sess-pointer"  # premise: the agent's own id is the file's
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "sess-payload",
               "tool_input": {"prompt": "explain what a mutex is", "subagent_type": "general-purpose"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    try:
        mod.main()
    except SystemExit:
        pass
    assert seen == ["sess-payload"]


@pytest.mark.asyncio
async def test_env_session_id_is_stamped_only_inside_an_mcp_tool_call(_router_db, monkeypatch):
    """CLAUDE_CODE_SESSION_ID is also in every Bash tool shell, so a gateway or route_server
    started from one would stamp that session on every call it later serves. Outside an MCP
    tool call (no tool_use id bound) the row stores NULL."""
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-shell")
    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._call_text", _fake_call_text):
        await route_and_call(TaskType.QUERY, PROMPT)
    rows = _rows(_router_db)
    assert len(rows) == 1 and (rows[0]["session_id"], rows[0]["tool_use_id"]) == (None, None)


def test_the_id_validator_does_not_load_the_mcp_server_stack():
    """savings_logger runs inside the UserPromptSubmit hook; importing the validator must not
    pull in the MCP server (measured: +234 ms)."""
    import subprocess
    import sys

    code = ("import sys, llm_router.call_identity as c; "
            "print(c.ledger_session_id('sdk'), 'mcp.server.mcpserver' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.split() == ["None", "False"], out.stderr[-2000:]


# ── review follow-ups (third round: M0.4) ────────────────────────────────────

def test_a_joined_unit_is_decided_by_the_transcript_even_when_the_proxy_counts_more_turns():
    """The proxy counts every non-continuation request as a human turn, so a Task subagent's
    first call (same session id) adds proxy 'turns' the transcript does not have. For a unit
    joined exactly, the transcript's turns_after decides both ways: 0 human turns after the
    call stays pending however many proxy rows follow it."""
    t = NOW - 5000
    base = [proxy_row(0, sid="s-org", tier="sonnet", ts=t - 10, kind="organic")]
    subagents = _turns("s-org", t, 2)  # two proxy 'turns' after the call: the subagents' first calls
    units = _build(rows=base + subagents, local=[_local(t, tool="toolu_s")],
                   outcomes=[_outcome("toolu_s", outcome="unknown", turns_after=0)])["la"]
    joined = [u for u in units if u["class"] == osh.CLASS_LOCAL]
    assert (joined[0]["window_open"], joined[0]["redone"]) == (True, False)
    assert osh.local_answers(units)["pending"] == 1 and osh.local_answers(units)["accepted"] == 0
    # and the transcript closes a window the proxy never saw (existing rule, unchanged)
    closed = _build(local=[_local(t, tool="toolu_c")], outcomes=[_outcome("toolu_c", turns_after=2)])["la"]
    assert closed[0]["window_open"] is False


def test_local_units_dropped_as_untagged_or_other_kind_are_counted_and_stated(monkeypatch):
    """A local answer from a session whose kind does not resolve (or is research) used to
    vanish from the 'local answers' line: '0 served' with no reason."""
    from llm_router import northstar as ns

    session_kind.tag_session("s-res", "/Users/someone/Projects/app", env={"LLM_ROUTER_SESSION_KIND": "research"})
    t = NOW - 5000
    units = [_local(t + i, sid=sid, tool=f"toolu_{i}") for i, sid in
             enumerate(["s-none", "s-none", "s-res"])]
    built = osh.build_units([], units, now=NOW, days=7, allowed=ORG,
                            kind_of=lambda sid, stamp: {"s-none": None, "s-res": "research"}[sid])
    assert (built["local_untagged"], built["local_other_kind"]) == (2, 1)
    assert built["untagged"] == 2 and built["other_kind"] == 1  # still in O3's own exclusion note too
    # through the scorecard
    fx.write_jsonl(paths.state_path("proxy_calls.jsonl"), fx.baseline_rows())
    monkeypatch.setattr(ns, "local_shadow_units", lambda days=30, db_path=None: iter(units))
    o3 = kpi.compute_scorecard(days=7, now=NOW)["o3"]
    la = o3["local_answers"]
    assert la["breakdown"]["served"] == 0 and (la["breakdown"]["untagged"], la["breakdown"]["other_kind"]) == (2, 1)
    assert "2 local answer(s) from sessions with no kind tag" in la["lines"][0]
    assert "1 from a research/other-kind session" in la["lines"][0]


@pytest.mark.asyncio
async def test_the_tool_use_id_is_reset_after_an_in_process_call_tool():
    """No protocol layer here: an embedder or a test calls ``call_tool`` directly with a
    context that carries the meta, then routes in the same task. The id must not outlive
    the call (the reset in ``IdentityMCPServer.call_tool``)."""
    from types import SimpleNamespace

    async def which() -> str:
        return str(call_identity.tool_use_id())

    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"claudecode/toolUseId": "toolu_01ABCdef"}))
    server = _probe_server(which)
    inside = await server.call_tool("which", {}, ctx)
    assert "toolu_01ABCdef" in str(inside)
    assert call_identity.tool_use_id() is None


@pytest.mark.asyncio
async def test_alternating_calls_with_different_env_ids_each_write_their_own(_router_db, monkeypatch):
    """PLAN M0.4 test (b): the id is read per call, so two callers that alternate (two
    sessions' servers share nothing, but one process must not remember the last id)."""
    for i, sid in enumerate(["sess-a", "sess-b", "sess-a", "sess-b"]):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        await _route_through_mcp({"claudecode/toolUseId": f"toolu_alt{i}"})
    got = [(r["session_id"], r["tool_use_id"]) for r in _rows(_router_db)]
    assert sorted(got) == [("sess-a", "toolu_alt0"), ("sess-a", "toolu_alt2"),
                           ("sess-b", "toolu_alt1"), ("sess-b", "toolu_alt3")]
