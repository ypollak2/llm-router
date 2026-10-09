#!/bin/bash
# Claude Code statusline — llm_router routing indicators
#
# Layout: 🤖 CC quota · ⏰ reset · 📂 cwd · 🧠 ctx [bar] · 💰 est. saved · ⚖ mix · 🛡 mode · health · 🔀 last
#
# v10.1.5: Catppuccin Mocha palette + emoji icons + context bar, inspired by
# AwesomeJun/CC-statusline. Truecolor (24-bit) ANSI — falls back gracefully
# on terminals that strip escapes, since segment text is still readable.
#
# IMPORTANT: Must consume stdin — Claude Code pipes session JSON here.
# Without reading it, the pipe blocks and Claude Code times out.

# >>> statusline timing (P0.9-c) ─────────────────────────────────────────────
# LLM_ROUTER_STATUSLINE_TIMING=1 times 1 call in 20 (=all: every call) and
# appends a hook_latency row (hook "statusline", PRD bar 100 ms) from a
# BACKGROUNDED `python -m llm_router.hook_latency record-raw`, so the write is
# not inside the number and never delays the line. Clock: perl Time::HiRes,
# because macOS /bin/bash 3.2 has no EPOCHREALTIME. Unsampled calls pay only
# the env test and $RANDOM below (no process). The sampled number excludes the
# bash start-up before this line, as the hooks' numbers exclude the interpreter's.
_slt_on="${LLM_ROUTER_STATUSLINE_TIMING:-}"
_slt_t0=""
if [ -n "$_slt_on" ] && [ "$_slt_on" != "0" ] && [ "$_slt_on" != "off" ]; then
    if [ "$_slt_on" = "all" ] || [ $((RANDOM % 20)) -eq 0 ]; then
        _slt_t0=$(perl -MTime::HiRes=time -e 'printf "%.0f",time*1000' 2>/dev/null)
    fi
fi
# A python that can import llm_router: sets $_chz_py, or leaves it empty. Shared
# with the money segment below, which documents the resolution order.
_chz_find_py() {
    _chz_py=""
    for _cand in \
        "$(command -v llm-router 2>/dev/null | xargs -I{} head -1 {} 2>/dev/null | sed 's|^#!||' | awk '{print $1}')" \
        "$(command -v llm_router 2>/dev/null | xargs -I{} head -1 {} 2>/dev/null | sed 's|^#!||' | awk '{print $1}')" \
        "$(command -v python3 2>/dev/null)" \
        "$(command -v python 2>/dev/null)" \
        "$HOME/.local/pipx/venvs/llm-routing/bin/python" \
        "$HOME/.local/bin/python3" \
        "$HOME/Projects/llm-router/.venv/bin/python3" \
        "$(dirname "$0")/../../.venv/bin/python3"; do
        if [ -n "$_cand" ] && [ -x "$_cand" ] && "$_cand" -c "import llm_router" 2>/dev/null; then
            _chz_py="$_cand"; break
        fi
    done
}
# The row carries the session id from the session JSON on stdin ($input), so a
# reader can count sessions and drop research / executor ones (PLAN v16 §1.4
# rules 4 and 8). Matched in bash (no process). The t1 clock is read inside a
# freshly started perl, so every sampled number includes one perl start-up
# (a few ms idle, more under load): a known upward bias against the 100 ms bar,
# conservative for P0.9-c. The interpreter search runs in
# the backgrounded child, after the clock stopped: the fast line and a full line
# without usage.db never set $_chz_py, and a bare python3 that cannot import
# llm_router would write no row.
_slt_finish() {
    [ -n "$_slt_t0" ] || return 0
    local _t1 _sid="" _re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9._-]+)"'
    _t1=$(perl -MTime::HiRes=time -e 'printf "%.0f",time*1000' 2>/dev/null) || return 0
    [ -n "$_t1" ] || return 0
    [[ "$input" =~ $_re ]] && _sid="${BASH_REMATCH[1]}"
    ( { [ -n "$_chz_py" ] || _chz_find_py
        [ -n "$_chz_py" ] && "$_chz_py" -m llm_router.hook_latency record-raw \
            statusline Statusline "$((_t1 - _slt_t0))" "$_sid"
      } </dev/null >/dev/null 2>&1 & )
    return 0
}
[ -n "$_slt_t0" ] && trap _slt_finish EXIT
# <<< statusline timing ──────────────────────────────────────────────────────

