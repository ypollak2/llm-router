"""P0.8 fix: a ``usage`` row never has a NULL ``session_id``.

Live 2026-10-11 (copy of usage.db): ids 1885-1887, reason ``router_chain``, session_id
NULL, every other row in the 67-row window had one. Cause: ``log_usage`` took the id
from ``call_identity.call_session_id()``, which returned None unless the client had sent
a ``tool_use`` id in ``_meta``; an MCP call without one, or a call outside MCP, wrote
NULL. The P0.8-d scanner test only checks that ``reason=`` is passed; ``session_id`` has a
default, so the call sites passed and the value was None at runtime. These tests run the
writer and read the row.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from tests.test_p05_semantic_cache_key import _route
from tests.test_p05_semantic_cache_key import cache_env as _cache_env
from tests.test_p08e_cache_hit_row import real_usage_writer as _real_usage_writer

from llm_router import call_identity, cost
from llm_router.commands import kpi
from llm_router.types import LLMResponse, RoutingProfile, TaskType


cache_env = _cache_env  # re-exported fixtures
real_usage_writer = _real_usage_writer


def _resp() -> LLMResponse:
    return LLMResponse(content="ok", model="gpt-5.5", input_tokens=37, output_tokens=11,
                       cost_usd=0.0002, latency_ms=50.0, provider="codex")


def _sessions(db) -> list[str | None]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT session_id FROM usage ORDER BY id")]
    finally:
        con.close()


def _write() -> None:
    asyncio.run(cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.PREMIUM,
                               reason=cost.REASON_ROUTER_CHAIN))


def test_router_chain_row_outside_mcp_is_unknown_not_null(temp_db, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    _write()
    assert _sessions(temp_db) == ["unknown:non_mcp"]


def test_env_session_outside_an_mcp_call_is_still_not_stamped(temp_db, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-shell")
    _write()
    assert _sessions(temp_db) == ["unknown:non_mcp"]


def test_mcp_call_without_a_tool_use_id_gets_the_env_session(temp_db, monkeypatch):
    """The reproduced trigger: inside call_tool on stdio, client sent no _meta toolUseId."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-mcp-1")
    monkeypatch.setattr(call_identity, "_STDIO_TRANSPORT", True)  # server.main() sets this

    async def tool() -> str:
        assert call_identity.tool_use_id() is None  # the client sent none
        await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED,
                             reason=cost.REASON_ROUTER_CHAIN)
        return "done"

    server = call_identity.IdentityMCPServer("probe")
    server.tool()(tool)
    asyncio.run(server.call_tool("tool", {}, None))
    assert _sessions(temp_db) == ["sess-mcp-1"]
    assert not call_identity.in_mcp_call()  # flag does not leak out of the call


def test_mcp_call_with_no_env_session_is_unknown_mcp(temp_db, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    async def tool() -> str:
        await cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED,
                             reason=cost.REASON_ROUTER_CHAIN)
        return "done"

    server = call_identity.IdentityMCPServer("probe")
    server.tool()(tool)
    asyncio.run(server.call_tool("tool", {}, None))
    assert _sessions(temp_db) == ["unknown:mcp"]


def test_placeholder_session_is_unknown_not_null(temp_db):
    asyncio.run(cost.log_usage(_resp(), TaskType.CODE, RoutingProfile.BALANCED,
                               session_id="sdk", reason=cost.REASON_ROUTER_CHAIN))
    assert _sessions(temp_db) == ["unknown:non_mcp"]


def test_g3_counts_unknown_values_as_missing():
    assert kpi._prd_session("unknown:mcp") is None
    assert kpi._prd_session("unknown:non_mcp") is None
    assert kpi._prd_session("unknown") is None
    assert kpi._prd_session("") is None
    assert kpi._prd_session("5041bdf8-0980-4738-8845-5106d8a71999") is not None


@pytest.mark.asyncio
async def test_route_and_call_router_chain_row_is_never_null(cache_env, real_usage_writer, monkeypatch):
    """Real ``route_and_call`` and real ``log_usage`` (only the provider is stubbed): the
    ``router_chain`` row, with no session anywhere, reads ``unknown:non_mcp``, not NULL."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    await _route("explain the retry policy", caller_context="c", calls=[])
    con = sqlite3.connect(str(cache_env))
    try:
        rows = con.execute("SELECT session_id, reason FROM usage").fetchall()
    finally:
        con.close()
    assert rows == [("unknown:non_mcp", "router_chain")]
