"""MCP door: an MCP client calls a llm-router tool over stdio.

    python examples/mcp/main.py

Spawns ``llm-router``'s MCP server (on the offline fake, see examples/fake_backend.py), calls one
routed tool through the official ``mcp`` client, asserts the answer and the ledger row. In Claude
Code the same server is registered by ``llm-router install``; nothing else changes.
"""
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import fake_backend as fb  # noqa: E402

fb.install()

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


async def main() -> str:
    server = StdioServerParameters(
        command=sys.executable, args=[str(HERE / "fake_backend.py"), "mcp-server"],
        env=fb.child_env())                             # carries LLM_ROUTER_EXAMPLE_HOME: same ledger
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            fb.expect("llm" in names, f"llm not offered: {sorted(names)[:10]}")
            reply = await session.call_tool("llm", {"prompt": fb.PROMPT, "task": "query", "tier": "fast"})
            fb.expect(not reply.is_error, f"tool error: {reply.content}")
            return "".join(getattr(c, "text", "") for c in reply.content)


text = asyncio.run(main())
print(text)
fb.expect(fb.FINAL in text, f"unexpected answer: {text!r}")
rows = fb.usage_rows()
fb.expect(len(rows) >= 1 and rows[0]["success"] == 1, f"no successful ledger row: {rows}")
fb.finish()
