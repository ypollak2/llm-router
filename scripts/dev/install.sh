#!/usr/bin/env bash
# Install LLM Router as an MCP server in Claude Code.
# Usage: ./scripts/dev/install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
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

# Install auto-routing hook
HOOKS_DIR="$HOME/.claude/hooks"
HOOK_SRC="$PROJECT_DIR/src/llm_router/hooks/auto-route.py"
HOOK_DST="$HOOKS_DIR/llm-router-auto-route.py"

if [ -f "$HOOK_SRC" ]; then
    mkdir -p "$HOOKS_DIR"
    cp "$HOOK_SRC" "$HOOK_DST"
    chmod +x "$HOOK_DST"

    # Add UserPromptSubmit hook to global settings if not already present
    python3 -c "
import json, os

settings_path = os.path.expanduser('~/.claude/settings.json')
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault('hooks', {})
ups_hooks = hooks.setdefault('UserPromptSubmit', [])

hook_cmd = 'python3 $HOOK_DST'
already = any(
    h.get('hooks', [{}])[0].get('command', '') == hook_cmd
    for h in ups_hooks if isinstance(h, dict)
)

if not already:
    ups_hooks.append({
        'matcher': '',
        'hooks': [{'type': 'command', 'command': hook_cmd}]
    })
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
    print('Installed auto-routing hook')
else:
    print('Auto-routing hook already installed')
"
fi

# Install usage-refresh hook (PostToolUse)
USAGE_HOOK_SRC="$PROJECT_DIR/.claude/hooks/usage-refresh.py"
USAGE_HOOK_DST="$HOOKS_DIR/llm-router-usage-refresh.py"

if [ -f "$USAGE_HOOK_SRC" ]; then
    cp "$USAGE_HOOK_SRC" "$USAGE_HOOK_DST"
    chmod +x "$USAGE_HOOK_DST"

    python3 -c "
import json, os

settings_path = os.path.expanduser('~/.claude/settings.json')
if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault('hooks', {})
ptu_hooks = hooks.setdefault('PostToolUse', [])

hook_cmd = 'python3 $USAGE_HOOK_DST'
already = any(
    h.get('hooks', [{}])[0].get('command', '') == hook_cmd
    for h in ptu_hooks if isinstance(h, dict)
)

if not already:
    ptu_hooks.append({
        'matcher': 'llm_',
        'hooks': [{'type': 'command', 'command': hook_cmd}]
    })
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
    print('Installed usage-refresh hook')
else:
    print('Usage-refresh hook already installed')
"
fi

# Install routing rules globally
RULES_DIR="$HOME/.claude/rules"
RULES_SRC="$PROJECT_DIR/src/llm_router/rules/llm-router.md"
RULES_DST="$RULES_DIR/llm-router.md"

if [ -f "$RULES_SRC" ]; then
    mkdir -p "$RULES_DIR"
    cp "$RULES_SRC" "$RULES_DST"
    echo "Installed routing rules → $RULES_DST"
fi

echo ""
echo "Done! Restart Claude Code to load the LLM Router MCP server."
echo ""
# The tool list is PRINTED FROM THE SOURCE, not hardcoded.
#
# It used to be eight literal echo lines, which CHZ-SURF-01 flagged — rightly.
# Which tools exist depends on LLM_ROUTER_SLIM, so a fixed list tells a user
# on a slim tier that tools are available which were never registered, at the
# exact moment they are deciding what to try first.
#
# Falls back to a pointer rather than a stale list if the package cannot be
# imported yet: naming no tools is honest, naming the wrong ones is not.
echo "Available tools after restart:"
if ! python3 -c "
from llm_router.tool_surface import route_tool  # noqa
import llm_router.tool_surface as ts
names = getattr(ts, 'active_tool_names', None)
print('\n'.join(f'  {n}' for n in names())) if callable(names) else exit(1)
" 2>/dev/null; then
    echo "  (run \`llm-router doctor\` after restart for the active tool list)"
fi
echo ""
