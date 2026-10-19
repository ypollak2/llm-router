#!/usr/bin/env python3
# llm_router-hook-version: 2
"""Print the session's statusline snapshot line after a Codex turn, without ending its session.

P35-3. The line is the one ``llm-router statusline`` prints as its second row and the Pi summary
extension prints: ``statusline_snapshot`` renders it once, into ``snapshots/codex-<session>.json``,
and every surface only reads that file. This hook therefore does a ``stat`` and one small JSON read,
imports nothing from ``llm_router`` (no ``asyncio``, no sqlite, no ledger sum of its own), and starts a
detached ``llm-router statusline --refresh --host codex --session <id>`` so the NEXT turn has a fresh
line. The hook never waits for it. The budget is 300 ms (``hook_latency.HOOK_BUDGETS_MS``).

A snapshot that is absent, unreadable or older than 15 minutes prints ``n/a``: unknown is never a number.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time

STALE_S = 15 * 60.0          # same bar as statusline_snapshot.STALE_S
REFRESH_MIN_S = 10.0         # at most one refresher started per this many seconds
STAMP_NAME = ".codex-stop-refresh"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_PLAIN_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_NA = "n/a"


def _home() -> str:
    override = os.environ.get("LLM_ROUTER_HOME", "").strip()
    return os.path.expanduser(override) if override else os.path.join(os.path.expanduser("~"), ".llm-router")


def _stop_setting(home: str) -> str:
    """``LLM_ROUTER_STOP_HOOK``: the real environment wins, else the last such line of ``<home>/.env``.
    The package's dotenv loader is not imported here (this hook imports nothing from ``llm_router``), so
    this one knob, the only one the hook has, is read by hand. A ``.env`` that is not a regular file
    (a FIFO would block the open: F15) is ignored."""
    value = os.environ.get("LLM_ROUTER_STOP_HOOK")
    if value is not None:
        return value.strip().lower()
    path = os.path.join(home, ".env")
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return ""
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        print(f"llm-router: codex-stop could not read .env ({type(exc).__name__})", file=sys.stderr)
        return ""
    found = ""
    for raw in lines:
        m = re.match(r"\s*(?:export\s+)?LLM_ROUTER_STOP_HOOK\s*=\s*(.*?)\s*$", raw)
        if m:
            found = m.group(1).strip("\"'").strip().lower()
    return found


def _session_id() -> str | None:
    """The ``session_id`` Codex pipes in, when it is a plain id (it becomes part of a file name)."""
    try:
        raw = sys.stdin.read()
        sid = json.loads(raw).get("session_id") if raw.strip() else None
    except (OSError, ValueError, AttributeError) as exc:
        print(f"llm-router: codex-stop could not read the session id ({type(exc).__name__})", file=sys.stderr)
        return None
    return sid if isinstance(sid, str) and _PLAIN_ID.fullmatch(sid) else None


def _snapshot_path(home: str, session: str) -> str:
    return os.path.join(home, "snapshots", f"codex-{_UNSAFE.sub('_', session)}.json")


def _line(home: str, session: str | None, now: float) -> str:
    if not session:
        return _NA
    try:
        with open(_snapshot_path(home, session), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _NA
    except (OSError, ValueError) as exc:
        print(f"llm-router: codex-stop snapshot unreadable ({type(exc).__name__})", file=sys.stderr)
        return _NA
    if not isinstance(data, dict) or data.get("v", 1) != 1:
        return _NA
    at, line = data.get("written_at"), data.get("line")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not isinstance(line, str) or not line:
        return _NA
    return line if 0 <= now - at <= STALE_S else _NA


def _which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(d, name)
        if d and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _refresh_argv(session: str) -> list[str] | None:
    """``llm-router statusline --refresh --host codex --session S``. ``LLM_ROUTER_STATUSLINE_REFRESH_CMD``
    (shell-quoted) replaces the ``llm-router statusline --refresh`` prefix, as it does for the status line."""
    override = os.environ.get("LLM_ROUTER_STATUSLINE_REFRESH_CMD", "").strip()
    if override:
        import shlex

        try:
            argv = shlex.split(override)
        except ValueError:
            return None
        if argv and os.sep not in argv[0]:
            argv[0] = _which(argv[0]) or argv[0]
    else:
        exe = _which("llm-router")
        argv = [exe, "statusline", "--refresh"] if exe else []
    return argv + ["--host", "codex", "--session", session] if argv else None


def _spawn_detached(argv: list[str]) -> None:
    """Own session, no stdio, not waited for (``os.fork`` + ``execv``: ``subprocess`` costs ~15 ms)."""
    if hasattr(os, "fork") and hasattr(os, "setsid"):
        if os.fork() == 0:  # child: never returns into the hook
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


def _maybe_refresh(home: str, session: str | None, now: float) -> None:
    """Start one detached refresher unless one was started in the last :data:`REFRESH_MIN_S` seconds."""
    if not session:
        return
    stamp = os.path.join(home, STAMP_NAME)
    try:
        if now - os.path.getmtime(stamp) < REFRESH_MIN_S:
            return
    except OSError:
        pass                                   # no stamp yet: due
    argv = _refresh_argv(session)
    if not argv:
        print("llm-router: codex-stop did not refresh the snapshot (llm-router is not on PATH)", file=sys.stderr)
        return
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    os.utime(stamp, (now, now))
    _spawn_detached(argv)


def main() -> None:
    home, now = _home(), time.time()
    if _stop_setting(home) == "disabled":
        return
    session = _session_id()
    message = _line(home, session, now)
    try:
        _maybe_refresh(home, session, now)
    except Exception as exc:  # noqa: BLE001 -- a refresh that cannot start must not cost the turn its line
        print(f"llm-router: codex-stop refresh not started ({type(exc).__name__}: {exc})", file=sys.stderr)
    print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()
