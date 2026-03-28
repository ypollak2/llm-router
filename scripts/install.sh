#!/usr/bin/env bash
# Install LLM Router as an MCP server in Claude Code.
# Usage: ./scripts/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CLAUDE_CONFIG="$HOME/.claude.json"

echo "╔══════════════════════════════════════════╗"
echo "║     LLM Router — Claude Code Install     ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Check for .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "No .env found. Running onboard wizard..."
    cd "$PROJECT_DIR"
    uv run llm-router-onboard
fi

# Check for uv
if ! command -v uv &>/dev/null; then
    echo "Error: 'uv' not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# Install Python dependencies
echo "Installing dependencies..."
cd "$PROJECT_DIR"
uv sync --quiet

# Build the MCP server config entry
MCP_ENTRY=$(cat <<EOF
{
  "command": "uv",
  "args": ["run", "--directory", "$PROJECT_DIR", "llm-router"],
  "env": {},
  "description": "Multi-LLM router — query, research, generate, analyze, code"
}
EOF
)

# Check if claude.json exists
if [ ! -f "$CLAUDE_CONFIG" ]; then
    echo "Creating $CLAUDE_CONFIG..."
    echo '{}' > "$CLAUDE_CONFIG"
fi

# Add MCP server entry using Python (safer JSON manipulation)
python3 -c "
import json, sys

config_path = '$CLAUDE_CONFIG'
with open(config_path) as f:
    config = json.load(f)

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['llm-router'] = json.loads('''$MCP_ENTRY''')

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2)

print('Added llm-router to', config_path)
"

echo ""
echo "Done! Restart Claude Code to load the LLM Router MCP server."
echo ""
echo "Available tools after restart:"
echo "  llm_query     — General questions (auto-routed)"
echo "  llm_research  — Search-augmented (Perplexity)"
echo "  llm_generate  — Content creation (Gemini/GPT)"
echo "  llm_analyze   — Deep analysis (GPT/o3)"
echo "  llm_code      — Coding tasks (GPT/Gemini)"
echo "  llm_set_profile — Switch budget/balanced/premium"
echo "  llm_usage     — View cost & token stats"
echo "  llm_health    — Provider health status"
echo ""
