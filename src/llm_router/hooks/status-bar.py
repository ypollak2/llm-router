#!/usr/bin/env python3
# llm_router-hook-version: 6
"""UserPromptSubmit hook — enhanced savings + routing status bar.

Displays a two-mode status line:
  compact (~80 chars):  📊 CC 45%s·28%w | 🦙✔ ⚙️✔ ☁️✔ | 💰 D:$1.42 W:$9.88 | 🛡️ enforce | 14.2x
  full    (~140 chars): 📊 CC 45%s·28%w·61%♪ | Ollama✔ Codex✔ APIs✔ | 💰 D:$1.42 W:$9.88 M:$41.15 (vs Sonnet:$58) | enforce🛡️ | eff 14.2x

Time buckets: today, this week (Mon), this calendar month, all-time.
Provider health: read from ~/.llm-router/health.json (written by background checks).
Enforcement mode: read from LLM_ROUTER_ENFORCE env var.

The hook only READS the line (PLAN v16 P0.9-b, status-bar p95 <= 300 ms). It is
computed by a detached refresher (``--refresh-cache``) into
``status_bar_cache.json`` (TTL 30 s). Live p95 was 4,488 ms (n = 344, [HL7]): two
``sqlite3.connect(usage.db, timeout=2)`` calls that wait out a writer's lock, plus
the Gemini quota read, all on the prompt's path. A stale line (up to
``_CACHE_SERVE_MAX_AGE_S``) is shown while one refresher runs; with no usable
cache the hook prints nothing this prompt rather than wait.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

# -- KPI G1: record how long this invocation ran (llm_router.hook_latency) -----
# The clock starts BEFORE the first llm_router import, so the package import is
# inside the measurement. Armed only when run as a script: a test that imports
# this file must not register an exit-time write. Fail-open: no llm_router on
# the path means no row for this run; any other error is reported on stderr
# (never stdout, which the host parses) and the hook carries on.
import time as _hl_time

_HOOK_T0 = _hl_time.monotonic()
if __name__ == "__main__":
    try:
        from llm_router import hook_latency as _hook_latency

        _hook_latency.begin("status-bar", "UserPromptSubmit", _HOOK_T0)
    except ImportError:
        pass  # llm_router is not importable on this host: no recorder, no row
    except Exception as _hl_exc:  # noqa: BLE001 -- timing must never break the hook
        import sys as _hl_sys

        print(f"llm-router: hook latency not recorded ({type(_hl_exc).__name__})", file=_hl_sys.stderr)

# .env -> os.environ for this process (llm_router.env_loader). The real
# environment wins; without the package this is a no-op, as it always was.
try:
    from llm_router.env_loader import load_dotenv_files as _apply_dotenv
    _apply_dotenv()
except Exception:
    pass


# ── Registered-tool surface (CHZ-SURF-01) ────────────────────────────────────
# Tool names are tier-dependent (LLM_ROUTER_SLIM). NEVER put a raw tool name in
# output: under the DEFAULT `consolidated` tier the legacy llm_query /
# llm_analyze / llm_code / llm_research / llm_generate names are not registered,
# so naming one hands the caller "Error: No such tool available" — after which
# it silently does the work on the expensive model and the savings dashboard
# cannot distinguish that from "chose not to route".
def _net(baseline_usd: float, actual_usd: float) -> float:
    """`savings.net_saved`, with a SIGNED fallback if the import fails.

    The fallback is deliberately the same arithmetic rather than a clamp: a
    surface that silently reverts to clamping when an import breaks is the
    defect returning through the error path, which is how it came back four
    times before.
    """
    try:
        from llm_router.savings import net_saved
        return net_saved(baseline_usd, actual_usd)
    except Exception:  # noqa: BLE001
        return float(baseline_usd) - float(actual_usd)


def _router_home():
    """Router state dir, resolved per call so LLM_ROUTER_HOME is honoured.

    M-04: this was a module constant bound at import, so a hook launched with
    LLM_ROUTER_HOME set still wrote to the operator's real home directory.

    Imports locally: hooks are standalone scripts with varied import headers and
    several do not import Path or os at module scope.
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    return _P(base).expanduser() if base else _P.home() / ".llm-router"


