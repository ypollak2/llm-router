FROM python:3.12-slim

WORKDIR /app

# stdio transport for MCP proxy inspection (Glama, Claude Code, etc.)
# uv run --with installs the package on-demand into an isolated venv
CMD ["uv", "run", "--with", "claude-code-llm-router", "llm-router"]
