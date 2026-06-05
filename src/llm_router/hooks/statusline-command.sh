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

parts=()

# -- Claude subscription usage --
if [ -f "$USAGE_JSON" ]; then
    session_pct=$(python3 -c "import json; d=json.load(open('$USAGE_JSON')); print(f\"{d.get('session_pct',0):.0f}\")" 2>/dev/null)
    weekly_pct=$(python3 -c "import json; d=json.load(open('$USAGE_JSON')); print(f\"{d.get('weekly_pct',0):.0f}\")" 2>/dev/null)
    if [ -n "$session_pct" ] && [ "$session_pct" != "0" ]; then
        parts+=("CC ${session_pct}%s ${weekly_pct}%w")
    fi
fi

# -- Today's gross savings --
#
# v10.1.3: Read from v9.3 per-platform tables (claude_usage, codex_usage,
# gemini_usage) in addition to the legacy `usage` table. Newer routing
# decisions persist to the per-platform tables, not `usage`. Pre-v10.1.3 the
# statusline missed them and reported $0 on days with real savings.
#
# Schema notes:
#   - legacy `usage` table:  saved_usd column, success=1 filter
#   - per-platform tables:   cost_saved_usd column, no success filter
#   - savings_log.jsonl:     un-flushed DIRECT routings (live session)
today_saved=0
if [ -f "$USAGE_DB" ]; then
    today_start=$(date -u +"%Y-%m-%d 00:00:00")

    # Legacy `usage` table (kept for backward compat with older sessions).
    legacy=$(sqlite3 "$USAGE_DB" "
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

    # v9.3 per-platform tables. Each query is guarded so a missing table
    # (older DBs) produces 0 without aborting the others.
    platform_sum=0
    for table in claude_usage codex_usage gemini_usage; do
        val=$(sqlite3 "$USAGE_DB" "
            SELECT COALESCE(SUM(cost_saved_usd), 0)
            FROM $table
            WHERE date(timestamp,'localtime')=date('now','localtime');
        " 2>/dev/null)
        if [ -n "$val" ]; then
            platform_sum=$(python3 -c "print(float('$platform_sum') + float('$val'))" 2>/dev/null)
        fi
    done

    today_saved=$(python3 -c "print(float('${legacy:-0}') + float('${platform_sum:-0}'))" 2>/dev/null)
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
#
# v10.1.3: Routes are persisted per-session as `last_route_<session_id>.json`
# with keys `tool`, `task_type`, `complexity`, `saved_at` (unix timestamp).
# Find the newest by mtime and show it if within 5 minutes.
last=$(python3 -c "
import json, glob, os, time
files = glob.glob(os.path.expanduser('$STATE_DIR/last_route_*.json'))
if files:
    newest = max(files, key=os.path.getmtime)
    try:
        d = json.load(open(newest))
        age = time.time() - d.get('saved_at', 0)
        if age < 300:
            tool = d.get('tool', '?').replace('llm_', '')
            task = d.get('task_type', tool)
            print(f'{task}>{tool}' if task != tool else tool)
    except Exception:
        pass
" 2>/dev/null)
if [ -n "$last" ]; then
    parts+=("$last")
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