def _load_tool_surface_fns():
    """(route_tool, route_call, route_call_with_complexity) from llm_router.tool_surface.

    Falls back to the stdlib-only copy the installer drops next to the hooks, then
    to the in-repo source, then to identity (correct only for tier `off`).
    """
    try:
        from llm_router.tool_surface import (
            route_call,
            route_call_with_complexity,
            route_tool,
        )
        return route_tool, route_call, route_call_with_complexity
    except ImportError:
        pass
    try:
        import importlib.util as _ilu
        from pathlib import Path as _P
        _here = _P(__file__).resolve().parent
        for _cand in (_here / "llm_router_tool_surface.py", _here.parent / "tool_surface.py"):
            if not _cand.exists():
                continue
            _spec = _ilu.spec_from_file_location("llm_router_tool_surface", _cand)
            _mod = _ilu.module_from_spec(_spec)
            sys.modules["llm_router_tool_surface"] = _mod  # dataclasses needs this
            _spec.loader.exec_module(_mod)
            return _mod.route_tool, _mod.route_call, _mod.route_call_with_complexity
    except Exception:  # noqa: BLE001 — a broken support module must not kill the hook
        pass
    return (
        lambda n, **k: n,
        lambda n, *a, **k: (f"{n}({', '.join(a)})" if a else n),
        lambda n, c, *a, **k: f"{n}(complexity='{c}'" + ("".join(', ' + x for x in a)) + ")",
    )


route_tool, route_call, route_call_with_complexity = _load_tool_surface_fns()

# ── Paths ──────────────────────────────────────────────────────────────────
def _state_dir():
    return str(_router_home())
def _usage_json():
    return os.path.join(_state_dir(), "usage.json")
def _usage_db():
    return os.path.join(_state_dir(), "usage.db")
def _health_json():
    return os.path.join(_state_dir(), "health.json")
def _session_start_file():
    return os.path.join(_state_dir(), "session_start.txt")
def _prompt_count_file():
    return os.path.join(_state_dir(), "prompt_count.txt")

# ── Config ─────────────────────────────────────────────────────────────────
STATUS_EVERY = os.environ.get("LLM_ROUTER_STATUS_EVERY", "0")
STATUS_MODE = os.environ.get("LLM_ROUTER_STATUS_MODE", "compact")  # compact | full
ENFORCE_MODE = os.environ.get("LLM_ROUTER_ENFORCE", "hard").lower()

# Baseline cost for "what would Opus have cost?" comparison.
#
# WP-03: was a hardcoded 15.0/75.0 — the retired Opus 3 tier — so the savings
# figure on the status line, the number a user sees on every prompt, was
# overstated 3x. The pricing lint missed this: its value check wants both halves
# of a retired pair inside ONE assignment and these are two separate scalars,
# while its structural check only fires on dict/list/tuple containers. A bare
# float named ..._PER_M slips through both. Recorded as a lint gap — the lint is
# an immutable asset for this work package and cannot be edited here.
try:
    from llm_router import pricing as _pricing

    _host_price = _pricing.price_for("opus")
except ImportError:  # pragma: no cover — copied to ~/.claude/hooks/, runs standalone
    _host_price = None

HOST_PRICE_KNOWN = _host_price is not None
HOST_INPUT_PER_M = _host_price.input if _host_price else 0.0
HOST_OUTPUT_PER_M = _host_price.output if _host_price else 0.0

# ── ANSI colours ───────────────────────────────────────────────────────────
G = "\033[92m"   # green  — savings, provider OK, enforce
Y = "\033[93m"   # yellow — suggest mode, warn
R = "\033[91m"   # red    — error, provider down
C = "\033[96m"   # cyan   — shadow mode, active model
B = "\033[94m"   # blue   — efficiency multiplier (the "wow" number)
DIM = "\033[90m" # grey   — labels, separators
RST = "\033[0m"  # reset

SEP = f"{DIM} │ {RST}"

_FREE_PROVIDERS = {"ollama", "codex", "gemini_cli"}


