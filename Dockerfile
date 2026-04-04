FROM python:3.12-slim

WORKDIR /app

# Install uv for fast package management (uvx pattern)
RUN pip install --no-cache-dir uv

# uvx installs claude-code-llm-router into an isolated venv and runs it.
# stdio transport is used for MCP proxy inspection (Glama, Claude Code, etc.)
CMD ["uvx", "claude-code-llm-router"]
