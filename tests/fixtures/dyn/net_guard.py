"""Fail a test that opens a connection to a host that is not loopback."""
from __future__ import annotations

import socket
from contextlib import contextmanager

_LOOPBACK = ("127.", "::1", "localhost")


class ForeignHostConnection(AssertionError):
    """A test tried to reach a non-loopback host."""


@contextmanager
def net_guard():
    """Patch ``socket.socket.connect`` so a non-loopback destination raises.

    Yields the list of attempted foreign hosts (also raised on the spot, because a
    caller's fail-open handler could swallow the raise but not the record).
    """
    attempts: list[str] = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if isinstance(host, str) and not host.startswith(_LOOPBACK) and self.family != getattr(socket, "AF_UNIX", -1):
            attempts.append(host)
            raise ForeignHostConnection(f"net_guard: connection to {host!r} refused")
        return real_connect(self, address)

    socket.socket.connect = guarded
    try:
        yield attempts
    finally:
        socket.socket.connect = real_connect
