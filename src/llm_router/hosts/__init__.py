"""Host adapters — wire LLM Router's MCP server into each supported CLI.

Each host has its own manifest format and config location:

    claude_code: ~/.claude/claude_desktop_config.json (Claude Desktop)
                 or .mcp.json + .claude-plugin/ (Claude Code CLI)
    cursor:      ~/.cursor/mcp.json
    codex_oai:   OpenAI Codex IDE — manifest under .codex/plugins/
    codex_cli:   .codex-plugin/ (already supported in llm-router)
    gemini_cli:  ~/.gemini/mcp_servers.json (Gemini CLI v1.3+)

Adapters are pure config writers — they don't speak to the host process.
The MCP server (llm_router-sse or llm_router stdio) does the actual work.
"""
from llm_router.hosts.base import HostAdapter

__all__ = ["HostAdapter"]
