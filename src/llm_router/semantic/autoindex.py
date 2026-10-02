"""Index a project automatically on first use.

Owner decision (``semantic_audit/REPORT.md`` follow-up): when routing or
retrieval sees a project root with no semantic index, or one with zero
indexed files, it should not silently run empty-handed forever — it should
start one. :func:`maybe_start_background_index` is that start: a detached,
size-capped ``llm-router semantic index``, reusing the exact code path
``llm-router semantic index`` runs by hand (:mod:`llm_router.semantic.indexer`
via the CLI), and never blocking the call that noticed.

GUARDS, each with the reason it exists
---------------------------------------

* **git repos only.** ``resolve_scope()`` always answers, falling back to the
  cwd itself when no ``.git`` is found. The MCP server's cwd is commonly
  ``$HOME`` — indexing a non-repo directory would not just waste a build, it
  would seed a shared bucket with whatever happens to live there.
* **never ``$HOME``, stated explicitly.** A user's home directory can itself
  be a git repo (dotfiles), so the git check alone would not catch it.
* **a tracked-file-count cap.** ``git ls-files`` on a monorepo can return six
  figures. Indexing that in the background on every session is not what
  "automatic" was asked for; a repo over the cap is left for
  ``llm-router semantic index`` to be run by hand. Documented and overridable
  via ``LLM_ROUTER_SEMANTIC_AUTOINDEX_MAX_FILES``.
* **a cooldown lock.** Two concurrent sessions, or two retrievals in the same
  session, must not both spawn a build. The lock is a timestamp file, not a
  PID file — a PID can be reused, and this only needs to know how recently a
  build was last STARTED, not whether it is still running.
* **behind an env flag, default ON** (``LLM_ROUTER_SEMANTIC_AUTOINDEX``), so an
  operator who wants no background process ever can turn it off.

Every failure path here is a no-op plus a :mod:`llm_router.failopen` record —
this runs on the hot path of every retrieval and must never slow or break the
call that triggered it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["maybe_start_background_index", "index_is_empty"]

_DEFAULT_MAX_FILES = 20_000
_DEFAULT_COOLDOWN_S = 3600.0
_FAILOPEN_CODE = "CHZ-FO-SEMANTIC-AUTOINDEX"


def _enabled() -> bool:
    return os.environ.get("LLM_ROUTER_SEMANTIC_AUTOINDEX", "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _max_files() -> int:
    raw = os.environ.get("LLM_ROUTER_SEMANTIC_AUTOINDEX_MAX_FILES", "").strip()
    try:
        return int(raw) if raw else _DEFAULT_MAX_FILES
    except ValueError:
        return _DEFAULT_MAX_FILES


def _cooldown_s() -> float:
    raw = os.environ.get("LLM_ROUTER_SEMANTIC_AUTOINDEX_COOLDOWN_S", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_COOLDOWN_S
    except ValueError:
        return _DEFAULT_COOLDOWN_S


def _lock_path(root: Path, base: "Path | None" = None) -> Path:
    from llm_router import okf

    kwargs: dict = {"root": root}
    if base is not None:
        kwargs["base"] = base
    return okf.project_knowledge_dir(**kwargs) / "semantic" / "autoindex.lock"


def _recently_attempted(lock: Path, cooldown: float) -> bool:
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False  # no lock, or unreadable — never attempted recently
    return age < cooldown


def _claim_lock(lock: Path, cooldown: float) -> bool:
    """Atomically claim the lock so two concurrent callers cannot both win.

    The earlier version checked ``_recently_attempted`` and then, separately,
    wrote the lock file — a classic check-then-act race: N callers that reach
    the check at the same instant (the normal case for an empty index, which
    every one of them sees as eligible) all see "not recently attempted" and
    all write the lock and all spawn a build. Reproduced live: 12 concurrent
    processes against one empty repo spawned 12 builds, every run.

    ``os.open(..., O_CREAT | O_EXCL)`` makes *creating* the file atomic: only
    one caller can create a file that does not yet exist, so only one caller
    gets the lock when it starts out absent — the common case this bug hit.
    A caller that loses the race sees ``FileExistsError`` and must not spawn.

    A stale lock (older than the cooldown) is taken over with a narrower,
    non-atomic unlink-then-retry: two callers could both win that handoff and
    both spawn, but that window opens at most once per cooldown period
    (default one hour), not on every call — an acceptable cost for not
    needing a cross-platform file lock.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    now = str(time.time()).encode()
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _recently_attempted(lock, cooldown):
            return False
        try:
            lock.unlink(missing_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return False
    try:
        os.write(fd, now)
    finally:
        os.close(fd)
    return True


def _tracked_file_count(root: Path) -> "int | None":
    """``None`` means "could not tell" — treated as "skip", not as "zero"."""
    from llm_router.safe_subprocess import get_delegated_env

    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=30,
            env=get_delegated_env(),  # allowlisted: git needs PATH/HOME, not API keys
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def index_is_empty(root: Path, base: "Path | None" = None) -> bool:
    """True when there is no index yet, or one with zero indexed files.

    Opening the store creates an empty database file as a side effect
    (``store.connect``), so "the file exists" is not the same question as
    "something was indexed" — a status check or a failed retrieval earlier in
    the same process can leave an empty file behind.
    """
    from llm_router.semantic import store as sstore

    path = sstore.index_path(root, base)
    if not path.exists():
        return True
    try:
        conn = sstore.connect(root, base)
        try:
            row = conn.execute("SELECT COUNT(*) n FROM file").fetchone()
            return row["n"] == 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — an unreadable index is treated as empty
        return True


def maybe_start_background_index(
    root: "Path | str | None" = None, base: "Path | None" = None
) -> bool:
    """Start a detached ``llm-router semantic index`` if this project needs one.

    Returns True only when a build was actually spawned (not merely eligible —
    a spawn failure returns False too, after recording the fail-open).
    """
    try:
        if not _enabled():
            return False

        from llm_router.semantic.scope import resolve_scope

        scope = resolve_scope(root)
        if scope == Path.home().resolve():
            return False
        if not (scope / ".git").exists():
            return False
        if not index_is_empty(scope, base):
            return False

        lock = _lock_path(scope, base)
        cooldown = _cooldown_s()
        if _recently_attempted(lock, cooldown):
            return False  # cheap pre-check; the real, race-safe gate is below

        count = _tracked_file_count(scope)
        if count is None or count > _max_files():
            return False

        # Claimed BEFORE spawning, and atomically (see _claim_lock): the
        # cooldown guards against a storm of concurrent spawns, not against
        # double-counting a build that is already running, so the claim must
        # both land first and be race-safe against concurrent callers.
        if not _claim_lock(lock, cooldown):
            return False

        from llm_router.paths import llm_router_home
        from llm_router.safe_subprocess import get_delegated_env

        env = get_delegated_env({"LLM_ROUTER_HOME": str(llm_router_home())})
        subprocess.Popen(
            [sys.executable, "-m", "llm_router.cli", "semantic", "index"],
            cwd=str(scope),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — never break the caller
        from llm_router import failopen

        failopen.record(_FAILOPEN_CODE, exc)
        return False
