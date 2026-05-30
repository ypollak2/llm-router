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

# -- Today's savings (persisted in usage.db + pending in savings_log.jsonl) --
#
# v9.4.0: Two changes from prior behaviour.
#   1. Prefer the saved_usd column (populated by cost.py v9.4.0+ with the
#      complexity-aware baseline). Fall back to the legacy Opus-token math
#      for older rows where saved_usd is still 0.0.
#   2. Add un-flushed savings from savings_log.jsonl. auto-route's DIRECT
#      execution appends a JSONL record per successful routing; those records
#      only land in the usage/savings_stats tables when the session ends.
#      Without this, a session driven entirely by DIRECT routing displayed
#      $0.00 saved live, even with real savings accumulating.
today_saved=0
if [ -f "$USAGE_DB" ]; then
    today_start=$(date -u +"%Y-%m-%d 00:00:00")
    persisted=$(sqlite3 "$USAGE_DB" "
        SELECT COALESCE(SUM(
            CASE
                WHEN COALESCE(saved_usd, 0) > 0 THEN saved_usd
                WHEN provider IN ('ollama','codex','gemini_cli')
                    THEN (COALESCE(input_tokens,0)*15.0 + COALESCE(output_tokens,0)*75.0)/1000000.0
                ELSE 0
            END
        ), 0)
        FROM usage
        WHERE timestamp >= '$today_start' AND success=1;
    " 2>/dev/null)
    if [ -n "$persisted" ]; then
        today_saved=$persisted
    fi
fi

SAVINGS_LOG="$STATE_DIR/savings_log.jsonl"
if [ -f "$SAVINGS_LOG" ]; then
    pending=$(python3 -c "
import json, datetime
today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
total = 0.0
try:
    with open('$SAVINGS_LOG') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = rec.get('timestamp', '')
                if ts.startswith(today):
                    total += float(rec.get('estimated_saved', 0))
            except Exception:
                pass
except OSError:
    pass
print(f'{total:.6f}')
" 2>/dev/null)
    if [ -n "$pending" ]; then
        today_saved=$(python3 -c "print(float('$today_saved') + float('$pending'))" 2>/dev/null)
    fi
fi

if [ -n "$today_saved" ] && [ "$today_saved" != "0" ] && [ "$today_saved" != "0.0" ]; then
    formatted=$(printf '%.2f' "$today_saved" 2>/dev/null)
    if [ "$formatted" != "0.00" ]; then
        parts+=("\$${formatted} saved")
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
