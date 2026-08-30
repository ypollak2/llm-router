#!/bin/bash
# Claude Code statusline — llm_router routing indicators
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
# GH#50: read four times below (health check + last-route token suffix) and
# never assigned, so every read expanded to "" and open("") threw into a
# swallowed except — the health indicator reported 'no provider' forever on
# any setup without a cloud key. Written by hooks/savings_logger.py.
SAVINGS_LOG="$STATE_DIR/savings_log.jsonl"

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
# Live updates: fire a background refresh when usage.json gets older than
# LLM_ROUTER_USAGE_TTL_SEC seconds (default 300 = 5 minutes). The statusline
# renders whatever's currently on disk; the next render after the
# background refresh completes picks up fresh percentages without
# blocking the current draw.
#
# The refresh script (llm_router-usage-refresh.py) talks to claude.ai via
# AppleScript / Playwright; we fire it nohup'd + stdout/stderr suppressed
# so a refresh failure can't bleed into the statusline output.
LLM_ROUTER_USAGE_TTL_SEC="${LLM_ROUTER_USAGE_TTL_SEC:-300}"
REFRESH_SCRIPT="$HOME/.claude/hooks/llm_router-usage-refresh.py"
if [ -f "$USAGE_JSON" ] && [ -x "$REFRESH_SCRIPT" ]; then
    file_age_s=$(CHZ_USAGE_JSON="$USAGE_JSON" python3 -c '
import json, time, os
try:
    d = json.load(open(os.environ["CHZ_USAGE_JSON"]))
    print(int(time.time() - d.get("updated_at", 0)))
except Exception:
    print(99999)
' 2>/dev/null)
    if [ -n "$file_age_s" ] && [ "$file_age_s" -gt "$LLM_ROUTER_USAGE_TTL_SEC" ]; then
        # Background refresh — fire & forget; statusline keeps drawing.
        #
        # Stampede guard via a timestamp file, NOT flock: flock is a Linux-only
        # util and is absent on macOS, where `flock -n 9 || exit 0` failed with
        # "command not found" and silently aborted EVERY refresh — so the quota
        # never updated. A portable "last attempt" throttle launches at most one
        # refresh per LLM_ROUTER_REFRESH_THROTTLE_SEC (default 60s) on any OS.
        LAST_TRY="$STATE_DIR/.usage-refresh.last"
        throttle="${LLM_ROUTER_REFRESH_THROTTLE_SEC:-60}"
        do_refresh=1
        if [ -f "$LAST_TRY" ]; then
            try_age=$(CHZ_LAST_TRY="$LAST_TRY" python3 -c 'import os,time;print(int(time.time()-os.path.getmtime(os.environ["CHZ_LAST_TRY"])))' 2>/dev/null)
            [ -n "$try_age" ] && [ "$try_age" -lt "$throttle" ] && do_refresh=0
        fi
        if [ "$do_refresh" = "1" ]; then
            : > "$LAST_TRY" 2>/dev/null
            ( "$REFRESH_SCRIPT" </dev/null >/dev/null 2>&1 & ) >/dev/null 2>&1 &
            disown 2>/dev/null || true
        fi
    fi
fi

if [ -f "$USAGE_JSON" ]; then
    session_pct=$(CHZ_USAGE_JSON="$USAGE_JSON" python3 -c 'import json,os; d=json.load(open(os.environ["CHZ_USAGE_JSON"])); print("%.0f" % d.get("session_pct",0))' 2>/dev/null)
    weekly_pct=$(CHZ_USAGE_JSON="$USAGE_JSON" python3 -c 'import json,os; d=json.load(open(os.environ["CHZ_USAGE_JSON"])); print("%.0f" % d.get("weekly_pct",0))' 2>/dev/null)
    if [ -n "$session_pct" ]; then
        s_color=$(_pct_color "$session_pct")
        w_color=$(_pct_color "$weekly_pct")
        # Append a ° marker when the displayed numbers are stale beyond
        # the TTL — gives the user a visual cue that a refresh is in
        # flight (or that the refresh chain is broken).
        stale_marker=""
        if [ -n "$file_age_s" ] && [ "$file_age_s" -gt "$LLM_ROUTER_USAGE_TTL_SEC" ]; then
            stale_marker="${_DIM}°${_RESET}"
        fi
        parts+=("🤖 ${s_color}${session_pct}%${_RESET}${_DIM}/5h${_RESET} ${w_color}${weekly_pct}%${_RESET}${_DIM}/wk${_RESET}${stale_marker}")
    fi
fi

# ── ⏰ Quota reset time ──────────────────────────────────────────────────────
if [ -f "$USAGE_JSON" ]; then
    reset_str=$(CHZ_USAGE_JSON="$USAGE_JSON" python3 -c '
import json, datetime, os
try:
    d = json.load(open(os.environ["CHZ_USAGE_JSON"]))
    raw = d.get("session_resets_at", "")
    if not raw:
        raise ValueError
    raw = raw.replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(raw).astimezone()
    if dt < datetime.datetime.now(datetime.timezone.utc).astimezone():
        raise ValueError
    print(dt.strftime("%-I:%M%p").lower())
except Exception:
    pass
' 2>/dev/null)
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
    ctx_total=$(CHZ_TRANSCRIPT="$transcript_path" python3 -c '
import json, os
total = None
try:
    with open(os.environ["CHZ_TRANSCRIPT"]) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            tokens = (
                u.get("input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
            )
            if tokens > 0:
                total = tokens
    print(total if total is not None else 0)
except Exception:
    print(0)
' 2>/dev/null)
    if [ -n "$ctx_total" ] && [ "$ctx_total" != "0" ]; then
        ctx_pct=$(( ctx_total * 100 / CONTEXT_LIMIT ))
        [ "$ctx_pct" -gt 100 ] && ctx_pct=100
        ctx_human=$(CHZ_CTX_TOTAL="$ctx_total" python3 -c '
import os
try:
    n = int(os.environ["CHZ_CTX_TOTAL"])
except (KeyError, ValueError):
    n = 0
if n >= 1_000_000: print("%.1fM" % (n/1_000_000))
elif n >= 1_000:   print("%.1fk" % (n/1_000))
else:              print(str(n))
' 2>/dev/null)
        ctx_bar=$(_bar "$ctx_pct" 8)
        parts+=("🧠 ${_PINK}${ctx_human}${_RESET} ${ctx_bar} ${_DIM}${ctx_pct}%${_RESET}")
    fi
fi

# (The hand-rolled `today_saved` computation that lived here — ~80 lines of
#  SQL over `usage` plus a JSONL fallback — was removed, not just unhooked.
#  Leaving it would have left a second savings computation in the file that
#  nothing rendered: dead code that still looks authoritative to the next
#  reader, and that the INV-COST-004 guard would keep failing on.)

# ── 💰 Today's savings, via the CANONICAL aggregation ────────────────────────
#
# INV-COST-004: "the aggregation functions are the ONLY cost totals; surfaces
# delegate." This surface did not delegate — it ran its own SQL over the legacy
# `usage` table and reported the result as the day's total.
#
# That under-reports, and dashboard_data.py says why in its own docstring:
# "Every consumer that wants to show today's calls / tokens / savings must UNION
# across all sources or under-report." It unions five tables — claude_usage,
# codex_usage, gemini_usage, legacy usage, and savings_stats.
#
# Measured on one day:
#     usage alone            840 rows    $78.68
#     savings_stats alone  1,109 rows   $102.88
#     query_window (union) 2,215 rows   $205.19   <- the total
#
# The surfaces were not disagreeing about arithmetic. Each queried a SUBSET and
# presented it as the whole, which is why three renderers showed three numbers
# and no reader could tell which was right.
#
# LABELLED "today", because the previous bare `$102.31` sat beside a quota
# percentage and read as SPEND — the opposite of its meaning.
#
# 57ms measured, which is why it is acceptable to call from a statusline at all;
# if that regresses, drop the figure rather than caching a stale one.
if [ -f "$USAGE_DB" ]; then
    # A python that can import llm_router. The statusline runs under whatever
    # `python3` the shell finds, which on a normal install is NOT the venv the
    # package lives in — the first version of this silently produced nothing for
    # exactly that reason, and the "never break the statusline" fallback hid it.
    # Resolution order: the interpreter behind the installed CLI, then the
    # AMBIENT python3/python, then pipx's venv, then a dev checkout. Each
    # candidate is probed with `import llm_router` — presence on PATH is not
    # evidence it can import the package.
    #
    # The ambient entries were added after G-D failed: inside a wheel venv the
    # package IS importable but `llm_router` is not on PATH, so a resolver that only
    # looked for the CLI found nothing and silently dropped the figure. The
    # narrow version passed locally, where a dev checkout always matched.
    #
    # If none can import llm_router the figure is omitted rather than guessed.
    _chz_py=""
    for _cand in \
        "$(command -v llm_router 2>/dev/null | xargs -I{} head -1 {} 2>/dev/null | sed 's|^#!||' | awk '{print $1}')" \
        "$(command -v python3 2>/dev/null)" \
        "$(command -v python 2>/dev/null)" \
        "$HOME/.local/pipx/venvs/llm-routing/bin/python" \
        "$HOME/.local/bin/python3" \
        "$(dirname "$0")/../../../.venv/bin/python3"; do
        if [ -n "$_cand" ] && [ -x "$_cand" ] && "$_cand" -c "import llm_router" 2>/dev/null; then
            _chz_py="$_cand"; break
        fi
    done

    _saved=""
    [ -n "$_chz_py" ] && _saved=$(CHZ_DB="$USAGE_DB" "$_chz_py" -c '
import os, pathlib
try:
    from llm_router.dashboard_data import query_window
    t = query_window("today", db_path=pathlib.Path(os.environ["CHZ_DB"]))
    print(f"{t.saved_usd:.2f}")
except Exception:
    print("")            # never break the statusline over a reporting figure
' 2>/dev/null)
    if [ -n "$_saved" ] && [ "$_saved" != "0.00" ]; then
        parts+=("💰 ${_GREEN}\$${_saved}${_RESET}${_DIM} today${_RESET}")
    fi

    # ⚖ route mix — local vs paid over the last 6h. Answers "is routing working
    # right now", which quota does not: quota says what is left, this says
    # whether it is being earned. Green only when local carries the majority.
    _mix=$(sqlite3 "$USAGE_DB" "
        SELECT
          SUM(CASE WHEN model LIKE 'ollama/%' THEN 1 ELSE 0 END),
          SUM(CASE WHEN model NOT LIKE 'ollama/%' THEN 1 ELSE 0 END)
        FROM usage
        WHERE timestamp >= datetime('now', '-6 hours');" 2>/dev/null)
    _local=$(echo "$_mix" | cut -d'|' -f1)
    _paid=$(echo "$_mix" | cut -d'|' -f2)
    if [ -n "$_local" ] && [ $(( ${_local:-0} + ${_paid:-0} )) -gt 0 ]; then
        if [ "${_local:-0}" -ge "${_paid:-0}" ]; then _mixc="$_GREEN"; else _mixc="$_YELLOW"; fi
        parts+=("⚖ ${_mixc}${_local:-0}L/${_paid:-0}P${_RESET}")
    fi
fi

# ── 🛡 Enforce mode ──────────────────────────────────────────────────────────
enforce="${LLM_ROUTER_ENFORCE:-smart}"
case "$enforce" in
    hard|on)        parts+=("🛡  ${_RED}enforce${_RESET}") ;;
    soft|suggest)   parts+=("🛡  ${_YELLOW}suggest${_RESET}") ;;
    off|observe|shadow) parts+=("🛡  ${_DIM}shadow${_RESET}") ;;
    smart|advise)   parts+=("🛡  ${_SKY}smart${_RESET}") ;;
esac

# ── ❤ Health (mirrors llm_router.observability.surface_status, dependency-free) ────────────────
# ok ✓      : a provider (cloud key, Claude subscription, or recently-active
#             Ollama) is configured, and usage data is fresh.
# degraded ⚠: as ok, but usage data is stale (>30 min).
# idle ○    : no cloud key/subscription and no Ollama activity in the last 30
#             min, but a cheap reachability probe confirms Ollama is up —
#             quiet, not broken (GH#63).
# down ✗    : no cloud key/subscription configured AND Ollama is unreachable —
#             the ONLY combination that earns the outage glyph (GH#63).
health=$(CHZ_SAVINGS_LOG="$SAVINGS_LOG" CHZ_USAGE_JSON="$USAGE_JSON" CHZ_OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}" python3 -c '
import json, os, time
now = time.time()

# GH#63: match install_hooks.check_api_keys() truthy parsing EXACTLY. A second,
# divergent parser of LLM_ROUTER_CLAUDE_SUBSCRIPTION is how this class of bug
# recurs — "1"/"true"/"yes", case-insensitive, same as doctor reports.
keys = ("ANTHROPIC_API_KEY","OPENAI_API_KEY","GEMINI_API_KEY","DEEPSEEK_API_KEY","GROQ_API_KEY")
subscription_on = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION","").lower() in ("1","true","yes")
providers = any(os.environ.get(k) for k in keys) or subscription_on

# Recent Ollama activity is an ACTIVITY signal, kept separate from "providers"
# so it cannot stand in for "a provider is configured" (GH#63 root cause #1)
# while still counting as live evidence routing is working (GH#63 root cause #2
# is handled below: its ABSENCE no longer means "down" by itself).
ollama_recent = False
try:
    from datetime import datetime
    for line in reversed(open(os.environ["CHZ_SAVINGS_LOG"]).readlines()[-200:]):
        r = json.loads(line)
        m = r.get("model","")
        if isinstance(m,str) and m.startswith("ollama/"):
            ts = datetime.fromisoformat(r["timestamp"]).timestamp()
            if now - ts <= 1800:
                ollama_recent = True; break
except Exception:
    pass

try:
    stale = (now - os.path.getmtime(os.environ["CHZ_USAGE_JSON"])) > 1800
except OSError:
    stale = True

if providers or ollama_recent:
    print("degraded" if stale else "ok")
else:
    # Nothing configured, no recent local activity. This script runs on every
    # render (GH#50 history), so the probe must be cheap and MUST NOT raise:
    # short timeout, any failure at all (network, DNS, missing stdlib bits)
    # just means "treat as unreachable" — never propagate.
    reachable = False
    try:
        import urllib.request
        url = os.environ.get("CHZ_OLLAMA_URL","http://localhost:11434").rstrip("/") + "/api/tags"
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=0.3):
            reachable = True
    except Exception:
        reachable = False
    print("idle" if reachable else "down")
' 2>/dev/null)
# A glyph with no noun is not actionable. `✗` now means "no provider keys/
# subscription in env AND Ollama unreachable" — a real fault, distinct from
# "○ idle" (Ollama reachable, just hasn't run recently) — the same defect as
# the unlabelled money figure it sits beside.
case "$health" in
    ok)       parts+=("${_GREEN}✓${_RESET}") ;;
    degraded) parts+=("${_YELLOW}⚠ stale${_RESET}") ;;
    idle)     parts+=("${_DIM}○ idle${_RESET}") ;;
    down)     parts+=("${_RED}✗ no provider${_RESET}") ;;
