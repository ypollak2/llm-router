"""W0-4 / PRD S7 -- state files are 0600 and state directories 0700, through one helper.

Audit 2026-10-25: 365 of 2,551 top-level files in a live state directory were group- or
world-readable (``proxy_calls.jsonl``, ``sessions.db`` among them). The directory itself was
0700, so this was a literal S7 violation rather than an exposure -- but the requirement is
explicit, and a copied or backed-up directory loses the parent's protection.

Why files drift: ``open(p, "a")`` creates at ``0666 & ~umask`` (0644), and ``mkdir`` at 0755.
Fixing writers one by one has failed before, so this module is the single place that knows how:

* :func:`private_dir` -- create a directory (and missing parents) at 0700, umask-safe.
* :func:`open_private` -- ``open()`` that creates at 0600 and tightens an existing file.
* :func:`ensure_private_file` -- create-or-tighten, for a file another library will open
  (sqlite). SQLite gives ``-wal``, ``-shm`` and ``-journal`` the mode of the main database
  file, so a 0600 main file means 0600 sidecars; :func:`connect_sqlite` also tightens sidecars
  that already exist.
* :func:`harden_tree` / :func:`migrate_once` -- one-time repair of files written by older
  versions. Never follows a symlink, never touches anything outside the root, records COUNTS
  only (no file names, no content).
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from pathlib import Path

from llm_router import paths

FILE_MODE = 0o600
DIR_MODE = 0o700
MARKER_NAME = ".modes_migrated_v1"
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _tighten(path: str | os.PathLike, want: int, keep_owner_x: bool = False) -> bool:
    """chmod ``path`` to ``want`` if it grants group/other anything. True when changed.
    A symlink is never followed. Never raises."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return False
        cur = stat.S_IMODE(st.st_mode)
        target = want | (cur & 0o100 if keep_owner_x else 0)
        if cur == target:
            return False
        os.chmod(path, target)
        return True
    except OSError:
        return False


def private_dir(path: str | os.PathLike) -> Path:
    """``mkdir -p`` where every directory THIS call creates is 0700 (umask cannot widen it).
    Pre-existing directories are left as they are (``harden_tree`` repairs them)."""
    p = Path(path)
    missing = []
    cur = p
    while not cur.exists() and cur != cur.parent:
        missing.append(cur)
        cur = cur.parent
    for d in reversed(missing):
        try:
            os.mkdir(d, DIR_MODE)
        except FileExistsError:
            continue
        _tighten(d, DIR_MODE)  # mkdir applies mode & ~umask; a 0 umask would still give 0700
    return p


def open_private(path: str | os.PathLike, mode: str = "a", **kwargs):
    """``open()`` for a state file: parent dir 0700 if created, file created at 0600
    (no 0644 window), and an existing file tightened. Text or binary per ``mode``."""
    p = Path(path)
    if any(c in mode for c in "wax+"):
        private_dir(p.parent)
    fh = open(p, mode, opener=paths.private_opener, **kwargs)
    _tighten(p, FILE_MODE, keep_owner_x=False)
    return fh


def ensure_private_file(path: str | os.PathLike) -> Path:
    """Create ``path`` empty at 0600 if absent; tighten it (and any sqlite sidecars) if present."""
    p = Path(path)
    private_dir(p.parent)
    try:
        os.close(os.open(p, os.O_RDONLY | os.O_CREAT, FILE_MODE))
    except OSError:
        return p
    _tighten(p, FILE_MODE)
    for suffix in _SQLITE_SIDECARS:
        _tighten(str(p) + suffix, FILE_MODE)
    return p


def connect_sqlite(path: str | os.PathLike, **kwargs) -> sqlite3.Connection:
    """``sqlite3.connect`` with the database pre-created at 0600 so ``-wal``/``-shm`` inherit it."""
    if str(path) in ("", ":memory:") or str(path).startswith("file:"):
        return sqlite3.connect(str(path), **kwargs)  # in-memory / URI: no file for us to create
    p = ensure_private_file(path)
    conn = sqlite3.connect(str(p), **kwargs)
    for suffix in _SQLITE_SIDECARS:  # created by the first write, mode copied from the main file
        _tighten(str(p) + suffix, FILE_MODE)
    return conn


def harden_tree(root: str | os.PathLike | None = None) -> dict[str, int]:
    """Tighten every regular file under ``root`` (default: the state dir) to 0600 and every
    directory to 0700. Symlinks are skipped and never followed; nothing outside ``root`` is
    touched. An owner-execute bit on a file is kept. Returns counts only."""
    base = Path(root) if root is not None else paths.llm_router_home()
    counts = {"files_scanned": 0, "files_fixed": 0, "dirs_scanned": 0, "dirs_fixed": 0, "symlinks_skipped": 0}
    try:
        if os.path.islink(base) or not base.is_dir():
            return counts
    except OSError:
        return counts
    stack = [str(base)]
    while stack:
        d = stack.pop()
        counts["dirs_scanned"] += 1
        if _tighten(d, DIR_MODE):
            counts["dirs_fixed"] += 1
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    counts["symlinks_skipped"] += 1
                elif e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    counts["files_scanned"] += 1
                    if _tighten(e.path, FILE_MODE, keep_owner_x=True):
                        counts["files_fixed"] += 1
            except OSError:
                continue
    return counts


def migrate_once(root: str | os.PathLike | None = None) -> dict[str, int] | None:
    """Run :func:`harden_tree` at most once per state directory (marker file, counts only).
    Returns the counts when it ran, ``None`` when the marker already exists or the state
    directory does not exist yet. Never raises."""
    try:
        base = Path(root) if root is not None else paths.llm_router_home()
        if not base.is_dir() or os.path.islink(base):
            return None
        marker = base / MARKER_NAME
        if marker.exists():
            return None
        counts = harden_tree(base)
        counts["ts"] = int(time.time())
        with open_private(marker, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(counts))
        return counts
    except Exception:  # noqa: BLE001 -- housekeeping must never break a caller
        return None
