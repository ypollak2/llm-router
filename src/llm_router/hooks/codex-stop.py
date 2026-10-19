#!/usr/bin/env python3
# llm_router-hook-version: 3
"""Print the session's statusline snapshot line after a Codex turn, without ending its session.

P35-3. The line is the one ``llm-router statusline`` prints as its second row and the Pi summary
extension prints: ``statusline_snapshot`` renders it once, into ``snapshots/codex-<session>.json``,
and every surface only reads that file. This hook therefore does a ``stat`` and one small JSON read,
imports nothing from ``llm_router`` (no ``asyncio``, no sqlite, no ledger sum of its own), and starts a
detached ``llm-router statusline --refresh --host codex --session <id>`` so the NEXT turn has a fresh
line. The hook never waits for it. The budget is 300 ms (``hook_latency.HOOK_BUDGETS_MS``).

A snapshot that is absent, unreadable or older than 15 minutes prints ``n/a``: unknown is never a number.

F111. The state home is resolved the way the package does (``env_loader``: the real environment first, then
the first of ``~/.llm-router/.env`` and ``~/.env`` that has the key, parsed as ``env_loader`` parses it);
the package's loader is not imported (import cost), so its two keys (``LLM_ROUTER_HOME``,
``LLM_ROUTER_STOP_HOOK``) are read by hand with a parser pinned to it by tests/test_codex_stop.py. The refresh stamp is one small file per session. The hook
appends its own G1 row to ``hook_latency.jsonl`` (same fields as ``hook_latency.record``; the recorder is an
``llm_router`` import, so the one-``os.write`` append is repeated here).
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time

_T0 = time.monotonic()       # F111: the G1 row's clock starts before anything else runs
STALE_S = 15 * 60.0          # same bar as statusline_snapshot.STALE_S
REFRESH_MIN_S = 10.0         # at most one refresher started per this many seconds
STAMP_DIR = ".codex-stop-refresh.d"   # F111: one stamp file per session (a dir: the v2 stamp was a file)
STAMP_KEEP_S = 3600.0        # a stamp this old is cleaned up
STAMP_MAX = 64               # ... and never more than this many stamp files
LATENCY_NAME = "hook_latency.jsonl"
LATENCY_MAX_BYTES = 4 * 1024 * 1024   # hook_latency._DEFAULT_MAX_BYTES
HOST_TIMEOUT_MS = 60_000     # hook_latency.DEFAULT_TIMEOUT_MS
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_PLAIN_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_NA = "n/a"


def _parse_env(path: str) -> dict[str, str]:
    """``env_loader._parse_env_file``, line for line: ``KEY=value`` lines (the last one of a key wins);
    blank lines, ``#`` comments and lines with no ``=`` are skipped; the value is stripped, then stripped of
    surrounding quote characters; ``export KEY=...`` is NOT understood (the key becomes ``export KEY``), so
    the hook and the package never read different homes. Only a regular file is opened (a FIFO would block:
    F15); an unreadable or binary file gives ``{}`` as in the package."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return {}
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"llm-router: codex-stop could not read .env ({type(exc).__name__})", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip().strip("\"'")
    return out


def _env_files() -> list[str]:
    """The ``.env`` files ``env_loader.candidate_env_paths`` lists after the working directory's (project
    content, not consulted here): ``paths.state_path(".env")`` -- computed from the REAL environment's
    ``LLM_ROUTER_HOME`` before any file is loaded, so a home set inside a ``.env`` does not move this file --
    then ``~/.env``."""
    user = os.path.expanduser("~")
    real = os.environ.get("LLM_ROUTER_HOME", "").strip()
    base = os.path.expanduser(real) if real else os.path.join(user, ".llm-router")
    return [os.path.join(base, ".env"), os.path.join(user, ".env")]


def _setting(key: str, real: str | None) -> str | None:
    """``os.environ[key]`` after ``load_dotenv_files``; ``real`` is what the caller read from the real
    environment (``os.environ.get``: ``None`` only when unset). The real environment (an empty value counts as set)
    wins, else the first file that has the key (an empty value counts as set and blocks later files).
    ``None`` when nothing sets it."""
    if real is not None:
        return real
    for env_file in _env_files():
        values = _parse_env(env_file)
        if key in values:
            return values[key]
    return None


def _home() -> str:
    """Same answer as ``paths.llm_router_home()`` after ``env_loader``: a blank value is the default home."""
    override = (_setting("LLM_ROUTER_HOME", os.environ.get("LLM_ROUTER_HOME")) or "").strip()
    return os.path.expanduser(override) if override else os.path.join(os.path.expanduser("~"), ".llm-router")