# ── Debug mode: the fast line (opt-in) ───────────────────────────────────────
# The default is the full layout below, unchanged from before PR #273 (owner
# decision, reversing #273's fast-by-default). LLM_ROUTER_STATUSLINE=fast swaps
# it for llm_router_statusline_tick.py: ONE short line (llm-router, mode, North
# Star with n, Claude 5h / weekly / Sonnet quota from usage.json, Codex window,
# a slow-hooks warning) read from cache files only; it never fetches, never
# waits on its background refresh, and prints "n/a" (never 0) for unknown.
# The real environment wins; otherwise the same key in the router's own .env
# (${LLM_ROUTER_HOME:-~/.llm-router}/.env, the file the hooks already load) is
# honoured, so the switch needs no edit to ~/.claude/settings.json.
# LLM_ROUTER_STATUSLINE=both prints the full layout, then the fast line on a
# second row (Claude Code renders each printed line as its own status row:
# https://code.claude.com/docs/en/statusline, "Multiple lines"). Any other
# value (unset, full, unknown) is the full layout only.
# Pure bash on purpose: no extra process on a once-a-second loop.
_sl_mode="${LLM_ROUTER_STATUSLINE:-}"
if [ -z "$_sl_mode" ]; then
    _sl_home="${LLM_ROUTER_HOME:-$HOME/.llm-router}"
    _sl_home="${_sl_home/#\~/$HOME}"
    if [ -f "$_sl_home/.env" ] && [ -r "$_sl_home/.env" ]; then
        while IFS= read -r _sl_line || [ -n "$_sl_line" ]; do
            _sl_line="${_sl_line#export }"
            case "$_sl_line" in
                LLM_ROUTER_STATUSLINE=*) _sl_mode="${_sl_line#LLM_ROUTER_STATUSLINE=}" ;;
            esac
        done < "$_sl_home/.env"
    fi
fi
_sl_mode="${_sl_mode//\"/}"
_sl_mode="${_sl_mode//\'/}"
_sl_mode="${_sl_mode%%[[:space:]]*}"
# -I: the source tree's own types.py sits beside statusline_tick.py and would
# shadow the stdlib module of that name if the script's folder were on sys.path.
# -S: the tick is stdlib-only, and skipping site saves ~8 ms a tick.
_tick="${0%/*}/llm_router_statusline_tick.py"
[ -f "$_tick" ] || _tick="${0%/*}/../statusline_tick.py"
if [ "$_sl_mode" = "fast" ]; then
    if [ -f "$_tick" ] && command -v python3 >/dev/null 2>&1; then
        # A timed call cannot exec (the EXIT trap would never run). It reads the
        # session JSON itself so the row can carry the session id, and hands it on.
        if [ -n "$_slt_t0" ]; then
            input=$(cat)
            printf '%s' "$input" | python3 -I -S "$_tick"
            exit $?
        fi
        exec python3 -I -S "$_tick"
    fi
fi

# ── Input: three fields out of the session JSON, in bash (P0.9-c) ────────────
# These were three `python3 -c` processes (~20 ms each). Claude Code's JSON has
# no backslashes in a normal path; if it does (escaped quote, \n, \uXXXX) the
# regexes below could mis-read it, so ANY backslash sends the whole parse to one
# python, which is exactly what the three one-liners did. Fields only ever reach
# a process through argv/env (CHZ-SEC-07), never through source text.
# `read -d ''` slurps stdin with no `cat` process; it returns 1 at EOF, which is not an error here.
IFS= read -r -d '' input || true
session_cwd="" transcript_path="" model_id="" _sid=""
if [[ "$input" == *\\* ]]; then
    _parsed=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    d = {}
