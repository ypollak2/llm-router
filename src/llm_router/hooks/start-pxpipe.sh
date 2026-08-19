#!/usr/bin/env bash
# start-pxpipe.sh — Ensure a local pxpipe proxy is running for heavy-model
# context compression (https://github.com/teamchong/pxpipe). Mirrors
# start-ollama.sh's start/detach/poll pattern.
#
# Usage:
#   ./start-pxpipe.sh           — start pxpipe if needed (silent if already up)
#   ./start-pxpipe.sh --status  — print status and exit
#
# Exit codes:
#   0 — pxpipe is running
#   1 — pxpipe failed to start or npx unavailable

PXPIPE_URL="${LLM_ROUTER_PXPIPE_URL:-http://127.0.0.1:47821}"
PXPIPE_MODELS_ENV="${LLM_ROUTER_PXPIPE_HEAVY_MODELS:-claude-fable-5}"
MAX_WAIT=10  # seconds to wait for pxpipe to become ready after starting

# ── Helpers ──────────────────────────────────────────────────────────────────

is_running() {
    curl -sf "${PXPIPE_URL}" -o /dev/null 2>&1
}

# ── --status mode ─────────────────────────────────────────────────────────────

if [[ "$1" == "--status" ]]; then
    if is_running; then
        echo "✅ pxpipe running (${PXPIPE_URL})"
        exit 0
    else
        echo "❌ pxpipe not running (${PXPIPE_URL})"
        exit 1
    fi
fi

# ── Start pxpipe if not running ───────────────────────────────────────────────

if ! is_running; then
    if ! command -v npx &>/dev/null; then
        echo "❌ npx not found — install Node.js to use pxpipe (https://nodejs.org)" >&2
        exit 1
    fi

    # Start in background, detached from this process. PXPIPE_MODELS controls
    # which models pxpipe itself compresses — pass through LLM Router's own
    # heavy-model config so the two stay consistent.
    PXPIPE_MODELS="${PXPIPE_MODELS_ENV}" nohup npx --yes pxpipe-proxy >/dev/null 2>&1 &
    PXPIPE_PID=$!

    # Wait for pxpipe to become ready
    waited=0
    while ! is_running; do
        sleep 0.5
        waited=$((waited + 1))
        if (( waited * 5 >= MAX_WAIT * 10 )); then
            echo "⚠️  pxpipe started (pid ${PXPIPE_PID}) but not yet ready after ${MAX_WAIT}s" >&2
            exit 1
        fi
    done

    echo "✅ pxpipe started (pid ${PXPIPE_PID})"
else
    : # already running, no output
fi

exit 0
