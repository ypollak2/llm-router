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
# Set only when the operator names a model. An unset value means "whatever is
# already installed" — this script must never download a model of its own
# choosing. It used to default to qwen3.5:latest and `ollama pull` it, which on
# 2026-09-14 silently re-downloaded 17GB of a model the user had removed, put it
# back at the head of the routing chain, and contaminated a benchmark in flight.
OLLAMA_MODEL="${LLM_ROUTER_OLLAMA_MODEL:-}"
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

installed_models() {
    curl -sf "${OLLAMA_URL}/api/tags" 2>/dev/null \
        | python3 -c "
import json, sys
skip = ('embed', 'bge-', 'gte-', 'e5-', 'all-minilm')
for m in json.load(sys.stdin).get('models', []):
    name = m.get('name', '')
    if name and not any(s in name for s in skip):
        print(name)
" 2>/dev/null
}

# ── --status mode ─────────────────────────────────────────────────────────────

if [[ "$1" == "--status" ]]; then
    if ! is_running; then
        echo "❌ Ollama not running (${OLLAMA_URL})"
        exit 1
    fi
    if [[ -z "${OLLAMA_MODEL}" ]]; then
        found=$(installed_models | paste -sd, -)
        if [[ -n "${found}" ]]; then
            echo "✅ Ollama running | installed: ${found}"
            exit 0
        fi
        echo "⚠️  Ollama running but no completion models installed"
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

    # Tuning, measured rather than guessed, and set HERE because it otherwise
    # lives only in whichever shell happened to start the server and evaporates
    # on the next restart.
    #
    # KEEP_ALIVE: how long a 17-21GB model stays resident. Measured on 765 real
    # gaps between this user's prompts across 6 sessions (p50 6.4 min, p90 47
    # min):
    #
    #     keep_alive   prompts finding a warm model   reload cost /100 prompts
    #        5 min                44%                        225s
    #       15 min                68%                        127s
    #       30 min                84%                         65s
    #
    # No knee; it is a straight trade. 30 min buys 0.6s per prompt over 15 min and
    # holds 17GB a third longer — and at 30 min a background task on this 48GB
    # machine was killed for memory. 15 min takes two thirds of the benefit at
    # meaningfully less pressure. Override with LLM_ROUTER_OLLAMA_KEEP_ALIVE.
    #
    # MAX_LOADED_MODELS=2 is aspirational on this hardware: the server reports
    # 38,337 MiB free and the two models need ~42GB together, so it holds one.
    # Harmless, and correct on a larger machine.
    export OLLAMA_KEEP_ALIVE="${LLM_ROUTER_OLLAMA_KEEP_ALIVE:-15m}"
    export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-2}"
    # 32768 was ~70x the real payload (measured worst case ~457 tokens) and cost
    # 10GB of KV cache per model.
    export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-8192}"

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

# Pull ONLY a model the operator asked for by name, or one explicitly requested
# with --pull. With no model named, having any completion model installed is
# success; having none is a message, never a multi-gigabyte download.
if [[ -z "${OLLAMA_MODEL}" ]]; then
    if installed_models | grep -q .; then
        exit 0
    fi
    echo "⚠️  Ollama is running but no models are installed." >&2
    echo "   Pull one you want, e.g.: ollama pull qwen3-coder:30b" >&2
    echo "   Or set LLM_ROUTER_OLLAMA_MODEL=<model> to have this script pull it." >&2
    exit 1
fi

if ! has_model || [[ "$1" == "--pull" ]]; then
    echo "⬇️  Pulling '${OLLAMA_MODEL}' (named by LLM_ROUTER_OLLAMA_MODEL)..."
    if ! ollama pull "${OLLAMA_MODEL}"; then
        echo "❌ Failed to pull '${OLLAMA_MODEL}'" >&2
        exit 1
    fi
    echo "✅ Model '${OLLAMA_MODEL}' ready"
fi

exit 0