m = d.get("model")
mid = m.get("id", "") if isinstance(m, dict) else (m if isinstance(m, str) else "")
sid = d.get("session_id", "")
for v in (d.get("cwd", ""), d.get("transcript_path", ""), mid, sid):
    print(str(v).replace("\n", "\x01"))
' 2>/dev/null)
    { IFS= read -r session_cwd; IFS= read -r transcript_path; IFS= read -r model_id; IFS= read -r _sid; } <<< "$_parsed"
    transcript_path="${transcript_path//$'\x01'/$'\n'}"
else
    # These regexes take the FIRST match anywhere in the JSON, not strictly the top-level
    # key. The session JSON has each key once; a nested look-alike could only change a
    # label (values reach a process through argv only), so this is documented, not guarded.
    _re='"cwd"[[:space:]]*:[[:space:]]*"([^"]*)"'
    [[ "$input" =~ $_re ]] && session_cwd="${BASH_REMATCH[1]}"
    _re='"transcript_path"[[:space:]]*:[[:space:]]*"([^"]*)"'
    [[ "$input" =~ $_re ]] && transcript_path="${BASH_REMATCH[1]}"
    _re='"model"[[:space:]]*:[[:space:]]*\{[^}]*"id"[[:space:]]*:[[:space:]]*"([^"]*)"'
    if [[ "$input" =~ $_re ]]; then
        model_id="${BASH_REMATCH[1]}"
    else
        _re='"model"[[:space:]]*:[[:space:]]*"([^"]*)"'
        [[ "$input" =~ $_re ]] && model_id="${BASH_REMATCH[1]}"
    fi
    _re='"session_id"[[:space:]]*:[[:space:]]*"([A-Za-z0-9._-]+)"'
    [[ "$input" =~ $_re ]] && _sid="${BASH_REMATCH[1]}"
fi
[[ "$_sid" =~ ^[A-Za-z0-9._-]+$ ]] || _sid="default"

STATE_DIR="$HOME/.llm-router"
USAGE_JSON="$STATE_DIR/usage.json"
USAGE_DB="$STATE_DIR/usage.db"
# GH#50: the health check and last-route token suffix read this log; it is now
# read by statusline_segments.py (STATE_DIR/savings_log.jsonl), which defines it
# the same way. Kept assigned here so the variable is never read unset.
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

# Pick color by 0–100 percentage threshold (green→yellow→red). Sets $_c (no
# subshell: `$(_pct_color …)` cost a fork per call).
_pct_color() {
    local pct=$1
    if [ "$pct" -ge 80 ]; then _c="$_RED"
    elif [ "$pct" -ge 50 ]; then _c="$_YELLOW"
    else _c="$_GREEN"
    fi
}

# Render a fixed-width progress bar with intensity color. Sets $_barout.
_bar() {
    local pct=$1 width=${2:-10}
    [ "$pct" -lt 0 ] && pct=0
    [ "$pct" -gt 100 ] && pct=100
    local filled=$(( pct * width / 100 ))
    local empty=$(( width - filled ))
    _pct_color "$pct"
    local bar="" i=0
    while [ $i -lt $filled ]; do bar+="█"; i=$((i+1)); done
    i=0
    while [ $i -lt $empty ]; do bar+="░"; i=$((i+1)); done
    _barout="${_c}${bar}${_RESET}"
}