esac

# ── 🔀 Last route (always shown) ─────────────────────────────────────────────
# Persistent: always render the most recent route. A dim ° marker is appended
# when the route is older than 5 min, matching the quota segment's stale cue.
# Output format from python: "<route>\t<stale>" where stale is "1" or "".
last_raw=$(CHZ_STATE_DIR="$STATE_DIR" python3 -c '
import json, glob, os, time
files = glob.glob(os.path.join(os.environ["CHZ_STATE_DIR"], "last_route_*.json"))
if files:
    newest = max(files, key=os.path.getmtime)
    try:
        d = json.load(open(newest))
        tool = d.get("tool", "?").replace("llm_", "")
        task = d.get("task_type", tool)
        route = (task + ">" + tool) if task != tool else tool
        stale = "1" if (time.time() - d.get("saved_at", 0)) >= 300 else ""
        print(route + "\t" + stale)
    except Exception:
        pass
' 2>/dev/null)
last="${last_raw%%$'\t'*}"
last_stale="${last_raw##*$'\t'}"
if [ -n "$last" ]; then
    stale_marker=""
    [ -n "$last_stale" ] && stale_marker="${_DIM}°${_RESET}"
    # Token count of the most recent routed call (input+output), read from the
    # savings log (last_route_*.json carries no token counts). Compact: "1.2k tok".
    last_tok=$(CHZ_SAVINGS_LOG="$SAVINGS_LOG" python3 -c '
import json, os
try:
    for line in reversed(open(os.environ["CHZ_SAVINGS_LOG"]).readlines()[-200:]):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        n = (d.get("input_tokens") or 0) + (d.get("output_tokens") or 0)
        if n > 0:
            print(("%.1fk tok" % (n/1000)) if n >= 1000 else ("%d tok" % n))
        break
except Exception:
    pass
' 2>/dev/null)
    tok_seg=""
    [ -n "$last_tok" ] && tok_seg=" ${_DIM}${last_tok}${_RESET}"
    parts+=("🔀 ${_MAUVE}${last}${_RESET}${stale_marker}${tok_seg}")
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
