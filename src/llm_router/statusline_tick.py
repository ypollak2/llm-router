"""The status line's per-tick renderer. STDLIB ONLY, and it must stay that way.

Claude Code runs the ``statusLine`` command about once a second. This file is
what that command executes in debug mode (``LLM_ROUTER_STATUSLINE=fast``;
``hooks/statusline-command.sh`` execs it; the installer copies it next to the
hooks as ``llm_router_statusline_tick.py``), so everything here is on a
one-second loop:

* it never imports ``llm_router`` (the package import alone costs more than the
  whole tick budget) and never computes a KPI. It reads ONE small cache file,
  ``statusline_cache.json``, that :mod:`llm_router.statusline_refresh` writes,
  and the Claude quota snapshot ``usage.json`` (session 5h / weekly / Sonnet %,
  the numbers ``llm-router status`` prints), which the hooks keep fresh. Quota
  is never fetched from here;
* when that cache is missing or older than :data:`REFRESH_AFTER_S` it starts the
  refresher DETACHED and does not wait for it. At most one start per
  :data:`REFRESH_AFTER_S` (a timestamp file, not a lock: a hung refresher holds
  nothing a tick waits on), so a stuck refresh cannot delay a tick or pile up;
* a value it does not have is printed as ``n/a``, never as 0. A cache older than
  :data:`STALE_AFTER_S` is not data: every cached field reads ``n/a``. A quota
  snapshot older than ``LLM_ROUTER_USAGE_TTL_SEC`` (default 300 s, the same TTL
  as the full layout's ``°`` marker) is still shown, with ``(stale <age>)``;
* it reads nothing from stdin but drains it (Claude Code pipes the session JSON
  and times out a command that leaves the pipe full). The session JSON carries
  no prompt text today, and nothing from it is printed either way.

Output: one line, at most :data:`MAX_CHARS` characters (counted without the
colour codes), e.g.::

    llm-router · smart · NS 0.0% n=3599 · Claude 5h 12% wk 41% sonnet 3% · Codex 7/15 ↻21:14 · ⚠ hooks p95 3.0s auto-route
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

# subprocess / shlex are imported only on the rare path that needs them: on this
# machine's python3 they add ~15 ms to a tick, and the bar is 100 ms.

CACHE_NAME = "statusline_cache.json"
#: The Claude quota snapshot the hooks write (``llm-router status`` reads it too).
USAGE_NAME = "usage.json"
#: Past this age a quota snapshot is shown with a stale marker (the full
#: layout's ``LLM_ROUTER_USAGE_TTL_SEC`` default).
DEFAULT_USAGE_TTL_S = 300.0
#: (usage.json key, label), in the order ``llm-router status`` lists them.
_QUOTA_FIELDS = (("session_pct", "5h"), ("weekly_pct", "wk"), ("sonnet_pct", "sonnet"))
REFRESH_STAMP_NAME = ".statusline-refresh.last"
#: The cache is refreshed in the background at most this often.
REFRESH_AFTER_S = 60.0
#: Past this age the cached values are not shown at all ("n/a").
STALE_AFTER_S = 15 * 60.0
MAX_CHARS = 200
#: Longest model / hook name the line will carry before it is cut.
_NAME_MAX = 24

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SAFE = re.compile(r"[^A-Za-z0-9._:/+-]")


def router_home() -> str:
    """``LLM_ROUTER_HOME`` or ``~/.llm-router`` (mirrors ``llm_router.paths``)."""
    override = os.environ.get("LLM_ROUTER_HOME", "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".llm-router")


def read_cache(home: str) -> dict | None:
    try:
        with open(os.path.join(home, CACHE_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_usage(home: str) -> dict | None:
    try:
        with open(os.path.join(home, USAGE_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def usage_ttl() -> float:
    raw = os.environ.get("LLM_ROUTER_USAGE_TTL_SEC", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_USAGE_TTL_S
    return value if value > 0 else DEFAULT_USAGE_TTL_S


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _quota(usage: dict | None, now: float, ttl: float) -> str:
    """``Claude 5h 12% wk 41% sonnet 3%`` from a ``usage.json`` snapshot.

    Unknown is ``n/a``, never 0: no file, the install placeholder (``pending``),
    the failed-fetch snapshot (``is_fallback``: invented 50s), or a field that is
    not a number. A snapshot past ``ttl`` is still shown, marked ``(stale <age>)``;
    one with no ``updated_at`` has an unknown age and is marked ``(stale)``.
    """
    if not isinstance(usage, dict) or usage.get("pending") or usage.get("is_fallback"):
        return "Claude n/a"
    values = [(label, _num(usage.get(key))) for key, label in _QUOTA_FIELDS]
    if all(v is None for _, v in values):
        return "Claude n/a"
    seg = "Claude " + " ".join(f"{label} {v:.0f}%" if v is not None else f"{label} n/a"
                               for label, v in values)
    updated = _num(usage.get("updated_at"))
    if updated is None or updated <= 0:
        return seg + " (stale)"
    if now - updated > ttl:
        seg += f" (stale {_age(now - updated)})"
    return seg


def _num(value) -> float | None:
    """A real number, or None. ``True`` is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return float(value)


def _name(value) -> str:
    """A model / hook name made safe for a terminal line: no escapes, bounded."""
    text = _SAFE.sub("", str(value))[:_NAME_MAX]
    return text or "?"