# ── The segments come from a cache, not from fourteen processes (P0.9-c) ─────
# Everything below that used to be computed here -- quota, reset time, context
# tokens, today's money, route mix, proxy probe, health, last route -- is computed
# by ONE detached process (statusline_segments.py) into a per-session key=value
# file that this script reads with `read` (no process, no eval). Measured before:
# ~540 ms median over 16 subprocesses; the PRD bar is 100 ms. See that module for
# the file format and the staleness rule.
#
#   cache missing / other schema  -> compute once, synchronously (first render of a
#                                    session; also every run in a fresh test HOME)
#   cache older than 30 s, or the transcript is newer than it
#                                 -> render the cache, start a detached refresh
#   cache older than 90 s         -> render it with a visible "cached <age>" marker
_SEG_TTL=30
_SEG_STALE_MARK=90
_seg_file="$STATE_DIR/statusline_seg_${_sid}.kv"
_seg_py_file="$STATE_DIR/.statusline_python"
_seg_script="${0%/*}/llm_router_statusline_segments.py"
[ -f "$_seg_script" ] || _seg_script="${0%/*}/../statusline_segments.py"
# bash >= 4.2 has the clock built in; macOS /bin/bash 3.2 does not and pays one `date`.
printf -v _now '%(%s)T' -1 2>/dev/null || _now=$(date +%s 2>/dev/null)
case "$_now" in ''|*[!0-9]*) _now=0 ;; esac

# Sets $_seg_py: the interpreter that imports llm_router, else the ambient python3
# (money is then omitted). .statusline_python holds three lines: path, epoch it was
# probed, kind (full = imports llm_router, plain = it did not). A "plain" answer is
# re-probed after 5 min and a "full" one after a day, so installing llm_router later
# lights money up without anyone deleting a file. A file with only a path (older
# writers, tests) is trusted. THIS FUNCTION PROBES (several `python -c "import
# llm_router"`, ~100 ms each) and therefore only ever runs in the detached child, or
# in the rate-limited first-render sync call -- never on a cache hit's render path.
_seg_resolve_py() {
    _seg_py=""
    local _ep="" _kind="" _ttl=86400
    if [ -r "$_seg_py_file" ]; then
        { IFS= read -r _seg_py; IFS= read -r _ep; IFS= read -r _kind; } < "$_seg_py_file"
    fi
    _seg_int "$_ep" 10; _ep="$_n"
    [ "$_kind" = "plain" ] && _ttl=300
    if [ -n "$_seg_py" ] && [ -x "$_seg_py" ]; then
        if [ -z "$_ep" ] || [ $(( _now - _ep )) -lt "$_ttl" ]; then return 0; fi
    fi
    _chz_find_py
    if [ -n "$_chz_py" ]; then
        _seg_py="$_chz_py"; _kind=full
    else
        _seg_py="$(command -v python3 2>/dev/null)"; _kind=plain
    fi
    [ -n "$_seg_py" ] && ( umask 077; printf '%s\n%s\n%s\n' "$_seg_py" "$_now" "$_kind" > "$_seg_py_file" ) 2>/dev/null
}
# $1 = "sync" to wait, anything else to detach.
_seg_exec() {
    _seg_resolve_py
    [ -n "$_seg_py" ] || return 0
    local -a _cmd
    # -I: the source tree's own types.py etc. sit beside the script and would
    # shadow the stdlib if its folder were on sys.path (as for the fast tick).
    # Deploy order: llm_router_statusline_segments.py goes in BEFORE this script. If it
    # is absent the fallback is `-m llm_router.statusline_segments` (works once the
    # package is upgraded); if that fails too nothing is cached and the line shows
    # "segments pending" instead of silently dropping the segments.
    if [ -f "$_seg_script" ]; then _cmd=("$_seg_py" -I "$_seg_script")
    else _cmd=("$_seg_py" -m llm_router.statusline_segments); fi
    _cmd+=(--state "$STATE_DIR" --session "$_sid" --transcript "$transcript_path" --model "$model_id")
    "${_cmd[@]}" </dev/null >/dev/null 2>&1
}
_seg_run() {
    if [ "$1" = "sync" ]; then
        _seg_exec
    else
        # Detached, probe included: the render never waits for an interpreter search.
        ( _seg_exec & ) >/dev/null 2>&1
    fi
}

