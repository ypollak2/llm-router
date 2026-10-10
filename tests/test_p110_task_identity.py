"""P1.10 task identity (PLAN v16 R-EVL-2, MUST P1.10-a).

Before P1.10 no ledger writer carried a task id: ``usage`` and ``routing_decisions`` had no
``task_id`` column (P0.8 completeness always counted it missing), the SDK stamped the
placeholder ``sdk`` as its session, and ``edit_outcomes`` / proxy / execution rows had no
``task_id`` or ``trace_id``. These tests are fixtures and synthetic rows only (D-42:
mechanics gate): no provider is called, every database is a tmp file.

The 200-row tests at the bottom are the P1.10-a MECHANICS check: for each path that writes
rows, 200 rows through the real writer, >= 99% carry session_id and task_id.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from llm_router import call_identity as ci
from llm_router import cost
from llm_router.hooks.direct_executor import DirectResult, ModelSpec
from llm_router.types import LLMResponse, RoutingProfile, TaskType

_REPO = Path(__file__).resolve().parent.parent
_HOOKS = _REPO / "src" / "llm_router" / "hooks"
SID = "5a1c0000-0000-4000-8000-00000000a001"
HEX16 = re.compile(r"[0-9a-f]{16}")


def _rows(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _cols(db: Path, table: str) -> list[str]:
    return [r[1] for r in _rows(db, f"PRAGMA table_info({table})")]


def _resp(i: int = 0) -> LLMResponse:
    return LLMResponse(content="ok", model="gpt-4o-mini", input_tokens=62 + i, output_tokens=164,
                       cost_usd=0.0007, latency_ms=300.0, provider="openai")


def _direct() -> DirectResult:
    return DirectResult(text="some answer", model=ModelSpec(provider="ollama", model="qwen3.5:latest"),
                        latency_ms=650, input_tokens=120, output_tokens=60)


# ── ids ──────────────────────────────────────────────────────────────────────

def test_task_id_is_the_plan_formula_sha256_of_session_plus_turn():
    import hashlib

    assert ci.derive_task_id(SID, 3) == hashlib.sha256((SID + "3").encode()).hexdigest()[:16]


def test_task_id_is_sha256_of_session_and_turn_16_hex():
    t1, t2 = ci.derive_task_id(SID, 1), ci.derive_task_id(SID, 2)
    assert HEX16.fullmatch(t1) and HEX16.fullmatch(t2)
    assert t1 != t2 and t1 == ci.derive_task_id(SID, 1)
    assert t1 != ci.derive_task_id("other-session", 1)


def test_begin_turn_advances_per_session_and_other_readers_see_it(temp_db):
    first = ci.begin_turn(SID)
    assert first == ci.derive_task_id(SID, 1) == ci.turn_task_id(SID)
    second = ci.begin_turn(SID)
    assert second == ci.derive_task_id(SID, 2) == ci.turn_task_id(SID)
    assert ci.begin_turn("another-session") == ci.derive_task_id("another-session", 1)
    assert ci.turn_task_id(SID) == second            # the other session did not move this one


def test_begin_turn_refuses_placeholders_and_never_raises(temp_db):
    for bad in ("", None, "sdk", "unknown", "has space", 5, "x" * 200):
        assert ci.begin_turn(bad) is None
        assert ci.turn_task_id(bad) is None


def test_trace_is_new_per_scope_and_task_is_shared_by_nested_calls(temp_db):
    ci.begin_turn(SID)
    with ci.scope(SID) as (task_a, trace_a):
        with ci.scope(SID) as (task_b, trace_b):          # an escalation / retry inside the call
            assert task_b == task_a == ci.turn_task_id(SID)
            assert trace_b != trace_a
            assert ci.current_trace_id() == trace_b
        assert ci.current_trace_id() == trace_a
    assert ci.current_task_id() is None and ci.current_trace_id() is None


def test_explicit_task_id_wins_and_placeholders_are_ignored(temp_db):
    ci.begin_turn(SID)
    with ci.scope(SID, task_id="caller-task-7") as (task, _):
        assert task == "caller-task-7"
    with ci.scope(SID, task_id="sdk") as (task, _):       # a placeholder is not an id
        assert task == ci.turn_task_id(SID)


def test_unknown_task_is_generated_never_none(temp_db):
    task, trace = ci.row_ids(None)
    assert ci.is_generated_task_id(task) and len(task) == 16 and re.fullmatch(r"[0-9a-f]{32}", trace)
    assert not ci.is_generated_task_id(ci.derive_task_id(SID, 1))
    with ci.scope(None) as (t1, _):
        pass
    with ci.scope(None) as (t2, _):
        pass
    assert t1 != t2                                       # nothing names the task: one per call


# ── schema: additive and safe on an existing ledger ──────────────────────────

def test_migration_adds_columns_keeps_old_rows_null_and_is_idempotent(temp_db):
    from llm_router.config import get_config

    db_path = Path(get_config().llm_router_db_path)
    con = sqlite3.connect(str(db_path))
    con.execute(cost.CREATE_TABLE)
    con.execute(cost.CREATE_ROUTING_DECISIONS_TABLE)
    for i in range(3):
        con.execute("INSERT INTO usage (model, provider, task_type, profile, input_tokens, output_tokens, "
                    "cost_usd, latency_ms) VALUES ('m', 'p', 'code', 'balanced', 9, 1, 0, 1)")
    con.commit()
    assert "task_id" not in _cols(db_path, "usage")
    con.close()

    async def _open_twice():
        for _ in range(2):
            db = await cost._get_db()
            await db.close()

    asyncio.run(_open_twice())
    for table in ("usage", "routing_decisions"):
        cols = _cols(db_path, table)
        assert cols.count("task_id") == 1 and cols.count("trace_id") == 1
        assert cols.count("session_id") == 1              # already there; not added twice
    assert _rows(db_path, "SELECT id, task_id, trace_id FROM usage ORDER BY id") == [
        (1, None, None), (2, None, None), (3, None, None)]    # history is unknown, not guessed


# ── writers ──────────────────────────────────────────────────────────────────

def test_usage_and_routing_rows_of_one_call_share_task_and_trace(temp_db):
    from llm_router.config import get_config

    db_path = Path(get_config().llm_router_db_path)
    ci.begin_turn(SID)

    async def _call():
        with ci.scope(SID):
            await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED, session_id=SID)
            await cost.log_routing_decision(
                prompt="p", task_type="code", profile="balanced", classifier_type="heuristic",
                classifier_model=None, classifier_confidence=None, classifier_latency_ms=None,
                complexity="simple", recommended_model="gpt-4o-mini", base_model="gpt-4o-mini",
                was_downshifted=None, budget_pct_used=None, quality_mode=None,
                final_model="gpt-4o-mini", final_provider="openai", success=True,
                input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0, session_id=SID)

    asyncio.run(_call())
    u = _rows(db_path, "SELECT session_id, task_id, trace_id FROM usage")
    r = _rows(db_path, "SELECT session_id, task_id, trace_id FROM routing_decisions")
    assert len(u) == len(r) == 1 and u == r
    assert u[0][0] == SID and u[0][1] == ci.turn_task_id(SID)
    assert re.fullmatch(r"[0-9a-f]{32}", u[0][2])


def test_failed_and_cache_rows_get_ids_without_passing_them(temp_db):
    """PR #398's error / cache-hit rows call log_usage and log_routing_decision with no id
    arguments; the ContextVar scope stamps them, so that PR needs no change from this one."""
    from llm_router.config import get_config

    db_path = Path(get_config().llm_router_db_path)

    async def _call():
        with ci.scope(SID, task_id="caller-task-9") as (_, trace):
            await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED, success=False,
                                 correlation_id="c1", reason="error_timeout", session_id=SID)
            return trace

    trace = asyncio.run(_call())
    assert _rows(db_path, "SELECT success, reason, task_id, trace_id FROM usage") == [
        (0, "error_timeout", "caller-task-9", trace)]


def test_398_error_row_carries_the_scope_ids_on_both_tables(temp_db):
    """The real ``cost.log_route_error`` (LEDGER-ERR-1): no id passed, both rows stamped."""
    from llm_router.config import get_config

    db_path = Path(get_config().llm_router_db_path)
    turn = ci.begin_turn(SID)

    async def _call():
        with ci.scope(SID) as (_, trace):
            ok = await cost.log_route_error(TaskType.CODE, RoutingProfile.BALANCED,
                                            reason=cost.REASON_ERROR_TIMEOUT, correlation_id="c-err",
                                            attempted_model="openai/gpt-4o-mini", latency_ms=5.0)
            return ok, trace

    ok, trace = asyncio.run(_call())
    assert ok
    for table in ("usage", "routing_decisions"):
        assert _rows(db_path, f"SELECT task_id, trace_id FROM {table}") == [(turn, trace)], table


def test_explicit_ids_on_the_writer_win_over_the_scope(temp_db):
    from llm_router.config import get_config

    db_path = Path(get_config().llm_router_db_path)

    async def _call():
        with ci.scope(SID):
            await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED, session_id=SID,
                                 task_id="explicit-task", trace_id="explicit-trace")

    asyncio.run(_call())
    assert _rows(db_path, "SELECT task_id, trace_id FROM usage") == [("explicit-task", "explicit-trace")]


def test_direct_hook_rows_carry_the_turn_and_agree_across_tables(temp_db):
    from llm_router.config import get_config
    from llm_router.hooks.savings_logger import log_direct_to_db

    db_path = Path(get_config().llm_router_db_path)
    turn = ci.begin_turn(SID)
    log_direct_to_db(_direct(), prompt="x", task_type="code", complexity="simple", session_id=SID)
    u = _rows(db_path, "SELECT session_id, task_id, trace_id FROM usage")
    r = _rows(db_path, "SELECT session_id, task_id, trace_id FROM routing_decisions")
    assert u == r and u[0][:2] == (SID, turn) and u[0][2]


def test_subagent_row_shares_the_parent_turn_task(temp_db):
    """agent-route writes through log_direct_to_db with the payload's session id: the
    sub-agent's rows read the parent's current turn, so one task spans the whole fan-out."""
    from llm_router.config import get_config
    from llm_router.hooks.savings_logger import log_direct_to_db

    db_path = Path(get_config().llm_router_db_path)
    turn = ci.begin_turn(SID)
    for _ in range(3):                                    # three sub-agents of one turn
        log_direct_to_db(_direct(), prompt="x", task_type="research", complexity="moderate",
                         classifier_type="agent-route", session_id=SID)
    tasks = {r[0] for r in _rows(db_path, "SELECT task_id FROM usage")}
    traces = {r[0] for r in _rows(db_path, "SELECT trace_id FROM usage")}
    assert tasks == {turn} and len(traces) == 3


def test_cc_usage_track_writes_task_and_trace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    ci.begin_turn(SID)
    spec = importlib.util.spec_from_file_location("_p110_cc_usage_track", _HOOKS / "cc-usage-track.py")
    hook = importlib.util.module_from_spec(spec)
    sys.modules["_p110_cc_usage_track"] = hook
    spec.loader.exec_module(hook)
    payload = {"session_id": SID, "tool_name": "Agent",
               "tool_input": {"subagent_type": "Explore", "prompt": "p" * 40},
               "tool_response": {"output": "r" * 40}, "duration_ms": 1200}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        hook.main()
    row = _rows(home / "usage.db", "SELECT session_id, task_id, trace_id FROM usage")
    assert row[0][:2] == (SID, ci.turn_task_id(SID)) and row[0][2]


def test_edit_outcome_row_carries_ids(temp_db):
    from llm_router import edit_ledger, paths

    ci.begin_turn(SID)
    edit_ledger.record_edit_outcome(file="a.py", model="m", applied=True, source="zero_claude",
                                    session_id=SID, turn_id="abc")
    row = json.loads(paths.state_path(edit_ledger.LEDGER_FILENAME).read_text().splitlines()[-1])
    assert row["task_id"] == ci.turn_task_id(SID) and row["trace_id"]


def test_execution_event_carries_ids(tmp_path):
    from llm_router import execution_ledger as el

    db = tmp_path / "exec.db"
    ci.begin_turn(SID)
    assert el.record_event(el.LedgerEvent(session_id=SID, verify="V1:pass"), path=db)
    assert el.record_event(el.LedgerEvent(session_id=SID, task_id="t-explicit", trace_id="tr-x"), path=db)
    rows = _rows(db, "SELECT session_id, task_id, trace_id FROM execution_events ORDER BY ts, rowid")
    assert rows[0][:2] == (SID, ci.turn_task_id(SID)) and rows[0][2]
    assert rows[1] == (SID, "t-explicit", "tr-x")


def test_execution_ledger_migrates_an_old_table(tmp_path):
    from llm_router import execution_ledger as el

    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    old_ddl = el._DDL.replace("    task_id TEXT,\n    trace_id TEXT\n", "").replace(
        "    used INTEGER,\n", "    used INTEGER\n")             # the table as it was before P1.10
    assert "task_id" not in old_ddl
    con.executescript(old_ddl)
    con.execute("INSERT INTO execution_events (schema_version, event_id, ts, session_id, event_type) "
                "VALUES (1, 'e0', 1.0, 's', 'route_started')")
    con.commit()
    con.close()
    el.record_event(el.LedgerEvent(session_id=SID), path=db)   # opens, migrates, writes
    assert {"task_id", "trace_id"} <= set(_cols(db, "execution_events"))
    assert _rows(db, "SELECT task_id FROM execution_events WHERE event_id = 'e0'") == [(None,)]


def test_proxy_row_identity_fields(temp_db):
    from llm_router.proxy.server import identity_fields

    turn = ci.begin_turn(SID)
    f = identity_fields(SID)
    assert f["task_id"] == turn and f["trace_id"]
    g = identity_fields(None)                              # a client with no session id
    assert ci.is_generated_task_id(g["task_id"]) and g["trace_id"]


# ── SDK ──────────────────────────────────────────────────────────────────────

def _stub_sdk(monkeypatch):
    import llm_router.gateway as gw
    import llm_router.hooks.chain_builder as cb
    import llm_router.hooks.direct_executor as de

    monkeypatch.setattr(gw, "_classify", lambda p: ("code", "simple"))
    monkeypatch.setattr(cb, "build_chain", lambda *a, **k: [])
    monkeypatch.setattr(cb, "get_current_pressure", lambda *a, **k: ("normal", 0.1), raising=False)
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: _direct())


def test_sdk_stops_stamping_the_sdk_placeholder(temp_db, monkeypatch):
    from llm_router import sdk
    from llm_router.config import get_config

    _stub_sdk(monkeypatch)
    monkeypatch.setattr(sdk, "_PROCESS_SESSION", None)
    sdk.route("what is 2+2", task_type="code", complexity="simple")
    sdk.route("what is 3+3", task_type="code", complexity="simple",
              session_id="caller-session-1", task_id="caller-task-1")
    rows = _rows(Path(get_config().llm_router_db_path),
                 "SELECT session_id, task_id FROM usage ORDER BY id")
    assert len(rows) == 2
    assert rows[0][0].startswith("sdk-") and rows[0][0] != "sdk" and ci.is_generated_task_id(rows[0][1])
    assert rows[1] == ("caller-session-1", "caller-task-1")
    assert _rows(Path(get_config().llm_router_db_path),
                 "SELECT count(*) FROM routing_decisions WHERE session_id = 'sdk'") == [(0,)]


# ── the hook advances the turn, both copies ──────────────────────────────────

def test_auto_route_begins_a_turn_before_it_writes_anything_in_both_copies():
    copies = [_HOOKS / "auto-route.py", _REPO / "hooks" / "auto-route.py"]
    assert copies[0].read_bytes() == copies[1].read_bytes()
    text = copies[0].read_text()
    begin = text.index("_ci_turn.begin_turn(session_id)")
    assert begin < text.index("_session_store.record_event(")
    assert begin < text.index("log_routing_decision(\n                task_type=task_type")
    assert begin > text.index('session_id = hook_input.get("session_id", "")')


# ── P1.10-a mechanics: >= 99% of >= 200 rows per path carry session_id and task_id ─────────

N = 200


def _complete(rows: list[tuple]) -> float:
    ok = sum(1 for r in rows if all(v not in (None, "") for v in r))
    return ok / len(rows)


@pytest.mark.timeout(180)
def test_p110a_usage_and_routing_paths_200_rows(temp_db):
    from llm_router.config import get_config
    from llm_router.hooks.savings_logger import log_direct_to_db

    db_path = Path(get_config().llm_router_db_path)

    for k in range(7):                                    # one human turn per session
        ci.begin_turn(f"{SID[:-3]}{k:03d}")

    async def _mcp(i: int):
        sid = f"{SID[:-3]}{i % 5:03d}"
        with ci.scope(sid):
            await cost.log_usage(_resp(i), TaskType.CODE, RoutingProfile.BALANCED, session_id=sid)

    async def _all():
        for i in range(N):
            await _mcp(i)

    asyncio.run(_all())
    for i in range(N):                                    # hook DIRECT + sub-agent path
        sid = f"{SID[:-3]}{i % 7:03d}"
        log_direct_to_db(_direct(), prompt="x", task_type="code", complexity="simple", session_id=sid)

    usage = _rows(db_path, "SELECT session_id, task_id, trace_id FROM usage WHERE reason IS NOT 'direct'")
    direct_usage = _rows(db_path, "SELECT session_id, task_id, trace_id FROM usage WHERE reason = 'direct'")
    direct_rd = _rows(db_path, "SELECT session_id, task_id, trace_id FROM routing_decisions "
                               "WHERE reason_code = 'direct'")
    assert (len(usage), len(direct_usage), len(direct_rd)) == (N, N, N)
    for rows in (usage, direct_usage, direct_rd):
        assert _complete(rows) >= 0.99
    # one task per (session, turn): each of the 5 sessions' task ids is stable across its rows
    assert len({r[1] for r in usage}) == 5


def test_p110a_proxy_and_edit_paths_200_rows(temp_db):
    from llm_router import edit_ledger, paths
    from llm_router.proxy.server import identity_fields

    sids = [f"{SID[:-3]}{i:03d}" for i in range(4)]
    for s in sids:
        ci.begin_turn(s)
    proxy = [identity_fields(sids[i % 4]) | {"session_id": sids[i % 4]} for i in range(N)]
    assert _complete([(r["session_id"], r["task_id"], r["trace_id"]) for r in proxy]) >= 0.99
    for i in range(N):
        edit_ledger.record_edit_outcome(file=f"f{i}.py", model="m", applied=True, source="llm_edit",
                                        session_id=sids[i % 4])
    edits = [json.loads(ln) for ln in paths.state_path(edit_ledger.LEDGER_FILENAME).read_text().splitlines()]
    assert len(edits) == N
    assert _complete([(r["session_id"], r["task_id"], r["trace_id"]) for r in edits]) >= 0.99


def test_g3_reads_task_id_from_the_real_writers(temp_db):
    """End to end: rows from the real writers, read by the kpi G3 reader, task_id recorded."""
    from llm_router.commands import kpi

    async def _all():
        for i in range(120):
            sid = f"{SID[:-3]}{i % 3:03d}"
            ci.begin_turn(sid)
            with ci.scope(sid):
                await cost.log_usage(_resp(i), TaskType.CODE, RoutingProfile.BALANCED, session_id=sid,
                                     reason="router_chain")

    asyncio.run(_all())
    recs, _no_col, unreadable = kpi._prd_sql_records(None, None)
    assert not unreadable and len(recs["usage"]) == 120
    assert all(vals["task_id"] for _sid, vals in recs["usage"])
    assert "task_id" not in _no_col["usage"]


# ── review fixes: lock, prune, generated ids not credited, G3 merge-date cutoff ──────────

def test_concurrent_begin_turn_never_loses_an_increment(temp_db):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda _: ci.begin_turn(SID), range(40)))
    assert len(set(got)) == 40                       # 40 distinct turns, none written twice
    assert ci.turn_task_id(SID) in got


def test_first_turn_prunes_old_turn_state_only(temp_db):
    import os
    import time

    ci.begin_turn("old-session")
    old = ci._turn_state_path("old-session")
    stale = time.time() - ci.TURN_STATE_TTL_S - 60
    os.utime(old, (stale, stale))
    ci.begin_turn("fresh-session")                   # idx == 1: housekeeping runs
    assert not old.exists() and ci._turn_state_path("fresh-session").exists()
    ci.begin_turn("fresh-session")                   # idx == 2: no scan
    assert ci._turn_state_path("fresh-session").exists()


def test_generated_task_ids_are_not_credited_by_g3(temp_db, monkeypatch):
    from llm_router.commands import kpi

    async def _all():
        for i in range(10):
            with ci.scope(None):                     # no session, no turn: generated stand-in
                await cost.log_usage(_resp(i), TaskType.CODE, RoutingProfile.BALANCED)

    asyncio.run(_all())
    recs, _nc, _un = kpi._prd_sql_records(None, None)
    assert len(recs["usage"]) == 10 and all(v["task_id"] is None for _s, v in recs["usage"])


def test_g3_scores_task_id_only_from_the_merge_date(temp_db, monkeypatch):
    from llm_router.commands import kpi

    class _Idx:
        def resolve(self, sid, stamp):
            class _K:
                kind = "organic"
            return _K()

    cut = 1_000_000.0
    rows = [(f"s{i}", {"session_id": f"s{i}", "task_id": None if i < 50 else "t", "model": "m",
                       "tier": "t", "reason": "r", "tokens": (1, 1), "cost": 0.0, "latency": 1.0,
                       "outcome": 1, "_ts": cut - 1000 if i < 50 else cut + 1000}) for i in range(100)]
    stamps = [None] * 100
    monkeypatch.setattr(kpi, "G3_TASK_ID_SCORED_FROM", None)
    off = kpi._prd_writer_result(rows, stamps, _Idx(), frozenset(), set())
    assert off["state"] == "pass" and off["fields"]["task_id"]["scored"] is False
    assert off["fields"]["task_id"]["missing"] == 50              # reported, not scored
    monkeypatch.setattr(kpi, "G3_TASK_ID_SCORED_FROM", cut)
    on = kpi._prd_writer_result(rows, stamps, _Idx(), frozenset(), set())
    assert on["state"] == "pass" and on["complete"] == 100        # NULL rows predate the cutoff
    late = [(s_, {**v, "_ts": cut + 5}) for s_, v in rows]        # same rows, now after it
    assert kpi._prd_writer_result(late, stamps, _Idx(), frozenset(), set())["state"] == "fail"
