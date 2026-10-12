"""P0.8 hardening: the env session is stamped only where it provably belongs to the call.

``CLAUDE_CODE_SESSION_ID`` is the MCP server process's, fixed at start. It is right for a
Claude Code stdio server (one per session); wrong for SSE (many clients, one process) and
for hosts that inherited it from a shell. A wrong real-looking id is worse than
``unknown:mcp``. Also pins the shared ``is_unknown_session`` predicate in each reader.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from llm_router import call_identity as ci
from llm_router import northstar
from llm_router.commands import kpi

SID = "11111111-aaaa-bbbb-cccc-222222222222"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(ci, "_STDIO_TRANSPORT", False)


def _probe(seen: list):
    async def tool() -> str:
        seen.append(ci.call_session_id())
        return "ok"

    server = ci.IdentityMCPServer("probe")
    server.tool()(tool)
    return server


def _ctx(tool_use: str | None):
    meta = {ci.TOOL_USE_META_KEY: tool_use} if tool_use else {}
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


def _call(stdio: bool, tool_use: str | None):
    ci._STDIO_TRANSPORT = stdio
    seen: list = []
    asyncio.run(_probe(seen).call_tool("tool", {}, _ctx(tool_use)))
    return seen[0]


def test_stdio_without_meta_stamps_the_env_session():
    assert _call(True, None) == SID


def test_sse_without_meta_is_not_stamped():
    assert _call(False, None) is None
    assert ci.unknown_session(ci.DOOR_MCP) == "unknown:mcp"


@pytest.mark.parametrize("stdio", [True, False])
def test_with_a_tool_use_id_the_env_session_is_stamped_on_both_transports(stdio):
    assert _call(stdio, "toolu_01ABC") == SID


def test_outside_a_call_nothing_is_stamped_even_on_stdio():
    ci._STDIO_TRANSPORT = True
    assert ci.call_session_id() is None


def test_server_main_marks_stdio_and_sse_entry_points_do_not():
    import inspect

    from llm_router import server

    assert "mark_stdio_transport()" in inspect.getsource(server.main)
    assert "mark_stdio_transport" not in inspect.getsource(server.main_sse)
    assert "mark_stdio_transport" not in inspect.getsource(server.main_sse_secured)


def test_interleaved_calls_with_different_ids_do_not_leak():
    ci._STDIO_TRANSPORT = False
    seen: dict[str, list] = {"a": [], "b": []}
    gate_a, gate_b = asyncio.Event(), asyncio.Event()

    async def run():
        async def tool(who: str) -> str:
            seen[who].append((ci.tool_use_id(), ci.call_session_id()))
            (gate_b if who == "a" else gate_a).set()
            await (gate_a if who == "a" else gate_b).wait()  # interleave: each waits for the other
            seen[who].append((ci.tool_use_id(), ci.call_session_id()))
            return who

        server = ci.IdentityMCPServer("probe")
        server.tool()(tool)
        await asyncio.gather(server.call_tool("tool", {"who": "a"}, _ctx("toolu_A")),
                             server.call_tool("tool", {"who": "b"}, _ctx("toolu_B")))
        assert not ci.in_mcp_call() and ci.tool_use_id() is None and ci.call_session_id() is None

    asyncio.run(run())
    assert seen["a"] == [("toolu_A", SID)] * 2
    assert seen["b"] == [("toolu_B", SID)] * 2


def test_interleaved_bare_and_bound_calls_do_not_share_the_flag():
    """A bare call on SSE running beside a bound one must stay unknown."""
    ci._STDIO_TRANSPORT = False
    got: dict[str, str | None] = {}
    bound_in, bare_done = asyncio.Event(), asyncio.Event()

    async def run():
        async def tool(who: str) -> str:
            if who == "bound":
                bound_in.set()
                await bare_done.wait()
            else:
                await bound_in.wait()
            got[who] = ci.call_session_id()
            if who == "bare":
                bare_done.set()
            return who

        server = ci.IdentityMCPServer("probe")
        server.tool()(tool)
        await asyncio.gather(server.call_tool("tool", {"who": "bound"}, _ctx("toolu_A")),
                             server.call_tool("tool", {"who": "bare"}, _ctx(None)))

    asyncio.run(run())
    assert got == {"bound": SID, "bare": None}


# ── the shared predicate and its readers ─────────────────────────────────────

@pytest.mark.parametrize("sid,expected", [
    (None, True), ("", True), ("unknown", True), ("unknown:mcp", True), ("unknown:non_mcp", True),
    (SID, False), ("s-1", False), (5, True)])
def test_is_unknown_session(sid, expected):
    assert ci.is_unknown_session(sid) is expected


def test_offload_share_counts_unknown_rows_as_no_session():
    rows = [{"session_id": s, "ts": "2026-10-06T14:00:00+00:00"} for s in ("unknown:mcp", "unknown:non_mcp", "")]
    from tests.test_kpi_offload_share import _build

    built = _build([], local=rows)
    assert built["units"] == [] and built["local_no_session"] == 3
    assert built["local_untagged"] == 0


def test_kpi_readers_treat_the_sentinel_as_missing():
    assert kpi._usable_session({"session_id": "unknown:mcp"}) is False
    assert kpi._usable_session({"session_id": "unknown"}) is False
    assert kpi._usable_session({"session_id": ""}) is False
    assert kpi._usable_session({"session_id": SID}) is True
    assert kpi._prd_session("unknown:mcp") is None and kpi._prd_session(SID) == SID


def test_northstar_shadow_units_drop_the_sentinel(tmp_path):
    import sqlite3

    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, timestamp TEXT, session_id TEXT, "
                "task_type TEXT, complexity TEXT, final_model TEXT, latency_ms REAL, final_provider TEXT, "
                "provenance TEXT)")
    for i, s in enumerate(("unknown:mcp", SID)):
        con.execute("INSERT INTO routing_decisions VALUES (?, '2099-01-01 00:00:00', ?, 'code', 'simple', "
                    "'qwen', 5.0, 'ollama', 'runtime')", (i + 1, s))
    con.commit()
    con.close()
    out = list(northstar.local_shadow_units(days=None, db_path=db))
    assert sorted(u["session_id"] or "" for u in out) == ["", SID]
