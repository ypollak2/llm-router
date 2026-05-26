#!/bin/bash
# Claude Code statusline — llm-router routing indicators
# Shows: CC usage | savings | enforce mode | last route
#
# IMPORTANT: Must consume stdin — Claude Code pipes session JSON here.
# Without reading it, the pipe blocks and Claude Code times out.

# Consume stdin (Claude Code pipes session JSON)
input=$(cat)

STATE_DIR="$HOME/.llm-router"
USAGE_JSON="$STATE_DIR/usage.json"
USAGE_DB="$STATE_DIR/usage.db"
LAST_ROUTE_FILE="$STATE_DIR/last_route.json"

parts=()

# -- Claude subscription usage --
if [ -f "$USAGE_JSON" ]; then
    session_pct=$(python3 -c "import json; d=json.load(open('$USAGE_JSON')); print(f\"{d.get('session_pct',0):.0f}\")" 2>/dev/null)
    weekly_pct=$(python3 -c "import json; d=json.load(open('$USAGE_JSON')); print(f\"{d.get('weekly_pct',0):.0f}\")" 2>/dev/null)
    if [ -n "$session_pct" ] && [ "$session_pct" != "0" ]; then
        parts+=("CC ${session_pct}%s ${weekly_pct}%w")
    fi
fi

# -- Today's savings from DB --
if [ -f "$USAGE_DB" ]; then
    today_start=$(date -u +"%Y-%m-%d 00:00:00")
    saved=$(sqlite3 "$USAGE_DB" "SELECT COALESCE(SUM(CASE WHEN provider IN ('ollama','codex','gemini_cli') THEN (COALESCE(input_tokens,0)*15.0 + COALESCE(output_tokens,0)*75.0)/1000000.0 ELSE 0 END), 0) FROM usage WHERE timestamp >= '$today_start' AND success=1;" 2>/dev/null)
    if [ -n "$saved" ] && [ "$saved" != "0" ] && [ "$saved" != "0.0" ]; then
        parts+=("\$$(printf '%.2f' "$saved") saved")
    fi
fi

# -- Enforce mode --
enforce="${LLM_ROUTER_ENFORCE:-smart}"
case "$enforce" in
    hard|on) parts+=("enforce") ;;
    soft)    parts+=("suggest") ;;
    off)     parts+=("shadow") ;;
    smart)   parts+=("smart") ;;
esac

# -- Last route (if recent) --
if [ -f "$LAST_ROUTE_FILE" ]; then
    last=$(python3 -c "
import json, time
d = json.load(open('$LAST_ROUTE_FILE'))
age = time.time() - d.get('timestamp', 0)
if age < 300:
    model = d.get('model', '?')
    task = d.get('task_type', '?')
    print(f'{task}>{model}')
" 2>/dev/null)
    if [ -n "$last" ]; then
        parts+=("$last")
    fi
fi

# -- Assemble with separators --
result=""
for i in "${!parts[@]}"; do
    if [ $i -gt 0 ]; then
        result+=" | "
    fi
    result+="${parts[$i]}"
done

echo "$result"
