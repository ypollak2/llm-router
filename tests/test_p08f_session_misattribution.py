"""P0.8 hardening: the env session is stamped only where it provably belongs to the call.

``CLAUDE_CODE_SESSION_ID`` is the MCP server process's, and every child of a Claude Code
shell inherits it (Codex, Cursor, Desktop). It is stamped only when the call shows it came
from Claude Code: a bound ``tool_use`` id, or ``clientInfo.name == "claude-code"`` in the
initialize handshake of the request's own MCP session. Anything else is ``unknown:mcp``.
Also pins the shared ``is_unknown_session`` predicate in each reader.
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


def _probe(seen: list):
    async def tool() -> str:
        seen.append(ci.call_session_id())
        return "ok"

    server = ci.IdentityMCPServer("probe")
    server.tool()(tool)
    return server


def _ctx(tool_use: str | None = None, client: str | None = None):
    """A request context shaped like the MCP SDK's: meta + session.client_params.client_info."""
    meta = {ci.TOOL_USE_META_KEY: tool_use} if tool_use else {}
    params = SimpleNamespace(client_info=SimpleNamespace(name=client)) if client else None
    session = SimpleNamespace(client_params=params)
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta, session=session))


def _call(tool_use: str | None = None, client: str | None = None):
    seen: list = []
    asyncio.run(_probe(seen).call_tool("tool", {}, _ctx(tool_use, client)))
    return seen[0]


@pytest.mark.parametrize("client", ["claude-code", "Claude-Code"])
def test_claude_code_client_without_meta_stamps_the_env_session(client):
    assert _call(client=client) == SID


@pytest.mark.parametrize("client", ["codex", "cursor", "claude-ai", "", None])
def test_other_or_missing_client_without_meta_is_unknown_even_with_env_set(client):
    assert _call(client=client) is None
    assert ci.unknown_session(ci.DOOR_MCP) == "unknown:mcp"


def test_missing_context_is_unknown():
    seen: list = []
    asyncio.run(_probe(seen).call_tool("tool", {}, None))
    assert seen == [None]


@pytest.mark.parametrize("client", ["codex", None, "claude-code"])
def test_a_bound_tool_use_id_stamps_the_env_session(client):
    assert _call(tool_use="toolu_01ABC", client=client) == SID


def test_outside_a_call_nothing_is_stamped():
    assert ci.call_session_id() is None


def test_server_main_leaves_no_global_state(monkeypatch):
    """server.main() once set a module flag that outlived the run and changed later tests."""
    from llm_router import server

    before = {k: v for k, v in vars(ci).items() if isinstance(v, (bool, int, str, type(None)))}
    monkeypatch.setattr(server, "_critical_modules_or_die", lambda: None)
    monkeypatch.setattr(server, "_startup_verify_or_die", lambda: None)
    monkeypatch.setattr(server, "mcp", type("M", (), {"run": lambda self: None})())
    server.main()
    after = {k: v for k, v in vars(ci).items() if isinstance(v, (bool, int, str, type(None)))}
    assert after == before
    assert _call(client="codex") is None  # a later non-Claude-Code call is still unknown


def test_interleaved_calls_from_different_sessions_do_not_leak():
    seen: dict[str, list] = {"cc": [], "codex": []}
    gate_a, gate_b = asyncio.Event(), asyncio.Event()

    async def run():
        async def tool(who: str) -> str:
            seen[who].append(ci.call_session_id())
            (gate_b if who == "cc" else gate_a).set()
            await (gate_a if who == "cc" else gate_b).wait()  # each waits for the other
            seen[who].append(ci.call_session_id())
            return who

        server = ci.IdentityMCPServer("probe")
        server.tool()(tool)
        await asyncio.gather(server.call_tool("tool", {"who": "cc"}, _ctx(client="claude-code")),
                             server.call_tool("tool", {"who": "codex"}, _ctx(client="codex")))
        assert not ci.in_mcp_call() and ci.call_session_id() is None

    asyncio.run(run())
    assert seen["cc"] == [SID, SID]
    assert seen["codex"] == [None, None]


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