# $1 = a value read from a file, $2 = max digits. Sets $_n to its base-10 value, or "" when
# it is not 1..$2 plain digits. Length-capped (a 23-digit operand overflows `$(( ))` and
# `[ -gt ]`) and forced to base 10 (a leading zero is octal to the shell: "08" is an error).
_seg_int() {
    _n=""
    case "$1" in ''|*[!0-9]*) return 0 ;; esac
    [ "${#1}" -le "$2" ] || return 0
    _n=$((10#$1))
}

s_v="" s_written="" s_usage="" s_session_pct="" s_weekly_pct="" s_usage_stale="" s_reset=""
s_ctx_human="" s_ctx_pct="" s_money="" s_mix_local="" s_mix_paid="" s_proxy_down=""
s_health="" s_last="" s_last_stale="" s_last_tok=""
_seg_load() {
    s_v="" s_written="" s_usage="" s_session_pct="" s_weekly_pct="" s_usage_stale="" s_reset=""
    s_ctx_human="" s_ctx_pct="" s_money="" s_mix_local="" s_mix_paid="" s_proxy_down=""
    s_health="" s_last="" s_last_stale="" s_last_tok=""
    [ -r "$_seg_file" ] || return 1
    local k v
    while IFS='=' read -r k v || [ -n "$k" ]; do
        case "$k" in
            v) s_v="$v" ;;                       written) s_written="$v" ;;
            usage) s_usage="$v" ;;               session_pct) s_session_pct="$v" ;;
            weekly_pct) s_weekly_pct="$v" ;;     usage_stale) s_usage_stale="$v" ;;
            reset) s_reset="$v" ;;               ctx_human) s_ctx_human="$v" ;;
            ctx_pct) s_ctx_pct="$v" ;;           money) s_money="$v" ;;
            mix_local) s_mix_local="$v" ;;       mix_paid) s_mix_paid="$v" ;;
            proxy_down) s_proxy_down="$v" ;;     health) s_health="$v" ;;
            last) s_last="$v" ;;                 last_stale) s_last_stale="$v" ;;
            last_tok) s_last_tok="$v" ;;
        esac
    done < "$_seg_file"
    # Every value used in arithmetic or a numeric test must be digits only: the file
    # is data, and `$(( ))` / `[[ -gt ]]` evaluate their operands (a value such as
    # a[$(cmd)] would run cmd). Anything else is dropped, i.e. the segment is hidden.
    _seg_int "$s_session_pct" 3; s_session_pct="$_n"; [ -n "$s_session_pct" ] && [ "$s_session_pct" -gt 100 ] && s_session_pct=100
    _seg_int "$s_weekly_pct" 3;  s_weekly_pct="$_n";  [ -n "$s_weekly_pct" ] && [ "$s_weekly_pct" -gt 100 ] && s_weekly_pct=100
    _seg_int "$s_ctx_pct" 3;     s_ctx_pct="$_n";     [ -n "$s_ctx_pct" ] && [ "$s_ctx_pct" -gt 100 ] && s_ctx_pct=100
    _seg_int "$s_mix_local" 9;   s_mix_local="$_n"
    _seg_int "$s_mix_paid" 9;    s_mix_paid="$_n"
    _seg_int "$s_written" 10;    s_written="$_n"
    [ "$s_v" = "1" ]
}

if ! _seg_load; then
    # First render of a session (or a refresher that cannot produce a cache): compute
    # synchronously, but at most once per 5 s per session, so a broken refresher costs
    # one failed call per 5 s and not one per render.
    _sync_file="$STATE_DIR/.statusline_seg_sync_${_sid}"
    _last_sync=0
    [ -r "$_sync_file" ] && IFS= read -r _last_sync < "$_sync_file"
    _seg_int "$_last_sync" 10; _last_sync="${_n:-0}"
    if [ $(( _now - _last_sync )) -ge 5 ]; then
        ( umask 077; printf '%s\n' "$_now" > "$_sync_file" ) 2>/dev/null
        _seg_run sync
        _seg_load
    fi
