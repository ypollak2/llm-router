#!/usr/bin/env python3
# llm_router-hook-version: 9
"""UserPromptSubmit hook — enhanced savings + routing status bar.

Displays a two-mode status line:
  compact: 📊 CC 45%s·28%w | 🦙✔ ⚙️✔ ☁️✔ | 💰 D:$1.42·today n=4 W:$9.88·7d n=31 | 🛡️
  full:    📊 CC 45%s·28%w·61%♪ | Ollama✔ Codex✔ APIs✔ | 💰 D:$1.42·today n=4 W:$9.88·7d n=31
           M:$41.15·month n=120 (vs <baseline model>) | enforce🛡️ | sub:4 free:3 paid:1 n=8 session

Every money figure is the ledger's (P3.4-B3, D-218): ``ledger_view.window_summary`` for the LOCAL
day, the rolling 7 days and the local month, printed with its n and window. Session call counts
are the session's mix (local/subscription/paid by evidence, proxy rows included; ``free`` = local,
``unc`` = unclassified). An unknown figure prints ``n/a``, never 0; a copy without ``llm_router``
(standalone) prints ``💰 n/a`` and scans nothing.
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
import sys
import time

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

# ── ANSI colours ───────────────────────────────────────────────────────────
G = "\033[92m"   # green  — savings, provider OK, enforce
Y = "\033[93m"   # yellow — suggest mode, warn
R = "\033[91m"   # red    — error, provider down
C = "\033[96m"   # cyan   — shadow mode, active model
B = "\033[94m"   # blue   — efficiency multiplier (the "wow" number)
DIM = "\033[90m" # grey   — labels, separators
RST = "\033[0m"  # reset

SEP = f"{DIM} │ {RST}"


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


# ── Ledger figures (P3.4-B3, D-218) ────────────────────────────────────────
# One source for every printed number: ledger_view.window_summary, local-time windows, each
# figure shown through ledger_view.show() with its n and window. No SQL in this file.

def _now() -> float:
    """The refresher's clock (one seam, so a test can freeze it)."""
    return time.time()


def _lv():
    """``llm_router.ledger_view``, or None for a standalone copy without the package: the
    caller then prints n/a and scans nothing. Any OTHER import-time error inside the package
    (F92) is counted (class only) and degrades the same way: the refresher then caches ``💰 n/a``
    instead of raising and leaving the last good line on screen."""
    try:
        from llm_router import ledger_view
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 -- counted below; the figures degrade to n/a
        _failopen("CHZ-FO-STATUS-BAR-LEDGER-IMPORT", exc)
        return None
    return ledger_view


def _failopen(code: str, exc: BaseException) -> None:
    """Count a swallowed exception (class name only, no text); never raises."""
    try:
        from llm_router import failopen

        failopen.record(code, exc)
    except Exception:  # noqa: BLE001 -- accounting must not raise; the package may be what is broken
        return


def _ledger_window(lv, window: str):
    """``LedgerWindow`` for ``today`` | ``7d`` | ``month`` | ``session``, or None when this home
    has no ledger at all. Called by the detached refresher only (it loads seats.json)."""
    from pathlib import Path

    root = _router_home()
    db, proxy = Path(_usage_db()), root / "proxy_calls.jsonl"
    if not db.is_file() and not proxy.is_file():
        return None
    if window == "session":
        return lv.window_summary("session", now=_now(), session_id=lv.read_session_id(root),
                                 session_start=lv.read_session_start(root), db_path=db,
                                 proxy_path=proxy)
    return lv.window_summary(window, now=_now(), db_path=db, proxy_path=proxy)


def _baseline_label() -> str | None:
    """The model every saving is measured against (``pricing.savings_baseline_model``)."""
    try:
        from llm_router import pricing
    except ImportError:
        return None
    return pricing.savings_baseline_model()


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


# ── Format helpers ─────────────────────────────────────────────────────────

def _money_part(lv, label: str, lw) -> str:
    """``D:$1.42·today n=4`` (green when positive), or ``D:n/a`` when the figure is unknown."""
    fig = None if lw is None else lw.figures()["saved"]
    text = lv.show(fig, "{:.2f}")
    if text is None:
        return f"{DIM}{label}:n/a{RST}"
    col = G if fig.value >= 0.01 else DIM
    return f"{col}{label}:${text}·{fig.window} n={fig.n}{RST}"


def _savings_str(lv, compact: bool) -> str:
    """D:$1.42·today n=4 W:$9.88·7d n=31 [M:$41.15·month n=120 (vs <baseline>)]."""
    if lv is None:
        return f"{DIM}n/a{RST}"
    keys = [("D", "today"), ("W", "7d")] + ([] if compact else [("M", "month")])
    windows = {window: _ledger_window(lv, window) for _label, window in keys}
    parts = [_money_part(lv, label, windows[window]) for label, window in keys]
    if not compact:
        baseline, month = _baseline_label(), windows["month"]
        if baseline and month is not None and lv.show(month.figures()["saved"]) is not None:
            parts.append(f"{DIM}(vs {baseline}){RST}")
    return " ".join(parts)


def _calls_str(lv) -> str:
    """The session's mix: ``sub:4 free:3 paid:1 [unc:8] n=16 session`` (free = local). Empty
    when the session's figures are unknown or it executed nothing."""
    if lv is None:
        return ""
    lw = _ledger_window(lv, "session")
    if lw is None:
        return ""
    figs = lw.figures()
    sub, free, paid, total = (lv.show(figs[k], "{:.0f}") for k in
                              ("mix_subscription", "mix_local", "mix_paid", "executed"))
    if None in (sub, free, paid, total) or int(total) == 0:
        return ""
    unc = f" unc:{lw.unclassified_n}" if lw.unclassified_n else ""
    window = "session" if lw.scope == "session_id" else "session~"   # ~ = by timestamp
    return f"sub:{sub} free:{free} paid:{paid}{unc} n={total} {window}"


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
    lv = _lv()
    health = _read_provider_health()
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
    savings_seg = f"💰 {_savings_str(lv, compact)}"

    # ── Enforcement mode ──
    enforce_seg = _enforce_badge(compact=compact)

    # ── Session call counts (full mode only) ──
    calls_seg = ""
    if not compact:
        counts = _calls_str(lv)
        calls_seg = f"{DIM}{counts}{RST}" if counts else ""

    # ── Assemble ──
    if compact:
        # 📊 CC 45%s·28%w │ ♊ 2% │ [🦙✔ ⚙️✔ ☁️✔ │] 💰 D:$1.42·today n=4 W:$9.88·7d n=31 │ 🛡️
        parts = ["📊 ", cc, g_seg]
        if health_seg:
            parts += [SEP, health_seg]
        parts += [SEP, savings_seg, SEP, enforce_seg]
    else:
        # 📊 CC 45%s·28%w·61%♪ │ ♊ 2% │ [Ollama✔ Codex✔ APIs✔ │] 💰 D:… W:… M:… (vs <baseline>) │ enforce🛡️ │ sub:… free:… paid:…
        parts = ["📊 ", cc, g_seg]
        if health_seg:
            parts += [SEP, health_seg]
        parts += [SEP, savings_seg, SEP, enforce_seg]
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
    """Detach one refresher through the package's one detached-spawn helper
    (fork + execv, own session, no stdio; the status line tick uses it too).
    Never raises."""
    try:
        from llm_router.statusline_tick import _spawn_detached

        _spawn_detached(_refresh_argv())
    except Exception:  # noqa: BLE001 -- no refresh this time; the next prompt retries
        return


def _refresh_cache() -> None:
    """The refresher: compute the line (ledger, Gemini quota, health) and cache it."""
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
