"""An append-only JSONL log that cannot grow without bound and never makes a
writer wait.

Two KPI logs share this (``hook_latency.jsonl``, ``provider_bench.jsonl``). One
of them is written from inside every hook invocation, so the write has to cost
next to nothing and must not serialise concurrent hooks:

* the append is ONE ``os.write`` on an ``O_APPEND`` descriptor. POSIX makes that
  atomic with respect to other appenders for a regular file, so concurrent
  processes cannot tear or interleave each other's lines, and nobody takes a
  lock to do it;
* the size cap is checked with the ``fstat`` of the descriptor already open, so
  the check is not a second path lookup;
* rotation is rare (once per ``max_bytes`` of logging), moves the whole file to
  ``<name>.1`` with ``os.replace`` (replacing the previous ``.1``), and is done by
  whichever writer wins a NON-blocking lock -- every other writer skips it and
  carries on. The winner re-checks the size under the lock, so two writers that
  both saw "full" cannot rotate twice and replace a full ``.1`` with a nearly
  empty file.

A line written to the old inode by a writer that opened it before the rotation
lands in ``.1`` and is not lost. Rows older than two generations are dropped;
that is the cap.

Nothing here raises for a missing directory or lock: ``append`` raises only the
``OSError`` of the write itself, for the caller to count.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["append", "read_dicts"]


def append(path: Path, data: bytes, max_bytes: int) -> None:
    """Append ``data`` (one complete line) to ``path``; rotate when it is full."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        # Cold path only: the state directory does not exist yet.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
        size = os.fstat(fd).st_size
    finally:
        os.close(fd)
    if size > max_bytes:
        _rotate(path, max_bytes)


def _rotate(path: Path, max_bytes: int) -> None:
    from llm_router.file_lock import exclusive_lock

    with exclusive_lock(path.with_name(path.name + ".lock"), timeout=0.0) as locked:
        if not locked:
            return
        try:
            if path.stat().st_size <= max_bytes:
                return  # someone rotated while this writer was getting here
        except FileNotFoundError:
            return
        os.replace(path, path.with_name(path.name + ".1"))


def read_dicts(path: Path) -> list[dict]:
    """Every JSON object in both generations, oldest generation first. A
    malformed or half-written line is skipped, never read as an empty row.
    Never raises."""
    rows: list[dict] = []
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows
