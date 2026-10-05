"""Background refresh of the status line's cache (``statusline_cache.json``).

:mod:`llm_router.statusline_tick` renders the status line about once a second
and must stay cheap, so it never computes anything: it reads the file this
module writes, and starts ``llm-router statusline --refresh`` (detached) when
the file is older than a minute. The expensive work lives here:

* ``ns``                the North Star exactly as ``llm-router kpi`` reads it
                        (``commands.kpi.compute_scorecard``, 7 days, organic):
                        ``pct`` only when the KPI is measurable, ``n`` always
                        when the KPI counted something; ``None`` = unknown.
* ``claude_weekly_pct`` ``usage.json``'s ``weekly_pct``; ``None`` when the
                        snapshot is a fallback (``is_fallback``: invented 50s),
                        unreadable, or older than :data:`QUOTA_STALE_S`.
* ``codex``             ``codex_window.snapshot()`` when the window file
                        exists; ``None`` when Codex has never been counted on
                        this machine (0/15 would claim a measurement).
* ``hooks_slow``        the worst hook p95 from the same scorecard's G1 (hook)
                        when it is over its budget or over
                        ``LLM_ROUTER_STATUSLINE_SLOW_MS`` (default 1000 ms);
                        session start/end hooks run once a session and are not
                        counted against the second threshold.
* ``mode``              ``enforce_config.resolve_enforce_mode()``.

One refresher at a time (a non-blocking lock: a second one exits at once), and
the cache is replaced atomically at 0600. Nothing here raises to the caller; a
part that fails is written as ``None`` (unknown), never as 0.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from llm_router import paths

CACHE_NAME = "statusline_cache.json"
LOCK_NAME = ".statusline-refresh.lock"
#: usage.json older than this is not a quota reading (same 30 min the legacy
#: status line's health check used for "stale").
QUOTA_STALE_S = 1800.0
DEFAULT_SLOW_MS = 1000.0
#: A refresh still running after this is killed (SIGALRM): the lock is freed.
REFRESH_TIMEOUT_S = 120
#: Run once per session: a slow one is not a per-prompt delay.
_ONCE_PER_SESSION_HOOKS = frozenset({"session-start", "session-end"})


def cache_path() -> Path:
    return paths.state_path(CACHE_NAME)


def _slow_threshold_ms() -> float:
    raw = os.environ.get("LLM_ROUTER_STATUSLINE_SLOW_MS", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SLOW_MS
    return value if value > 0 else DEFAULT_SLOW_MS


def _quota(now: float) -> float | None:
    path = paths.state_path("usage.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("is_fallback"):
        return None
    ts = data.get("updated_at")
    try:
        age = now - float(ts) if ts is not None else now - path.stat().st_mtime
    except (OSError, TypeError, ValueError):
        return None
    if age > QUOTA_STALE_S:
        return None
    pct = data.get("weekly_pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return float(pct)


def _codex(now: float) -> dict | None:
    from llm_router import codex_window

    if not paths.state_path("codex_window.json").exists():
        return None
    s = codex_window.snapshot(now)
    return {"used": s.used, "budget": s.budget, "resets_at": s.resets_at}


def _ns(card: dict) -> dict | None:
    r = card["kpis"]["NS"]
    n = r.get("n")
    if not isinstance(n, int) or n <= 0:
        return None
    pct = None
    if r.get("measurable") and isinstance(r.get("denominator"), int) and r["denominator"] > 0:
        pct = round(100.0 * r["numerator"] / r["denominator"], 1)
    return {"pct": pct, "n": n}


def _hooks_slow(card: dict) -> dict | None:
    hooks = (card["kpis"].get("G1_hook") or {}).get("hooks") or {}
    threshold = _slow_threshold_ms()
    worst: tuple[float, str, float] | None = None
    for name, entry in hooks.items():
        p95, budget = entry.get("p95_ms"), entry.get("budget_ms")
        if not isinstance(p95, (int, float)):
            continue  # too few invocations to state a p95
        over_budget = isinstance(budget, (int, float)) and p95 > budget
        over_threshold = name not in _ONCE_PER_SESSION_HOOKS and p95 > threshold
        if not (over_budget or over_threshold):
            continue
        score = p95 / budget if isinstance(budget, (int, float)) and budget > 0 else p95
        if worst is None or score > worst[0]:
            worst = (score, name, float(p95))
    return None if worst is None else {"hook": worst[1], "p95_ms": worst[2]}


def _part(code: str, fn):
    """``fn()``, or ``None`` (unknown) after recording why it failed."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- one part failing must not blank the rest
        _note_failure(code, exc)
        return None


def build(now: float | None = None) -> dict[str, Any]:
    """The cache contents. Each part fails on its own, to ``None``."""
    now_ts = time.time() if now is None else now

    def _mode() -> str:
        from llm_router.enforce_config import resolve_enforce_mode

        return resolve_enforce_mode()

    out: dict[str, Any] = {
        "v": 1, "written_at": now_ts,
        "mode": _part("CHZ-FO-STATUSLINE-MODE", _mode),
        "claude_weekly_pct": _part("CHZ-FO-STATUSLINE-QUOTA", lambda: _quota(now_ts)),
        "codex": _part("CHZ-FO-STATUSLINE-CODEX", lambda: _codex(now_ts)),
        "ns": None, "hooks_slow": None,
    }

    def _card() -> dict:
        from llm_router.commands.kpi import compute_scorecard

        return compute_scorecard(days=7, now=now_ts)

    card = _part("CHZ-FO-STATUSLINE-KPI", _card)
    if card is not None:
        out["ns"] = _part("CHZ-FO-STATUSLINE-NS", lambda: _ns(card))
        out["hooks_slow"] = _part("CHZ-FO-STATUSLINE-HOOKS", lambda: _hooks_slow(card))
    return out


def _note_failure(code: str, exc: BaseException) -> None:
    try:
        from llm_router import failopen

        failopen.record(code, exc)
    except Exception:  # noqa: BLE001 -- accounting of an accounting failure
        return


def write(data: dict) -> Path:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def refresh(now: float | None = None) -> bool:
    """Build and write the cache unless another refresher holds the lock.
    Returns True when this call wrote it."""
    from llm_router.file_lock import exclusive_lock

    with exclusive_lock(paths.state_path(LOCK_NAME), timeout=0.0) as locked:
        if not locked:
            return False
        write(build(now))
        return True


def cmd_statusline(args: list[str]) -> int:
    """``llm-router statusline [--refresh]``: refresh the cache, or print the line."""
    import argparse

    ap = argparse.ArgumentParser(prog="llm-router statusline")
    ap.add_argument("--refresh", action="store_true",
                    help="recompute statusline_cache.json now (the status line starts this itself)")
    parsed = ap.parse_args(args)
    if parsed.refresh:
        # Watchdog: a hung KPI source must not hold the refresh lock forever (every
        # later refresh would back off and the line would read n/a until a kill).
        if hasattr(signal, "SIGALRM"):
            signal.alarm(REFRESH_TIMEOUT_S)
        return 0 if refresh() else 1
    from llm_router import statusline_tick as tick

    print(tick.render(tick.read_cache(tick.router_home()), now=time.time(),
                      env_mode=os.environ.get("LLM_ROUTER_ENFORCE")))
    return 0
