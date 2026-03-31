#!/usr/bin/env bash
# start-ollama.sh — Ensure Ollama is running with the required model.
#
# Usage:
#   ./start-ollama.sh           — start Ollama + pull model if needed (silent)
#   ./start-ollama.sh --status  — print status and exit
#   ./start-ollama.sh --pull    — force pull the model even if installed
#
# Exit codes:
#   0 — Ollama is running and model is available
#   1 — Ollama failed to start or model unavailable

OLLAMA_URL="${LLM_ROUTER_OLLAMA_URL:-http://localhost:11434}"
OLLAMA_MODEL="${LLM_ROUTER_OLLAMA_MODEL:-qwen3.5:latest}"
MAX_WAIT=10  # seconds to wait for Ollama to become ready after starting

# ── Helpers ──────────────────────────────────────────────────────────────────

is_running() {
    curl -sf "${OLLAMA_URL}/api/tags" -o /dev/null 2>&1
}

has_model() {
    curl -sf "${OLLAMA_URL}/api/tags" 2>/dev/null \
        | python3 -c "
import json, sys, os
data = json.load(sys.stdin)
model = os.environ.get('OLLAMA_MODEL', '${OLLAMA_MODEL}')
base = model.split(':')[0]
names = [m.get('name','') for m in data.get('models',[])]
sys.exit(0 if any(n == model or n.startswith(base) for n in names) else 1)
" 2>/dev/null
}

# ── --status mode ─────────────────────────────────────────────────────────────

if [[ "$1" == "--status" ]]; then
    if ! is_running; then
        echo "❌ Ollama not running (${OLLAMA_URL})"
        exit 1
    fi
    if has_model; then
        echo "✅ Ollama running | model '${OLLAMA_MODEL}' ready"
        exit 0
    else
        echo "⚠️  Ollama running but '${OLLAMA_MODEL}' not installed"
        echo "   Run: ollama pull ${OLLAMA_MODEL}"
        exit 1
    fi
fi

# ── Start Ollama if not running ───────────────────────────────────────────────

if ! is_running; then
    if ! command -v ollama &>/dev/null; then
        echo "❌ Ollama not installed — install from https://ollama.com" >&2
        exit 1
    fi

    # Start in background, detached from this process
    nohup ollama serve >/dev/null 2>&1 &
    OLLAMA_PID=$!

    # Wait for Ollama to become ready
    waited=0
    while ! is_running; do
        sleep 0.5
        waited=$((waited + 1))
        if (( waited * 5 >= MAX_WAIT * 10 )); then
            echo "⚠️  Ollama started (pid ${OLLAMA_PID}) but not yet ready after ${MAX_WAIT}s" >&2
            exit 1
        fi
    done

    echo "✅ Ollama started (pid ${OLLAMA_PID})"
else
    : # already running, no output
fi

# ── Ensure model is installed ─────────────────────────────────────────────────

if ! has_model || [[ "$1" == "--pull" ]]; then
    echo "⬇️  Pulling '${OLLAMA_MODEL}'..."
    ollama pull "${OLLAMA_MODEL}"
    if [[ $? -ne 0 ]]; then
        echo "❌ Failed to pull '${OLLAMA_MODEL}'" >&2
        exit 1
    fi
    echo "✅ Model '${OLLAMA_MODEL}' ready"
fi

exit 0
