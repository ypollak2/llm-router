"""Outbound-connection guard for ``scripts/synthetic_replay.py`` (installed as ``sitecustomize``).

The replay harness copies this file to ``<scratch>/guard/sitecustomize.py`` and puts that
directory first on ``PYTHONPATH`` of every child it starts (hooks, the proxy), and calls
:func:`install` in its own process for the in-process doors. Python imports
``sitecustomize`` at start-up, so the guard is active before any hook or proxy code runs.

Every ``connect`` / ``connect_ex`` / ``create_connection`` / ``getaddrinfo`` to anything
that is not on the allowlist is refused BEFORE a packet leaves the process: the call
raises ``ConnectionRefusedError`` and one line is appended to the violations file. The
harness fails the run when that file is non-empty, so a door that tries to reach a
provider, an Ollama daemon or a live router port (8787/8797/8798) cannot pass silently.

Configuration (environment, read once at install):
  LLM_ROUTER_REPLAY_ALLOW       comma list of ``host:port`` that may be contacted
  LLM_ROUTER_REPLAY_VIOLATIONS  file that receives one JSON line per refused attempt
Unix-domain sockets are always allowed (no network). Nothing else is.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

_LOCAL_NAMES = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


def _allowed(spec: str) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for item in spec.split(","):
        host, _, port = item.strip().rpartition(":")
        if host and port.isdigit():
            for name in (_LOCAL_NAMES if host in _LOCAL_NAMES else {host}):
                out.add((name, int(port)))
    return out


_VIOLATIONS: list[str] = []


def _record(kind: str, host: object, port: object) -> None:
    path = _VIOLATIONS[0] if _VIOLATIONS else os.environ.get("LLM_ROUTER_REPLAY_VIOLATIONS")
    if not path:
        return
    row = {"ts": round(time.time(), 3), "pid": os.getpid(), "kind": kind,
           "host": str(host)[:200], "port": port if isinstance(port, int) else str(port)[:20],
           "argv0": os.path.basename(sys.argv[0]) if sys.argv else ""}
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def install(allow: str | None = None, violations: str | None = None) -> None:
    """Patch ``socket`` in this process. ``allow`` / ``violations`` default to the
    environment variables above (the ``sitecustomize`` path)."""
    if getattr(socket, "_llm_router_replay_guard", False):
        return
    if violations:
        _VIOLATIONS[:] = [violations]
    allow = _allowed(allow if allow is not None else (os.environ.get("LLM_ROUTER_REPLAY_ALLOW") or ""))
    allowed_hosts = {h for h, _ in allow}
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _ok(sock: socket.socket, address: object) -> bool:
        if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", object()):
            return True
        if isinstance(address, tuple) and len(address) >= 2:
            return (str(address[0]), address[1]) in allow
        return False

    def connect(self, address):  # type: ignore[no-untyped-def]
        if not _ok(self, address):
            host, port = (address[0], address[1]) if isinstance(address, tuple) else (address, "")
            _record("connect", host, port)
            raise ConnectionRefusedError(f"replay guard: outbound connection to {address!r} refused")
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        if not _ok(self, address):
            host, port = (address[0], address[1]) if isinstance(address, tuple) else (address, "")
            _record("connect_ex", host, port)
            return 111  # ECONNREFUSED
        return real_connect_ex(self, address)

    def getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and str(name) not in _LOCAL_NAMES and str(name) not in allowed_hosts:
            _record("getaddrinfo", name, port)
            raise socket.gaierror(socket.EAI_NONAME, f"replay guard: lookup of {name!r} refused")
        return real_getaddrinfo(host, port, *args, **kwargs)

    _ORIGINALS[:] = [real_connect, real_connect_ex, real_getaddrinfo]
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    socket._llm_router_replay_guard = True  # type: ignore[attr-defined]


_ORIGINALS: list = []


def uninstall() -> None:
    """Restore the real ``socket`` functions (the harness process, after a run)."""
    if not getattr(socket, "_llm_router_replay_guard", False) or not _ORIGINALS:
        return
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = _ORIGINALS  # type: ignore[method-assign]
    socket._llm_router_replay_guard = False  # type: ignore[attr-defined]
    _VIOLATIONS[:] = []


if os.environ.get("LLM_ROUTER_REPLAY_VIOLATIONS"):
    install()
