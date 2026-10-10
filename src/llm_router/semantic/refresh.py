"""Keep the semantic index current while a session edits files.

PLAN v16 P1.5 task 2 (R-CTX-6). Before this the index was built once, when it was
empty (`autoindex`), and never again: a function written at 14:20 was invisible
to retrieval at 14:21, and the session-start refresh only helps the NEXT session.

The mechanism is a queue, not a watcher:

* A PostToolUse hook for Edit / Write / MultiEdit calls :func:`note_edit` with the
  path. That appends one line to ``semantic_dirty.txt`` next to the index and
  returns. It never parses, never opens the index, never waits.
* Every :data:`THRESHOLD` queued files (default 10) :func:`note_edit` starts ONE
  detached child that runs :func:`run`: drain the queue, then
  `indexer.index_files` on exactly those paths.
* SessionStart starts the same child when anything is still queued, so a short
  session that never reached the threshold is still indexed next time.

Why a queue file and not an in-process list: the hook is a fresh process per
tool call, and the queue must survive a crash, a restart and several concurrent
hook processes. Appending a line is atomic enough on POSIX for that; draining
renames the file first so an edit that lands mid-drain goes to the next batch
rather than being lost.

Fail-open throughout. Nothing here may raise into a hook; a queue that cannot be
written means the file is picked up by the next full index, which is the
behaviour before this module existed.

Off with ``LLM_ROUTER_SEMANTIC_REFRESH=0``. Only projects that already HAVE an
index are queued: building one is `autoindex`'s decision (git repo, not $HOME, not
scratch, size cap), and this module must not create a store for a project that
decision declined.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from llm_router.semantic import store as sstore

DIRTY_NAME = "semantic_dirty.txt"
DEFAULT_THRESHOLD = 10
SPAWN_COOLDOWN_S = 30.0


def _enabled() -> bool:
    return os.environ.get("LLM_ROUTER_SEMANTIC_REFRESH", "").strip().lower() not in (
        "0", "off", "false", "no")


def threshold() -> int:
    try:
        return max(1, int(os.environ.get("LLM_ROUTER_SEMANTIC_REFRESH_EVERY", DEFAULT_THRESHOLD)))
    except ValueError:
        return DEFAULT_THRESHOLD


def dirty_path(root: Path | str | None = None, base: Path | None = None) -> Path:
    return sstore.index_path(root, base).parent / DIRTY_NAME


def _indexable_suffixes() -> frozenset[str]:
    from llm_router.semantic.indexer import _LANGUAGES
    return frozenset(_LANGUAGES)


def _relative(scope: Path, file_path: str) -> str | None:
    """*file_path* as a path relative to the project, or None if it is outside it."""
    try:
        p = Path(file_path)
        if not p.is_absolute():
            p = scope / p
        return p.resolve().relative_to(scope.resolve()).as_posix()
    except (OSError, ValueError, RuntimeError):
        return None


def pending(root: Path | str | None = None, base: Path | None = None) -> list[str]:
    """Queued paths, de-duplicated, in first-seen order. Does not drain."""
    try:
        lines = dirty_path(root, base).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return list(dict.fromkeys(line.strip() for line in lines if line.strip()))


def mark_dirty(file_path: str, root: Path | str | None = None,
               base: Path | None = None) -> bool:
    """Queue *file_path*. True when it was written to the queue.

    Declines (False) for: refresh off, a project with no index, a path outside the
    project, or a suffix no extractor handles.
    """
    if not _enabled():
        return False
    from llm_router.semantic.scope import resolve_scope
    scope = resolve_scope(root)
    if not sstore.index_path(scope, base).exists():
        return False
    rel = _relative(scope, file_path)
    if rel is None or Path(rel).suffix not in _indexable_suffixes():
        return False
    q = dirty_path(scope, base)
    try:
        with open(q, "a", encoding="utf-8") as fh:
            fh.write(rel + "\n")
    except OSError:
        return False
    return True


def drain(root: Path | str | None = None, base: Path | None = None) -> list[str]:
    """Take the queue. The file is renamed first, so a concurrent append is kept."""
    q = dirty_path(root, base)
    taken = q.with_name(q.name + f".{os.getpid()}.taking")
    try:
        os.replace(q, taken)
    except OSError:
        return []
    try:
        lines = taken.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    finally:
        try:
            taken.unlink()
        except OSError:
            pass
    return list(dict.fromkeys(line.strip() for line in lines if line.strip()))


def run(root: Path | str | None = None, base: Path | None = None):
    """Drain the queue and re-index those files. The detached child's entry point."""
    from llm_router.semantic import indexer
    paths = drain(root, base)
    if not paths:
        return None
    return indexer.index_files(paths, root=root, base=base)


def _spawn_child(root: Path) -> None:
    script = (
        "from pathlib import Path; from llm_router.semantic import refresh; "
        f"refresh.run(Path({str(root)!r}))"
    )
    # The child only re-indexes files; it needs the interpreter's own environment
    # and LLM_ROUTER_* (state dir), never the operator's provider keys.
    keep = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH",
            "VIRTUAL_ENV", "SYSTEMROOT")
    env = {k: v for k, v in os.environ.items()
           if k in keep or k.startswith("LLM_ROUTER_")}
    subprocess.Popen(
        [sys.executable, "-c", script], cwd=str(root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _claim_spawn(lock: Path, cooldown: float) -> bool:
    """True if no child was started within *cooldown* seconds; claims the slot."""
    try:
        if time.time() - lock.stat().st_mtime < cooldown:
            return False
    except OSError:
        pass
    try:
        lock.write_text(str(time.time()))
    except OSError:
        return False
    return True


def maybe_spawn(
    root: Path | str | None = None,
    base: Path | None = None,
    *,
    minimum: int | None = None,
    spawn: Callable[[Path], None] | None = None,
) -> bool:
    """Start the background re-index when at least *minimum* files are queued.

    ``minimum`` defaults to :func:`threshold`; SessionStart passes 1. Returns
    True only when a child was actually started. ``spawn`` is injectable so a test
    never forks.
    """
    if not _enabled():
        return False
    from llm_router.semantic.scope import resolve_scope
    scope = resolve_scope(root)
    need = threshold() if minimum is None else minimum
    if len(pending(scope, base)) < need:
        return False
    lock = dirty_path(scope, base).with_name(DIRTY_NAME + ".spawn")
    if not _claim_spawn(lock, SPAWN_COOLDOWN_S):
        return False
    try:
        (spawn or _spawn_child)(scope)
    except Exception:  # noqa: BLE001 - fail-open; the queue stays for next time
        return False
    return True


def note_edit(
    file_path: str,
    root: Path | str | None = None,
    base: Path | None = None,
    *,
    spawn: Callable[[Path], None] | None = None,
) -> bool:
    """The hook's one call: queue the edited path, spawn at the threshold.

    Never raises. Returns True when the path was queued.
    """
    try:
        queued = mark_dirty(file_path, root, base)
        if queued:
            maybe_spawn(root, base, spawn=spawn)
        return queued
    except Exception:  # noqa: BLE001
        return False
