"""Abort primitive for a local-model call whose holder was asked to yield (P12).

Mechanism only. ``AbortHandle`` lets the holder's own process cancel its in-flight HTTP call by
closing the socket; ``watch_yield`` sets it when ``Admission.should_yield()`` turns true.
This does NOT claim the model server frees its slot when the client disconnects (that is a
server property, measured separately), and it cannot abort a holder in another process: that
holder is only signalled through ``should_yield()``.
"""
from __future__ import annotations

import contextlib
import http.client
import threading
from urllib.parse import urlsplit

from llm_router.toolkit.local_gate import POLL_S, Admission

__all__ = ["AbortHandle", "LocalCallAborted", "abortable_post", "watch_yield"]


class LocalCallAborted(RuntimeError):
    """The in-flight local call was aborted through its AbortHandle."""


class AbortHandle:
    """A cancel event plus the closers (sockets, clients) that unblock the call when it is set."""

    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._closers: list = []

    @property
    def aborted(self) -> bool:
        return self.event.is_set()

    def register(self, closer) -> None:
        """Run ``closer()`` on abort. If already aborted, run it now."""
        with self._lock:
            if not self.event.is_set():
                self._closers.append(closer)
                return
        with contextlib.suppress(Exception):
            closer()

    def abort(self) -> None:
        with self._lock:
            self.event.set()
            closers, self._closers = self._closers, []
        for closer in closers:
            with contextlib.suppress(Exception):
                closer()


@contextlib.contextmanager
def watch_yield(adm: Admission, handle: AbortHandle, poll_s: float = POLL_S * 4):
    """While the block runs, abort ``handle`` as soon as ``adm.should_yield()`` is true."""
    stop = threading.Event()

    def _poll() -> None:
        while not stop.wait(poll_s):
            if adm.should_yield():
                handle.abort()
                return

    t = threading.Thread(target=_poll, name="local-gate-yield-watch", daemon=True)
    t.start()
    try:
        yield handle
    finally:
        stop.set()
        t.join(timeout=1.0)


def abortable_post(url: str, body: bytes, handle: AbortHandle, *, timeout: float = 600.0,
                   headers: dict | None = None) -> tuple[int, bytes]:
    """POST ``body`` to ``url`` over http.client; ``handle.abort()`` closes the socket mid-call.

    Returns (status, body). Raises LocalCallAborted when the handle was aborted."""
    parts = urlsplit(url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname, parts.port, timeout=timeout)

    def _kill() -> None:
        sock = conn.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                import socket
                sock.shutdown(socket.SHUT_RDWR)
        conn.close()

    handle.register(_kill)
    try:
        if handle.aborted:
            raise LocalCallAborted("aborted before send")
        conn.request("POST", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                     body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, http.client.HTTPException) as exc:
        if handle.aborted:
            raise LocalCallAborted("local call aborted") from exc
        raise
    finally:
        conn.close()
