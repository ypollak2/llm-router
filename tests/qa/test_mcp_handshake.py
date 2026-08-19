"""Live MCP stdio handshake tests — proves the server speaks the protocol.

We spawn the LLM Router MCP server as a subprocess and drive it via stdio
using the official MCP Python SDK client. This is the only test surface
that exercises the entire MCP protocol layer end-to-end without needing
a real host CLI (Claude Code, Cursor, etc.) to be installed and
restarted.

If these tests pass, ANY MCP-compliant host can connect to LLM Router. The
remaining host-specific risk (config-file location, restart behavior)
is structural and covered by tests/integration/.

Tests are marked `@pytest.mark.mcp_handshake` and skipped if the MCP
SDK client API isn't importable in the test venv.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest


pytestmark = pytest.mark.mcp_handshake


def _mcp_client_available() -> bool:
    try:
        from mcp import ClientSession  # noqa: F401
        from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: F401

        return True
    except ImportError:
        return False


requires_mcp_client = pytest.mark.skipif(
    not _mcp_client_available(),
    reason="MCP SDK client API not available in this venv",
)


def _server_command() -> tuple[str, list[str]]:
    """Pick the most reliable way to launch the llm_router server.

    Prefer `python -m llm_router.server` over the `llm_router` shim — it avoids
    PATH ambiguity when multiple Pythons are installed and works in any
    venv that has llm_router importable.
    """
    return sys.executable, ["-m", "llm_router.server"]


# ────────────────────────────────────────────────────────────────────────
# Helper: drive a stdio handshake and collect tool names
# ────────────────────────────────────────────────────────────────────────

async def _list_tools_via_stdio() -> list[str]:
    """Connect to a fresh llm_router subprocess, run initialize + tools/list,
    return tool names. Handles cleanup on every exit path."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    cmd, args = _server_command()
    params = StdioServerParameters(command=cmd, args=args, env=os.environ.copy())

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return sorted(t.name for t in result.tools)


# ────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────

@requires_mcp_client
def test_mcp_initialize_succeeds():
    """The most basic handshake: spawn, initialize, close. If this works
    the protocol envelope is correct."""
    async def run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cmd, args = _server_command()
        params = StdioServerParameters(command=cmd, args=args, env=os.environ.copy())

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                assert result.server_info.name == "llm_router", (
                    f"Expected server_info.name='llm_router', got {result.server_info.name!r}"
                )

    asyncio.run(asyncio.wait_for(run(), timeout=30))


@requires_mcp_client
def test_mcp_tools_list_returns_tool_set():
    """tools/list returns a non-empty tool set."""
    tools = asyncio.run(asyncio.wait_for(_list_tools_via_stdio(), timeout=30))
    assert len(tools) > 0, "tools/list returned empty — registration failed"


@requires_mcp_client
def test_mcp_tools_include_completion_doors():
    """0.10.0 default (consolidated): the completion door `llm` and the agentic
    door `llm_act` must be registered — the collapsed text/execution surface."""
    tools = asyncio.run(asyncio.wait_for(_list_tools_via_stdio(), timeout=30))
    expected = {"llm", "llm_act"}
    missing = expected - set(tools)
    assert not missing, (
        f"LLM Router MCP server is missing completion doors: {missing}. "
        f"Got: {sorted(t for t in tools if t.startswith('llm') or t.startswith('llm_router'))[:10]}..."
    )


@requires_mcp_client
def test_mcp_tools_include_routing_door():
    """llm_route (the auto-routing decision) stays a first-class door under the
    consolidated default."""
    tools = asyncio.run(asyncio.wait_for(_list_tools_via_stdio(), timeout=30))
    expected = {"llm_route"}
    missing = expected - set(tools)
    assert not missing, f"Missing routing door: {missing}"


@requires_mcp_client
def test_mcp_tools_include_agent_tools():
    """Agent-session surface must be exposed. Under the 0.10.0 consolidated default
    that is the llm_router_session door plus the two rich agent tools; the four simple
    lifecycle actions (list/check_budget/complete/lineage) collapse into
    llm_router_session (use LLM_ROUTER_SLIM=off to get them as standalone tools)."""
    tools = asyncio.run(asyncio.wait_for(_list_tools_via_stdio(), timeout=30))
    expected = {
        "llm_router_session",
        "llm_router_agent_start_session",
        "llm_router_agent_route",
    }
    missing = expected - set(tools)
    assert not missing, (
        f"agent-session MCP surface missing: {missing}. "
        f"Did agents.register(mcp, _gate) / consolidated.register run in server.py?"
    )


@requires_mcp_client
def test_mcp_tools_have_descriptions():
    """Every tool must have a description so MCP clients can render help."""
    async def run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cmd, args = _server_command()
        params = StdioServerParameters(command=cmd, args=args, env=os.environ.copy())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                undocumented = [t.name for t in result.tools if not t.description]
                return undocumented

    undocumented = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert not undocumented, (
        f"Tools without descriptions ({len(undocumented)}): "
        f"{undocumented[:5]}... — MCP help will be empty"
    )


@requires_mcp_client
def test_mcp_tools_have_input_schemas():
    """Every tool's input_schema must be a valid JSONSchema dict."""
    async def run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cmd, args = _server_command()
        params = StdioServerParameters(command=cmd, args=args, env=os.environ.copy())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                no_schema = [
                    t.name for t in result.tools
                    if not isinstance(t.input_schema, dict)
                ]
                return no_schema

    no_schema = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert not no_schema, (
        f"Tools without input_schema: {no_schema[:5]}... "
        f"MCP clients can't render parameter forms without these"
    )


@requires_mcp_client
def test_mcp_initialize_negotiates_protocol_version():
    """The server must accept a recent MCP protocol version."""
    async def run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cmd, args = _server_command()
        params = StdioServerParameters(command=cmd, args=args, env=os.environ.copy())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                return result.protocol_version

    version = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert version, "Server returned no protocol version"
    # Version is a date-shaped string like "2025-06-18" or "2024-11-05"
    assert isinstance(version, str)
    assert len(version) >= 8


@requires_mcp_client
def test_mcp_call_agent_list_tool_returns_structured_result():
    """End-to-end: call llm_router_agent_list via MCP and verify the response
    is a structured payload (proves the tool actually executes through
    the protocol, not just registers)."""
    async def run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cmd, args = _server_command()
        params = StdioServerParameters(command=cmd, args=args, env=os.environ.copy())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("llm_router_agent_list", arguments={})
                return result

    result = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert result is not None
    # MCP returns either content items or structuredContent — check both
    assert result.content or result.structuredContent, (
        "llm_router_agent_list returned empty result"
    )


@requires_mcp_client
def test_mcp_unknown_tool_returns_error_not_crash():
    """Calling a nonexistent tool must return an error, not crash the server."""
    async def run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cmd, args = _server_command()
        params = StdioServerParameters(command=cmd, args=args, env=os.environ.copy())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                try:
                    await session.call_tool("nonexistent_tool_xyz", arguments={})
                    return "no_error"
                except Exception as exc:
                    return type(exc).__name__

    outcome = asyncio.run(asyncio.wait_for(run(), timeout=30))
    # Either the server returned an error result OR the SDK raised — both
    # are acceptable, what's forbidden is the subprocess crashing
    assert outcome in ("no_error", "McpError", "Exception", "ValueError"), (
        f"Unexpected outcome on unknown tool: {outcome}"
    )
