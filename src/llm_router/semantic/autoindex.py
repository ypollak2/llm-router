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
* **never a scratch tempdir.** An agent sub-session routinely works out of a
  disposable git worktree under ``/tmp``, ``/private/tmp``,
  ``tempfile.gettempdir()`` or ``/var/folders`` (macOS) — a linked worktree's
  ``.git`` is a plain *file*, not a directory, but it still satisfies the
  git-repo guard above. Every fresh scratch path would otherwise seed a
  never-cleaned ~5MB knowledge-store entry. Checked against the *realpath* of
  the scope (symlinks such as macOS's ``/tmp`` -> ``/private/tmp`` resolved
  first). Overridable via ``LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES``
  (``os.pathsep``-separated; empty string disables the guard entirely) so
  tests running under pytest's own ``tmp_path`` — itself under a scratch
  prefix — are not caught by it.
* **a tracked-file-count cap.** ``git ls-files`` on a monorepo can return six
  figures. Indexing that in the background on every session is not what
  "automatic" was asked for; a repo over the cap is left for
  ``llm-router semantic index`` to be run by hand. Documented and overridable
  via ``LLM_ROUTER_SEMANTIC_AUTOINDEX_MAX_FILES``. This check, and the
  git-failure case (``git ls-files`` erroring), also claim the cooldown lock
  on a skip — not just on a spawn — so a project that is permanently over
  cap does not pay for a fresh ``git ls-files`` on every single call.
* **a cooldown lock.** Two concurrent sessions, or two retrievals in the same
  session, must not both spawn a build. The lock is a timestamp file, not a
  PID file — a PID can be reused, and this only needs to know how recently a
  build was last STARTED (or last skipped as over-cap), not whether it is
  still running. A lock directory that cannot be created at all (e.g. a
  read-only filesystem) falls back to an in-process marker, so this process
  still backs off for the cooldown instead of re-running git on every call —
  it just cannot coordinate that backoff with other processes.
* **behind an env flag, default ON** (``LLM_ROUTER_SEMANTIC_AUTOINDEX``), so an
  operator who wants no background process ever can turn it off.

Every failure path here is a no-op plus a :mod:`llm_router.failopen` record —
this runs on the hot path of every retrieval and must never slow or break the
call that triggered it.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

__all__ = ["maybe_start_background_index", "index_is_empty"]

_DEFAULT_MAX_FILES = 20_000
_DEFAULT_COOLDOWN_S = 3600.0
_FAILOPEN_CODE = "CHZ-FO-SEMANTIC-AUTOINDEX"

# Last-resort, in-process-only cooldown: used when the real lock FILE cannot
# be written at all (its directory can't even be created — e.g. a read-only
# filesystem). Keyed by the lock path so distinct scopes don't share a
# backoff. This cannot coordinate across processes — that is what the lock
# file is for — it only stops THIS process hammering `git ls-files` on every
# call when the filesystem truly will not take the write.
_inprocess_cooldown: "dict[str, float]" = {}


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
        pass  # no lock file, or unreadable — fall through to the in-process marker
    else:
        return age < cooldown
    last = _inprocess_cooldown.get(str(lock))
    return last is not None and (time.time() - last) < cooldown


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
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # The lock directory itself can't be created (e.g. read-only
        # filesystem). Record an in-process marker so THIS process still
        # backs off for the cooldown instead of re-running `git ls-files` on
        # every call — see `_inprocess_cooldown`.
        _inprocess_cooldown[str(lock)] = time.time()
        return False
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
    except OSError:
        _inprocess_cooldown[str(lock)] = time.time()
        return False
    try:
        os.write(fd, now)
    finally:
        os.close(fd)
    return True


def _release_lock(lock: Path) -> None:
    """Undo a successful ``_claim_lock`` after the thing it guarded failed to
    start, so a Popen failure does not cause a full cooldown's worth of
    blackout for something that never actually ran.

    Deliberately not wrapped in its own ``try/except``: the only caller,
    ``maybe_start_background_index``, already wraps its whole body in
    ``except Exception as exc: failopen.record(...)``, so a failure here is
    still recorded there instead of becoming a second, silent
    ``except: pass`` write site (T-14 census).
    """
    lock.unlink(missing_ok=True)


def _lower_priority(pid: int) -> None:
    """Renice the background build from the parent, after it has started.

    Done here rather than via ``preexec_fn``: the caller can be a
    multithreaded server process, where code run between fork and exec is
    unsafe. A child that already exited is fine to miss; anything else is
    recorded, not raised, because the build itself started correctly.
    """
    if not hasattr(os, "setpriority"):
        return
    try:
        os.setpriority(os.PRIO_PROCESS, pid, 10)
    except ProcessLookupError:
        return
    except OSError as exc:
        from llm_router import failopen

        failopen.record(_FAILOPEN_CODE, exc)


def _scratch_prefixes() -> "list[Path]":
    """Realpath'd directories that are scratch space, never a real project.

    ``LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES`` overrides the list
    (``os.pathsep``-separated; set to the empty string to disable the guard
    entirely) — used by tests, which run under pytest's own ``tmp_path`` and
    would otherwise always be excluded by this same guard.
    """
    return _resolved_scratch_prefixes(
        os.environ.get("LLM_ROUTER_SEMANTIC_AUTOINDEX_SCRATCH_PREFIXES")
    )


@functools.lru_cache(maxsize=8)
def _resolved_scratch_prefixes(raw: "str | None") -> "list[Path]":
    # Cached per override value: resolving four paths on every retrieval
    # call is measurable on the hot path, and the answer never changes.
    if raw is not None:
        if not raw.strip():
            return []
        candidates = [p for p in raw.split(os.pathsep) if p.strip()]
    else:
        candidates = [tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/folders"]
    prefixes = []
    for candidate in candidates:
        try:
            prefixes.append(Path(candidate).resolve())
        except OSError:
            continue
    return prefixes


def _is_scratch_path(scope: Path) -> bool:
    try:
        real = scope.resolve()
    except OSError:
        return False
    for prefix in _scratch_prefixes():
        try:
            real.relative_to(prefix)
        except ValueError:
            continue
        return True
    return False


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
        if _is_scratch_path(scope):
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
            # A negative result still claims the lock (best-effort — it may
            # fail, see `_claim_lock`): without this, a repo that is
            # permanently over the cap, or whose `git ls-files` permanently
            # errors, re-runs that synchronous git call on EVERY single
            # pack.build(), forever, instead of once per cooldown.
            _claim_lock(lock, cooldown)
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
        # `env=` stays a literal keyword (not folded into a kwargs dict and
        # splatted in): `test_r4_subprocess_env_allowlist.py` AST-scans call
        # sites for a literal `env=` keyword to confirm this subprocess does
        # not inherit the operator's full environment, and a call built as
        # `subprocess.Popen(argv, **kwargs)` is invisible to that scan.
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "llm_router.cli", "semantic", "index"],
                cwd=str(scope),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            # The lock was claimed for a build that never actually started —
            # release it so the next call retries immediately instead of
            # waiting out a full cooldown for nothing.
            _release_lock(lock)
            raise
        _lower_priority(proc.pid)
        return True
    except Exception as exc:  # noqa: BLE001 — never break the caller
        from llm_router import failopen

        failopen.record(_FAILOPEN_CODE, exc)
        return False