def _mode_label(raw: str | None) -> str:
    if not raw:
        return "n/a"
    mode = raw.strip().lower()
    if mode in ("off", "shadow", "observe"):
        return "off"
    return _name(mode)


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def render(cache: dict | None, *, now: float, env_mode: str | None = None, color: bool = False,
           usage: dict | None = None, usage_ttl_s: float = DEFAULT_USAGE_TTL_S) -> str:
    """The status line for ``cache`` and the quota snapshot ``usage`` at ``now``. Pure: no I/O.

    ``env_mode`` is ``LLM_ROUTER_ENFORCE`` from this process's environment; it
    wins over the cached mode exactly as ``enforce_config`` lets it win.
    """
    fresh = None
    if isinstance(cache, dict):
        written = _num(cache.get("written_at"))
        if written is not None and 0 <= now - written <= STALE_AFTER_S:
            fresh = cache

    parts: list[str] = ["llm-router"]

    parts.append(_mode_label(env_mode or (fresh or {}).get("mode")))

    ns = (fresh or {}).get("ns") if fresh else None
    ns_pct = _num(ns.get("pct")) if isinstance(ns, dict) else None
    ns_n = _num(ns.get("n")) if isinstance(ns, dict) else None
    if ns_pct is not None and ns_n is not None and ns_n > 0:
        parts.append(f"NS {ns_pct:.1f}% n={int(ns_n)}")
    elif ns_n is not None and ns_n > 0:
        parts.append(f"NS n/a n={int(ns_n)}")  # counted, too few to state a share
    else:
        parts.append("NS n/a")

    parts.append(_quota(usage, now, usage_ttl_s))

    codex = (fresh or {}).get("codex") if fresh else None
    used = _num(codex.get("used")) if isinstance(codex, dict) else None
    limit = _num(codex.get("budget")) if isinstance(codex, dict) else None
    if used is not None and limit is not None:
        seg = f"Codex {int(used)}/{int(limit)}"
        resets = _num(codex.get("resets_at"))
        if resets is not None and resets > now:
            seg += f" ↻{_clock(resets)}"
        parts.append(seg)
    else:
        parts.append("Codex n/a")

    slow = (fresh or {}).get("hooks_slow") if fresh else None
    if isinstance(slow, dict):
        p95 = _num(slow.get("p95_ms"))
        if p95 is not None:
            mark = f"⚠ hooks p95 {p95 / 1000:.1f}s {_name(slow.get('hook', '?'))}"
            parts.append(f"\x1b[33m{mark}\x1b[0m" if color else mark)

    line = " · ".join(parts)
    return _fit(line)


def _fit(line: str) -> str:
    """Cut to :data:`MAX_CHARS` visible characters, never mid-escape."""
    if len(_ANSI.sub("", line)) <= MAX_CHARS:
        return line
    plain = _ANSI.sub("", line)
    return plain[: MAX_CHARS - 1] + "…"


def _which(name: str) -> str | None:
    """``shutil.which`` without importing shutil (~10 ms on this python3)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(d, name)
        if d and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _refresh_argv() -> list[str] | None:
    """The refresher's command line. ``LLM_ROUTER_STATUSLINE_REFRESH_CMD`` (a
    shell-quoted string) overrides it, for tests and unusual installs; otherwise
    ``llm-router statusline --refresh`` found on PATH, or nothing at all."""
    override = os.environ.get("LLM_ROUTER_STATUSLINE_REFRESH_CMD", "").strip()
    if override:
        import shlex

        try:
            argv = shlex.split(override)
        except ValueError:
            return None
        if argv and os.sep not in argv[0]:
            argv[0] = _which(argv[0]) or argv[0]
        return argv or None
    exe = _which("llm-router")
    return [exe, "statusline", "--refresh"] if exe else None


def _spawn_detached(argv: list[str]) -> None:
    """Start ``argv`` in its own session with no stdio, and do not wait for it.

    ``os.fork`` + ``execv`` where it exists: importing ``subprocess`` costs ~15 ms
    a tick on this machine's python3, which is the whole margin of a 100 ms bar."""
    if hasattr(os, "fork") and hasattr(os, "setsid"):
        pid = os.fork()
        if pid == 0:  # child: never returns into the caller
            try:
                os.setsid()
                null = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                os.execv(argv[0], argv)
            finally:
                os._exit(127)
        return
    import subprocess  # Windows: no fork

    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: S603
                     stderr=subprocess.DEVNULL, close_fds=True)


def maybe_refresh(home: str, cache: dict | None, now: float) -> bool:
    """Start one detached refresh when the cache is due. Never waits, never raises.

    Returns True when a refresher was started."""
    try:
        written = _num((cache or {}).get("written_at"))
        if written is not None and 0 <= now - written < REFRESH_AFTER_S:
            return False
        stamp = os.path.join(home, REFRESH_STAMP_NAME)
        try:
            if now - os.path.getmtime(stamp) < REFRESH_AFTER_S:
                return False  # one was started recently; it may still be running
        except OSError:
            pass
        argv = _refresh_argv()
        if not argv:
            return False
        os.makedirs(home, mode=0o700, exist_ok=True)
        fd = os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.utime(stamp, (now, now))
        _spawn_detached(argv)
        return True
    except Exception:  # noqa: BLE001 -- a status line never fails over a refresh
        return False


def main() -> int:
    try:
        sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        pass
    now = time.time()
    home = router_home()
    cache = read_cache(home)
    maybe_refresh(home, cache, now)
    color = not os.environ.get("NO_COLOR")
    sys.stdout.write(render(cache, now=now, env_mode=os.environ.get("LLM_ROUTER_ENFORCE"), color=color,
                            usage=read_usage(home), usage_ttl_s=usage_ttl()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