# ── Claude subscription credits ────────────────────────────────────────────

def _read_claude_credits() -> tuple[float | None, float | None, float | None, bool]:
    """Return (session_pct, weekly_pct, sonnet_pct, is_stale)."""
    try:
        with open(_usage_json()) as f:
            data = json.load(f)
        stale = (time.time() - data.get("updated_at", 0)) > 1800
        return (
            data.get("session_pct"),
            data.get("weekly_pct"),
            data.get("sonnet_pct"),
            stale,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None, None, True


# ── Provider health ────────────────────────────────────────────────────────

def _read_provider_health() -> dict[str, str] | None:
    """Return {ollama, codex, apis} status, or None if health tracking not active.

    Returns None when health.json is missing or stale (>5 min) — callers
    suppress the health segment entirely in that case. The segment becomes
    visible automatically once the background health checker writes this file.
    """
    try:
        with open(_health_json()) as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) > 300:
            return None  # stale — checker may have stopped
        return {
            "ollama": data.get("ollama", "unknown"),
            "codex": data.get("codex", "unknown"),
            "apis": data.get("apis", "unknown"),
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None  # not yet active


def _health_icon(status: str, label: str = "", compact: bool = True) -> str:
    """Return coloured icon + optional label for a provider status."""
    if status == "ok":
        icon, col = "✔", G
    elif status == "warn":
        icon, col = "⚠", Y
    elif status == "down":
        icon, col = "✖", R
    else:
        icon, col = "?", DIM

    if compact:
        return f"{col}{icon}{RST}"
    return f"{label}{col}{icon}{RST}"


# ── Savings queries ────────────────────────────────────────────────────────

def _time_bucket_starts() -> dict[str, str]:
    """Return SQL-ready datetime strings for today, week, month."""
    now = datetime.now(tz=timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Monday of this week
    week_start = today.replace(day=today.day - today.weekday())
    # First of this month
    month_start = today.replace(day=1)
    return {
        "today": today.strftime("%Y-%m-%d %H:%M:%S"),
        "week": week_start.strftime("%Y-%m-%d %H:%M:%S"),
        "month": month_start.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _savings_for_period(conn: sqlite3.Connection, since: str) -> tuple[float, float]:
    """Return (saved_usd, baseline_usd) for calls since `since`."""
    rows = conn.execute(
        "SELECT provider, input_tokens, output_tokens, cost_usd FROM usage "
        "WHERE timestamp >= ? AND success = 1",
        (since,),
    ).fetchall()

    if not HOST_PRICE_KNOWN:
        # No price source: report nothing rather than a baseline of $0, which
        # would render as "saved $0.00" and read as a measurement.
        return 0.0, 0.0

    actual = baseline = 0.0
    for provider, in_tok, out_tok, cost in rows:
        in_tok = in_tok or 0
        out_tok = out_tok or 0
        cost = cost or 0.0
        host_cost = (in_tok * HOST_INPUT_PER_M + out_tok * HOST_OUTPUT_PER_M) / 1_000_000
        if provider == "subscription":
            continue  # no token data for subscription calls
        elif provider in _FREE_PROVIDERS:
            baseline += host_cost
            # actual cost is $0 for free providers
        else:
            actual += cost
            baseline += host_cost

    # AUD-06: signed, never clamped. A loss must reach the user — the clamp is exactly what stopped them finding out. This file was outside the CHZ-SS-01 lint's hand-maintained module list until R6 derived that list from `savings.SURFACES`.
    return _net(baseline, actual), baseline


def _read_savings() -> dict[str, tuple[float, float]]:
    """Return {today, week, month, session} → (saved_usd, baseline_usd)."""
    result: dict[str, tuple[float, float]] = {
        "today": (0.0, 0.0),
        "week": (0.0, 0.0),
        "month": (0.0, 0.0),
        "session": (0.0, 0.0),
    }
    try:
        buckets = _time_bucket_starts()
        conn = sqlite3.connect(_usage_db(), timeout=2)
        for key, since in buckets.items():
            result[key] = _savings_for_period(conn, since)

        # Session savings (since session start file)
        try:
            start_ts = float(open(_session_start_file()).read().strip())
            start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            result["session"] = _savings_for_period(conn, start_str)
        except (OSError, ValueError):
            pass

        conn.close()
    except Exception:
        pass
    return result


def _read_session_calls() -> tuple[int, int, int]:
    """Return (sub_calls, free_calls, paid_calls) for this session."""
    try:
        start = float(open(_session_start_file()).read().strip())
        start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(_usage_db(), timeout=2)
        rows = conn.execute(
            "SELECT provider FROM usage WHERE timestamp >= ? AND success = 1",
            (start_str,),
        ).fetchall()
        conn.close()
        sub = free = paid = 0
        for (provider,) in rows:
            if provider == "subscription":
                sub += 1
            elif provider in _FREE_PROVIDERS:
                free += 1
            else:
                paid += 1
        return sub, free, paid
    except Exception:
        return 0, 0, 0


# ── Enforcement mode badge ─────────────────────────────────────────────────

def _enforce_badge(compact: bool = True) -> str:
    mode = ENFORCE_MODE
    if mode in ("hard", "on"):
        col, icon, label = G, "🛡️", "enforce"
    elif mode == "soft":
        col, icon, label = Y, "💡", "suggest"
    elif mode == "off":
        col, icon, label = C, "👻", "shadow"
    else:
        col, icon, label = DIM, "?", mode

    if compact:
        return f"{col}{icon}{RST}"
    return f"{col}{icon} {label}{RST}"


# ── Efficiency multiplier ──────────────────────────────────────────────────

def _efficiency(saved: float, baseline: float) -> str:
    """Return coloured Nx efficiency string, or empty if no data."""
    if baseline < 0.001:
        return ""
    multiplier = baseline / max(baseline - saved, 0.0001)
    if multiplier >= 2.0:
        return f"{B}{multiplier:.1f}x{RST}"
    return f"{DIM}{multiplier:.1f}x{RST}"


# ── Format helpers ─────────────────────────────────────────────────────────

def _savings_str_compact(savings: dict[str, tuple[float, float]]) -> str:
    """D:$1.42 W:$9.88 — green when positive."""
    parts = []
    for label, key in [("D", "today"), ("W", "week")]:
        saved, _ = savings[key]
        col = G if saved >= 0.01 else DIM
        parts.append(f"{col}{label}:${saved:.2f}{RST}")
    return " ".join(parts)


def _savings_str_full(savings: dict[str, tuple[float, float]]) -> str:
    """D:$1.42 W:$9.88 M:$41.15 (vs Sonnet:$58) — with baseline comparison."""
    parts = []
    for label, key in [("D", "today"), ("W", "week"), ("M", "month")]:
        saved, _ = savings[key]
        col = G if saved >= 0.01 else DIM
        parts.append(f"{col}{label}:${saved:.2f}{RST}")

    month_saved, month_baseline = savings["month"]
    if month_baseline >= 0.01:
        parts.append(f"{DIM}(vs Sonnet:${month_baseline:.0f}){RST}")

    return " ".join(parts)


def _provider_health_compact(health: dict[str, str]) -> str:
    """🦙✔ ⚙️✔ ☁️✔"""
    return (
        f"🦙{_health_icon(health['ollama'])} "
        f"⚙️{_health_icon(health['codex'])} "
        f"☁️{_health_icon(health['apis'])}"
    )


def _provider_health_full(health: dict[str, str]) -> str:
    """Ollama✔ Codex✔ APIs✔"""
    return (
        f"{_health_icon(health['ollama'], 'Ollama', compact=False)} "
        f"{_health_icon(health['codex'], 'Codex', compact=False)} "
        f"{_health_icon(health['apis'], 'APIs', compact=False)}"
    )


def _read_gemini_credits() -> tuple[int | None, int | None, float | None]:
    """Return (count, limit, pressure)."""
    try:
        from llm_router.gemini_cli_quota import get_gemini_quota_status_sync
        status = get_gemini_quota_status_sync()
        if status.get("daily_limit", 0) > 0:
            return status.get("count"), status.get("daily_limit"), status.get("pressure")
    except Exception:
        pass
    return None, None, None


# ── Main format ────────────────────────────────────────────────────────────

def _format_status() -> str:
    session_pct, weekly_pct, sonnet_pct, stale = _read_claude_credits()
    g_count, g_limit, g_pressure = _read_gemini_credits()
    savings = _read_savings()
    health = _read_provider_health()
    sub, free, paid = _read_session_calls()
    compact = STATUS_MODE != "full"

    # ── Claude subscription segment ──
    if session_pct is not None:
        stale_mark = f" {Y}⚠{RST}" if stale else ""
        if compact:
            cc = f"CC {session_pct:.0f}%s·{weekly_pct:.0f}%w{stale_mark}"
        else:
            cc = f"CC {session_pct:.0f}%s·{weekly_pct:.0f}%w·{sonnet_pct:.0f}%♪{stale_mark}"
    else:
        cc = f"{DIM}CC — run {route_tool('llm_check_usage')}{RST}"

    # ── Gemini subscription segment ──
    g_seg = ""
    if g_count is not None:
        g_pct = int((g_pressure or 0) * 100)
        col = G if g_pct < 70 else (Y if g_pct < 90 else R)
        g_seg = f"{SEP}♊ {col}{g_pct}%{RST}"

    # ── Provider health segment (only when health.json is active and fresh) ──
    health_seg = None
    if health is not None:
        health_seg = _provider_health_compact(health) if compact else _provider_health_full(health)

    # ── Savings segment ──
    savings_seg = (
        f"💰 {_savings_str_compact(savings)}"
        if compact
        else f"💰 {_savings_str_full(savings)}"
    )

    # ── Enforcement mode ──
    enforce_seg = _enforce_badge(compact=compact)

    # ── Efficiency multiplier ──
    month_saved, month_baseline = savings["month"]
    eff = _efficiency(month_saved, month_baseline)

    # ── Session call counts (full mode only) ──
    calls_seg = ""
    if not compact and (sub + free + paid) > 0:
        calls_seg = f"{DIM}sub:{sub} free:{free} paid:{paid}{RST}"

    # ── Assemble ──
    if compact:
        # 📊 CC 45%s·28%w │ ♊ 2% │ [🦙✔ ⚙️✔ ☁️✔ │] 💰 D:$1.42 W:$9.88 │ 🛡️ │ 14.2x
        parts = ["📊 ", cc, g_seg]
        if health_seg:
            parts += [SEP, health_seg]
        parts += [SEP, savings_seg, SEP, enforce_seg]
        if eff:
            parts += [SEP, eff]
    else:
        # 📊 CC 45%s·28%w·61%♪ │ ♊ 2% │ [Ollama✔ Codex✔ APIs✔ │] 💰 D:$1.42 W:$9.88 M:$41.15 │ enforce🛡️ │ eff 14.2x
        parts = ["📊 ", cc, g_seg]
        if health_seg:
            parts += [SEP, health_seg]
        parts += [SEP, savings_seg, SEP, enforce_seg]
        if eff:
            parts += [SEP, f"{DIM}eff {RST}{eff}"]
        if calls_seg:
            parts += [SEP, calls_seg]

    return "".join(parts)


# ── Cache (P0.9-b): the hook reads, a detached refresher computes ─────────

_CACHE_FILENAME = "status_bar_cache.json"
_CLAIM_FILENAME = "status_bar_refresh.claim"
#: A line younger than this is current; an older one triggers a refresh.
_CACHE_TTL_S = 30.0
#: A line older than this is not shown at all (savings and quota go stale).
_CACHE_SERVE_MAX_AGE_S = 600.0
#: At most one refresher is started per this many seconds.
_REFRESH_CLAIM_S = 15.0


def _cache_path() -> str:
    return os.path.join(_state_dir(), _CACHE_FILENAME)


def _claim_path() -> str:
    return os.path.join(_state_dir(), _CLAIM_FILENAME)


def _cache_key() -> str:
    """The line depends on these settings; a cache written under others is unusable."""
    return f"{STATUS_MODE}|{ENFORCE_MODE}"


def _read_cache(now: float | None = None) -> tuple[str | None, float | None]:
    """(line, age_s) from the cache, or (None, None) when missing, unreadable or
    written under other settings. Never raises."""
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        ts, line = data.get("ts"), data.get("status")
        if (data.get("key") != _cache_key() or not isinstance(line, str)
                or isinstance(ts, bool) or not isinstance(ts, (int, float))):
            return None, None
        return line, max(0.0, (time.time() if now is None else now) - float(ts))
    except Exception:  # noqa: BLE001 -- no cache is the first-run normal
        return None, None


def _write_cache(line: str) -> None:
    """Atomically replace the cache. Never raises."""
    try:
        path = _cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "key": _cache_key(), "status": line}, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- the next refresh tries again
        return


def _claim_refresh(now: float | None = None) -> bool:
    """True when this call may start a refresher: none was started in the last
    ``_REFRESH_CLAIM_S``. A lost race starts at most one extra. Never raises."""
    try:
        now = time.time() if now is None else now
        path = _claim_path()
        try:
            if now - os.path.getmtime(path) < _REFRESH_CLAIM_S:
                return False
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(now))
        return True
    except Exception:  # noqa: BLE001
        return False


def _refresh_argv() -> list[str]:
    return [sys.executable, os.path.abspath(__file__), "--refresh-cache"]


def _spawn_refresh() -> None:
    """Detach one refresher. Never raises."""
    try:
        import subprocess

        subprocess.Popen(
            _refresh_argv(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001 -- no refresh this time; the next prompt retries
        return


def _refresh_cache() -> None:
    """The refresher: compute the line (sqlite, Gemini quota, health) and cache it."""
    try:
        _write_cache(_format_status())
    except Exception:  # noqa: BLE001 -- a broken refresh leaves the last good line
        return


# ── Throttle ───────────────────────────────────────────────────────────────

def _should_show() -> bool:
    if STATUS_EVERY == "session":
        return False

    try:
        every = int(STATUS_EVERY)
    except ValueError:
        every = 0

    if every <= 1:
        return True

    try:
        count = int(open(_prompt_count_file()).read().strip()) + 1 if os.path.exists(_prompt_count_file()) else 1
    except (ValueError, OSError):
        count = 1
    try:
        with open(_prompt_count_file(), "w") as f:
            f.write(str(count))
    except OSError:
        pass
    return count % every == 0


# ── Entry point ────────────────────────────────────────────────────────────

try:
    from llm_router.hook_latency import mark_main_start as _hl_mark_main, phase as _hl_phase
except ImportError:  # llm_router is not importable on this host: no recorder, no phases
    import contextlib as _hl_contextlib

    def _hl_phase(name):  # noqa: ARG001
        return _hl_contextlib.nullcontext()

    def _hl_mark_main():
        return None


def main() -> None:
    _hl_mark_main()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        payload = {}
    try:
        from llm_router.hook_latency import set_session as _hl_set_session

        _hl_set_session(payload.get("session_id") if isinstance(payload, dict) else None)
    except Exception:  # noqa: BLE001 -- older llm_router: no session on the row
        pass

    if not _should_show():
        sys.exit(0)

    with _hl_phase("read_cache"):
        status, age = _read_cache()
    if age is None or age > _CACHE_TTL_S:
        with _hl_phase("spawn"):
            if _claim_refresh():
                _spawn_refresh()
    if status is None or age is None or age > _CACHE_SERVE_MAX_AGE_S:
        sys.exit(0)  # nothing current to show; the refresher fills it for next time
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "systemMessage": status,
        }
    }))


def _entry(argv: list[str]) -> None:
    if "--refresh-cache" in argv:
        # The detached refresher re-runs this file, so the latency stanza armed a
        # status-bar row for it; its sqlite time is not the hook's (BUGS P09-3).
        os.environ["LLM_ROUTER_HOOK_LATENCY"] = "off"
        _refresh_cache()
    else:
        main()


if __name__ == "__main__":
    _entry(sys.argv[1:])