fi
_seg_age=0
case "$s_written" in ''|*[!0-9]*) ;; *) [ "$_now" -gt 0 ] && _seg_age=$(( _now - s_written )) ;; esac
if [ "$s_v" = "1" ]; then
    _want=""
    [ "$_seg_age" -ge "$_SEG_TTL" ] && _want=1
    # The transcript grows with every turn: a context figure older than the last
    # message is wrong, so a newer transcript asks for a refresh too (rate-limited below).
    [ -n "$transcript_path" ] && [ "$transcript_path" -nt "$_seg_file" ] && [ "$_seg_age" -ge 3 ] && _want=1
    if [ -n "$_want" ]; then
        # At most one launch per 5 s per session: the marker holds the last launch's epoch.
        _spawn_file="$STATE_DIR/.statusline_seg_spawn_${_sid}"
        _last_spawn=0
        [ -r "$_spawn_file" ] && IFS= read -r _last_spawn < "$_spawn_file"
        _seg_int "$_last_spawn" 10; _last_spawn="${_n:-0}"
        if [ $(( _now - _last_spawn )) -ge 5 ]; then
            ( umask 077; printf '%s\n' "$_now" > "$_spawn_file" ) 2>/dev/null
            _seg_run async
        fi
    fi
fi

parts=()

# ── 🤖 Claude subscription usage ─────────────────────────────────────────────
# is_fallback marks a snapshot session-start.py wrote when the OAuth fetch
# FAILED: session/weekly/sonnet all set to 50. Rendering that as a measurement
# told a user pacing a five-hour window that half of it was gone when the real
# figure was 2%. Three identical 50s is not data. (Decided in the segments file.)
if [ "$s_usage" = "fallback" ]; then
    # Say what is true: the number is unknown, not zero and not fifty.
    parts+=("🤖 ${_DIM}quota unknown${_RESET}")
elif [ "$s_usage" = "ok" ] && [ -n "$s_session_pct" ]; then
    _pct_color "$s_session_pct"; s_color="$_c"
    _pct_color "${s_weekly_pct:-0}"; w_color="$_c"
    # A ° marker when the displayed numbers are older than the TTL (a refresh is
    # in flight, or the refresh chain is broken).
    stale_marker=""
    [ "$s_usage_stale" = "1" ] && stale_marker="${_DIM}°${_RESET}"
    parts+=("🤖 ${s_color}${s_session_pct}%${_RESET}${_DIM}/5h${_RESET} ${w_color}${s_weekly_pct}%${_RESET}${_DIM}/wk${_RESET}${stale_marker}")
fi

# ── ⏰ Quota reset time ──────────────────────────────────────────────────────
if [ "$s_usage" = "ok" ] && [ -n "$s_reset" ]; then
    parts+=("⏰ ${_YELLOW}${s_reset}${_RESET}")
fi

# ── 📂 Working directory ─────────────────────────────────────────────────────
if [ -n "$session_cwd" ]; then
    dir_name="${session_cwd%/}"; dir_name="${dir_name##*/}"
    if [ -n "$dir_name" ] && [ "$dir_name" != "/" ]; then
        parts+=("📂 ${_BLUE}${dir_name}${_RESET}")
    fi
fi

# ── 🧠 Context tokens (with progress bar) ────────────────────────────────────
if [ -n "$s_ctx_human" ] && [ -n "$s_ctx_pct" ]; then
    _bar "$s_ctx_pct" 8
    parts+=("🧠 ${_PINK}${s_ctx_human}${_RESET} ${_barout} ${_DIM}${s_ctx_pct}%${_RESET}")
fi

# ── 💰 Today's savings, via the CANONICAL aggregation ────────────────────────
# INV-COST-004: the figure is shaped by dashboard_data.Summary.compact() and
# labelled "est." -- computed in statusline_segments.py (it needs `import
# llm_router`, ~170 ms, which is why it can never sit on this path). Omitted when
# no interpreter can import llm_router or the day is below the spend floor.
if [ -n "$s_money" ]; then
    parts+=("💰 ${_GREEN}${s_money}${_RESET}")
