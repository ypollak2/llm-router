#!/bin/bash
# Claude Code statusline — llm-router routing indicators
#
# Layout: 🤖 CC quota · ⏰ reset · 📂 cwd · 🧠 ctx [bar] · 💰 saved · 🛡 mode · 🔀 last
#
# v10.1.5: Catppuccin Mocha palette + emoji icons + context bar, inspired by
# AwesomeJun/CC-statusline. Truecolor (24-bit) ANSI — falls back gracefully
# on terminals that strip escapes, since segment text is still readable.
#
# IMPORTANT: Must consume stdin — Claude Code pipes session JSON here.
# Without reading it, the pipe blocks and Claude Code times out.

input=$(cat)
session_cwd=$(printf '%s' "$input" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('cwd', ''))
except Exception:
    pass
" 2>/dev/null)
transcript_path=$(printf '%s' "$input" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('transcript_path', ''))
except Exception:
    pass
" 2>/dev/null)
model_id=$(printf '%s' "$input" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    m = d.get('model')
    if isinstance(m, dict):
        print(m.get('id', ''))
    elif isinstance(m, str):
        print(m)
except Exception:
    pass
" 2>/dev/null)

STATE_DIR="$HOME/.llm-router"
USAGE_JSON="$STATE_DIR/usage.json"
USAGE_DB="$STATE_DIR/usage.db"

# ── Catppuccin Mocha palette (truecolor ANSI) ────────────────────────────────
ESC=$'\033'
_RESET="${ESC}[0m"
_BOLD="${ESC}[1m"
_DIM="${ESC}[38;2;108;112;134m"      # surface2
_TEXT="${ESC}[38;2;205;214;244m"     # text
_MAUVE="${ESC}[38;2;203;166;247m"
_BLUE="${ESC}[38;2;137;180;250m"
_GREEN="${ESC}[38;2;166;227;161m"
_YELLOW="${ESC}[38;2;249;226;175m"
_PEACH="${ESC}[38;2;250;179;135m"
_PINK="${ESC}[38;2;245;194;231m"
_RED="${ESC}[38;2;243;139;168m"
_SKY="${ESC}[38;2;137;220;235m"
_LAV="${ESC}[38;2;180;190;254m"

# Suppress colors if NO_COLOR is set or stdout is not a TTY-friendly target.
if [ "${NO_COLOR:-}" != "" ]; then
    _RESET="" _BOLD="" _DIM="" _TEXT=""
    _MAUVE="" _BLUE="" _GREEN="" _YELLOW="" _PEACH=""
    _PINK="" _RED="" _SKY="" _LAV=""
fi

# Pick color by 0–100 percentage threshold (green→yellow→red).
_pct_color() {
    local pct=$1
    if [ "$pct" -ge 80 ]; then printf '%s' "$_RED"
    elif [ "$pct" -ge 50 ]; then printf '%s' "$_YELLOW"
    else printf '%s' "$_GREEN"
    fi
}

# Render a fixed-width progress bar with intensity color.
_bar() {
    local pct=$1 width=${2:-10}
    [ "$pct" -lt 0 ] && pct=0
    [ "$pct" -gt 100 ] && pct=100
    local filled=$(( pct * width / 100 ))
    local empty=$(( width - filled ))
    local color
    color=$(_pct_color "$pct")
    local bar=""
    local i=0
    while [ $i -lt $filled ]; do bar+="█"; i=$((i+1)); done
    i=0
    while [ $i -lt $empty ]; do bar+="░"; i=$((i+1)); done
    printf '%s%s%s%s░%s' "$color" "$bar" "$_DIM" "" "$_RESET" >/dev/null
    printf '%s%s%s' "$color" "$bar" "$_RESET"
}

# Determine context cap from model id (suffix `[1m]` ⇒ 1_000_000, else 200_000).
CONTEXT_LIMIT="${CC_CONTEXT_LIMIT:-200000}"
case "$model_id" in
    *\[1m\]*|*1m*) CONTEXT_LIMIT=1000000 ;;
esac

parts=()