def _stop_setting() -> str:
    """``LLM_ROUTER_STOP_HOOK``, read from exactly the sources the package reads it from (:func:`_setting`).
    Not from ``<configured home>/.env``: the package does not look there either."""
    return (_setting("LLM_ROUTER_STOP_HOOK", os.environ.get("LLM_ROUTER_STOP_HOOK")) or "").strip().lower()


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


def _clean_stamps(stamp_dir: str, now: float) -> None:
    """Drop stamps older than :data:`STAMP_KEEP_S`, then the oldest beyond :data:`STAMP_MAX`."""
    entries = []
    for name in os.listdir(stamp_dir):
        path = os.path.join(stamp_dir, name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    entries.sort()
    keep = [e for e in entries if now - e[0] <= STAMP_KEEP_S]
    doomed = [e[1] for e in entries if now - e[0] > STAMP_KEEP_S] + [e[1] for e in keep[:max(0, len(keep) - STAMP_MAX)]]
    for path in doomed:
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue                           # another session's hook removed it first
        except OSError as exc:
            print(f"llm-router: codex-stop could not remove a stale stamp ({type(exc).__name__})", file=sys.stderr)


def _maybe_refresh(home: str, session: str | None, now: float) -> None:
    """Start one detached refresher per session unless that session started one in the last
    :data:`REFRESH_MIN_S` seconds (F111: the stamp was per home, so a second session starved)."""
    if not session:
        return
    stamp_dir = os.path.join(home, STAMP_DIR)
    stamp = os.path.join(stamp_dir, "s-" + _UNSAFE.sub("_", session))
    try:
        if now - os.path.getmtime(stamp) < REFRESH_MIN_S:
            return
    except OSError:
        pass                                   # no stamp yet: due
    argv = _refresh_argv(session)
    if not argv:
        print("llm-router: codex-stop did not refresh the snapshot (llm-router is not on PATH)", file=sys.stderr)
        return
    os.makedirs(stamp_dir, mode=0o700, exist_ok=True)
    os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    os.utime(stamp, (now, now))
    try:
        _clean_stamps(stamp_dir, now)
    except OSError as exc:
        print(f"llm-router: codex-stop stamp cleanup failed ({type(exc).__name__})", file=sys.stderr)
    _spawn_detached(argv)


def _record_latency(home: str, session: str | None, elapsed_ms: float) -> None:
    """Append one G1 row, compatible with ``hook_latency.record``. One ``os.write`` on an ``O_APPEND``
    descriptor (atomic, no lock). This hook does NOT rotate (rotation needs the package): a log already over
    the cap is left for the other hooks (the writers that import ``hook_latency``) to rotate, and this one
    writes nothing. If codex-stop is the only writer in a home, its rows stop at the cap (about 4 MiB) and
    stay stopped: bounded, but a known loss, accepted to keep the hook import-free."""
    if os.environ.get("LLM_ROUTER_HOOK_LATENCY", "").strip().lower() in ("0", "off", "false", "no", "disabled"):
        return
    try:
        cap = int(os.environ.get("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", "").strip())
    except ValueError:
        cap = 0
    cap = cap if cap > 0 else LATENCY_MAX_BYTES
    row = {"hook": "codex-stop", "event": "Stop", "elapsed_ms": round(elapsed_ms, 1),
           "timed_out": elapsed_ms >= HOST_TIMEOUT_MS, "ts": round(time.time(), 3)}
    if session:
        row["session_id"] = session
    row["host"] = "codex"
    try:
        row["load1"] = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        pass
    path = os.path.join(home, LATENCY_NAME)
    os.makedirs(home, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.fstat(fd).st_size <= cap:
            os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def main() -> None:
    home, now = _home(), time.time()
    session = None
    try:
        if _stop_setting() == "disabled":
            return
        session = _session_id()
        message = _line(home, session, now)
        try:
            _maybe_refresh(home, session, now)
        except Exception as exc:  # noqa: BLE001 -- a refresh that cannot start must not cost the turn its line
            print(f"llm-router: codex-stop refresh not started ({type(exc).__name__}: {exc})", file=sys.stderr)
        print(json.dumps({"systemMessage": message}))
        sys.stdout.flush()
    finally:
        try:
            _record_latency(home, session, (time.monotonic() - _T0) * 1000.0)
        except Exception as exc:  # noqa: BLE001 -- timing must never cost the turn its line
            print(f"llm-router: codex-stop latency not recorded ({type(exc).__name__})", file=sys.stderr)


if __name__ == "__main__":
    main()