fi

# ⚖ route mix — local vs paid over the last 6h. Answers "is routing working
# right now", which quota does not. Green only when local carries the majority.
if [ -n "$s_mix_local" ] && [ -n "$s_mix_paid" ]; then
    if [ "${s_mix_local:-0}" -ge "${s_mix_paid:-0}" ]; then _mixc="$_GREEN"; else _mixc="$_YELLOW"; fi
    parts+=("⚖ ${_mixc}${s_mix_local:-0}L/${s_mix_paid:-0}P${_RESET}")
fi

# ── 🛡 Enforce mode ──────────────────────────────────────────────────────────
enforce="${LLM_ROUTER_ENFORCE:-smart}"
case "$enforce" in
    hard|on)        parts+=("🛡  ${_RED}enforce${_RESET}") ;;
    soft|suggest)   parts+=("🛡  ${_YELLOW}suggest${_RESET}") ;;
    off|observe|shadow) parts+=("🛡  ${_DIM}shadow${_RESET}") ;;
    smart|advise)   parts+=("🛡  ${_SKY}smart${_RESET}") ;;
esac

# ── 🔌 Proxy-default down (llm-router install --proxy-default) ──────────────
# Every session depends on this proxy once installed -- a dead port fails every
# API call. Probed (raw TCP connect, 0.3 s timeout) by statusline_segments.py,
# gated on the sentinel; shown from the cache, so it can lag by the cache TTL.
if [ -n "$s_proxy_down" ]; then
    parts+=("${_RED}🔌 proxy down:${s_proxy_down}${_RESET}")
fi

# ── ❤ Health (statusline_segments.health_segment; mirrors observability.surface_status) ──
# ok ✓ / degraded ⚠ (usage data stale) / idle ○ (no provider activity, Ollama up)
# / down ✗ (no provider configured AND Ollama unreachable -- the only outage glyph, GH#63).
# A glyph with no noun is not actionable, hence the words.
case "$s_health" in
    ok)       parts+=("${_GREEN}✓${_RESET}") ;;
    degraded) parts+=("${_YELLOW}⚠ stale${_RESET}") ;;
    idle)     parts+=("${_DIM}○ idle${_RESET}") ;;
    down)     parts+=("${_RED}✗ no provider${_RESET}") ;;
esac

# ── 🔀 Last route (always shown) ─────────────────────────────────────────────
# Persistent: always render the most recent route. A dim ° marker is appended
# when the route is older than 5 min, matching the quota segment's stale cue.
if [ -n "$s_last" ]; then
    stale_marker=""
    [ "$s_last_stale" = "1" ] && stale_marker="${_DIM}°${_RESET}"
    tok_seg=""
    [ -n "$s_last_tok" ] && tok_seg=" ${_DIM}${s_last_tok}${_RESET}"
    parts+=("🔀 ${_MAUVE}${s_last}${_RESET}${stale_marker}${tok_seg}")
fi

# ── Cache staleness: a cache the refresher stopped updating says so ──────────
if [ "$s_v" != "1" ]; then
    # No cache and none could be made right now (lock held by a racing first render,
    # refresher broken, or the sync call rate-limited): say so.
    parts+=("${_DIM}segments pending${_RESET}")
elif [ "$_seg_age" -ge "$_SEG_STALE_MARK" ]; then
    if [ "$_seg_age" -ge 120 ]; then _age_txt="$(( _seg_age / 60 ))m"; else _age_txt="${_seg_age}s"; fi
    parts+=("${_DIM}cached ${_age_txt} ago${_RESET}")
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

# both: the fast line goes second. Its stdin is /dev/null (the full part above
# already consumed the session JSON); the tick reads caches only and never waits.
if [ "$_sl_mode" = "both" ] && [ -f "$_tick" ] && command -v python3 >/dev/null 2>&1; then
    python3 -I -S "$_tick" </dev/null
fi