# ── 🤖 Claude subscription usage ─────────────────────────────────────────────
if [ -f "$USAGE_JSON" ]; then
    session_pct=$(python3 -c "import json; d=json.load(open('$USAGE_JSON')); print(f\"{d.get('session_pct',0):.0f}\")" 2>/dev/null)
    weekly_pct=$(python3 -c "import json; d=json.load(open('$USAGE_JSON')); print(f\"{d.get('weekly_pct',0):.0f}\")" 2>/dev/null)
    if [ -n "$session_pct" ] && [ "$session_pct" != "0" ]; then
        s_color=$(_pct_color "$session_pct")
        w_color=$(_pct_color "$weekly_pct")
        parts+=("🤖 ${s_color}${session_pct}%${_RESET}${_DIM}/5h${_RESET} ${w_color}${weekly_pct}%${_RESET}${_DIM}/wk${_RESET}")
    fi
fi

# ── ⏰ Quota reset time ──────────────────────────────────────────────────────
if [ -f "$USAGE_JSON" ]; then
    reset_str=$(python3 -c "
import json, datetime
try:
    d = json.load(open('$USAGE_JSON'))
    raw = d.get('session_resets_at', '')
    if not raw:
        raise ValueError
    raw = raw.replace('Z', '+00:00')
    dt = datetime.datetime.fromisoformat(raw).astimezone()
    if dt < datetime.datetime.now(datetime.timezone.utc).astimezone():
        raise ValueError
    print(dt.strftime('%-I:%M%p').lower())
except Exception:
    pass
" 2>/dev/null)
    if [ -n "$reset_str" ]; then
        parts+=("⏰ ${_YELLOW}${reset_str}${_RESET}")
    fi
fi

# ── 📂 Working directory ─────────────────────────────────────────────────────
if [ -n "$session_cwd" ]; then
    dir_name=$(basename "$session_cwd")
    if [ -n "$dir_name" ] && [ "$dir_name" != "/" ]; then
        parts+=("📂 ${_BLUE}${dir_name}${_RESET}")
    fi
fi

# ── 🧠 Context tokens (with progress bar) ────────────────────────────────────
if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
    ctx_total=$(python3 -c "
import json
total = None
try:
    with open('$transcript_path') as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get('message')
            if not isinstance(msg, dict):
                continue
            u = msg.get('usage')
            if not isinstance(u, dict):
                continue
            tokens = (
                u.get('input_tokens', 0)
                + u.get('cache_creation_input_tokens', 0)
                + u.get('cache_read_input_tokens', 0)
            )
            if tokens > 0:
                total = tokens
    print(total if total is not None else 0)
except Exception:
    print(0)
" 2>/dev/null)
    if [ -n "$ctx_total" ] && [ "$ctx_total" != "0" ]; then
        ctx_pct=$(( ctx_total * 100 / CONTEXT_LIMIT ))
        [ "$ctx_pct" -gt 100 ] && ctx_pct=100
        ctx_human=$(python3 -c "
n=$ctx_total
if n >= 1_000_000: print(f'{n/1_000_000:.1f}M')
elif n >= 1_000:   print(f'{n/1_000:.1f}k')
else:              print(str(n))
" 2>/dev/null)
        ctx_bar=$(_bar "$ctx_pct" 8)
        parts+=("🧠 ${_PINK}${ctx_human}${_RESET} ${ctx_bar} ${_DIM}${ctx_pct}%${_RESET}")
    fi
fi

# ── 💰 Today's gross savings ─────────────────────────────────────────────────
today_saved=0
if [ -f "$USAGE_DB" ]; then
    today_start=$(date -u +"%Y-%m-%d 00:00:00")
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
        parts+=("💰 ${_GREEN}\$${formatted}${_RESET}")
    fi
fi

# ── 🛡 Enforce mode ──────────────────────────────────────────────────────────
enforce="${LLM_ROUTER_ENFORCE:-smart}"
case "$enforce" in
    hard|on) parts+=("🛡  ${_RED}enforce${_RESET}") ;;
    soft)    parts+=("🛡  ${_YELLOW}suggest${_RESET}") ;;
    off)     parts+=("🛡  ${_DIM}shadow${_RESET}") ;;
    smart)   parts+=("🛡  ${_SKY}smart${_RESET}") ;;
esac

# ── 🔀 Last route (if recent) ────────────────────────────────────────────────
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
    parts+=("🔀 ${_MAUVE}${last}${_RESET}")
fi

# ── Assemble with dim middle-dot separators ──────────────────────────────────
sep=" ${_DIM}·${_RESET} "
result=""
for i in "${!parts[@]}"; do
    if [ $i -gt 0 ]; then
        result+="$sep"
    fi
    result+="${parts[$i]}"
done

printf '%s\n' "$result"
