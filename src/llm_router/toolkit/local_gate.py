"""Cross-process admission gate for one local model server (PLAN-LOCAL-QUEUE v1-v6, PR-1).

Primitives only: URL canonicalisation, a per-server gate directory with permanent
``slot-<k>.lock`` files and a permanent ``config.lock``, ``slots.json`` written via
temp + fsync + rename, and ``admit`` / ``admit_exclusive`` (sync and async).

Wording: deadline-bounded waiting; not starvation-free; order not FIFO.

Off by default (``LLM_ROUTER_LOCAL_QUEUE``). PR-1 wires no callers: enabling the
queue before PR-3 is unsupported.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import errno
import glob
import hashlib
import json
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from urllib.parse import urlsplit

from llm_router import failopen, paths, state_files
from llm_router.toolkit.adapters.ollama import LocalModelConflict

__all__ = [
    "Admission",
    "LocalQueueConfigConflict",
    "LocalQueueReentrant",
    "LocalQueueResetRefused",
    "LocalQueueTimeout",
    "admit",
    "admit_exclusive",
    "aadmit",
    "aadmit_exclusive",
    "canonical_url",
    "gate_dir",
    "queue_enabled",
    "reset",
]

POLL_S = 0.025
_USE_MSVCRT = sys.platform == "win32"
_O_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
_CONTENTION = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK, 36}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_PORT = {"http": 80, "https": 443}
_FS_ALLOW = {"apfs", "hfs", "ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "zfs", "f2fs", "overlay"}
_FS_CACHE: dict[str, bool] = {}

# Test seam: called while config.lock is held, after validation, before slot attempts.
_hook_after_validate = None


class _LocalQueueError(LocalModelConflict):
    kind = "local_queue"

    def __init__(self, message: str, url: str = ""):
        # LocalModelConflict.__init__ describes a memory conflict; skip it, keep the typing.
        RuntimeError.__init__(self, message)
        self.resident, self.size_gb, self.url, self.allowed, self.limit_gb = "", 0.0, url, "", 0.0


class LocalQueueTimeout(_LocalQueueError):
    """The caller's absolute deadline passed before a slot was free."""

    kind = "local_queue_timeout"


class LocalQueueConfigConflict(_LocalQueueError):
    """LLM_ROUTER_LOCAL_SLOTS differs from the server's shared slots.json."""

    kind = "local_queue_config_conflict"


class LocalQueueReentrant(_LocalQueueError):
    """admit_exclusive() while this process already holds an admission on the same server (would self-block)."""

    kind = "local_queue_reentrant"


class LocalQueueResetRefused(_LocalQueueError):
    """reset() refused because a holder is live."""

    kind = "local_queue_reset_refused"


# ── env / paths ──────────────────────────────────────────────────────────────

def queue_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_LOCAL_QUEUE", "").strip().lower() in ("1", "on", "true", "yes")


def _wanted_slots() -> int:
    try:
        return max(1, int(os.environ.get("LLM_ROUTER_LOCAL_SLOTS", "1").strip() or "1"))
    except ValueError:
        return 1


def canonical_url(url: str) -> str:
    """scheme://host:port; loopback aliases collapse; default ports filled; path dropped."""
    raw = (url or "").strip()
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK:
        host = "loopback"
    try:
        port = parts.port or _DEFAULT_PORT.get(scheme, 0)
    except ValueError:
        port = _DEFAULT_PORT.get(scheme, 0)
    return f"{scheme}://{host}:{port}"


def _key(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12]


def gate_dir(url: str) -> Path:
    base = os.environ.get("LLM_ROUTER_LOCAL_GATE_DIR", "").strip()
    root = Path(base) if base else paths.state_path("run", "local-gate")
    return root / _key(url)


def _log_row(url: str, outcome: str, **fields) -> None:
    """Counts only, never prompt text."""
    row = {"ts": round(time.time(), 3), "url_key": _key(url), "outcome": outcome, **fields}
    try:
        with state_files.open_private(paths.state_path("local_gate.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        failopen.record("CHZ-FO-LOCAL-GATE-LOG", exc)


# ── filesystem check ─────────────────────────────────────────────────────────

def _linux_fstype(path: str, mounts_text: str) -> str:
    best, best_type = "", ""
    for line in mounts_text.splitlines():
        f = line.split()
        if len(f) < 3:
            continue
        mp = f[1].replace("\\040", " ")
        if (path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/") and len(mp) >= len(best):
            best, best_type = mp, f[2]
    return best_type


def _fstype_for(path: str) -> str:
    """Filesystem type of ``path`` ('' when unknown)."""
    real = os.path.realpath(path)
    if sys.platform == "darwin":
        import ctypes
        import ctypes.util

        class _Statfs(ctypes.Structure):
            _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
                        ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
                        ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                        ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
                        ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
                        ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                        ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                        ("f_mntfromname", ctypes.c_char * 1024), ("f_reserved", ctypes.c_uint32 * 8)]

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        st = _Statfs()
        if libc.statfs(real.encode(), ctypes.byref(st)) != 0:
            return ""
        return st.f_fstypename.decode(errors="replace")
    if sys.platform.startswith("linux"):
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            return _linux_fstype(real, fh.read())
    if sys.platform == "win32":
        import ctypes

        drive = os.path.splitdrive(real)[0] + "\\"
        return "fixed" if ctypes.windll.kernel32.GetDriveTypeW(drive) == 3 else ""
    return ""


def _fs_allowed(fstype: str) -> bool:
    return fstype in _FS_ALLOW or (sys.platform == "win32" and fstype == "fixed")


def _fs_ok(path: Path) -> bool:
    key = str(path)
    if key not in _FS_CACHE:
        try:
            _FS_CACHE[key] = _fs_allowed(_fstype_for(key))
        except Exception as exc:  # noqa: BLE001 - unknown means disabled
            failopen.record("CHZ-FO-LOCAL-GATE-FSTYPE", exc)
            _FS_CACHE[key] = False
    return _FS_CACHE[key]


# ── locking primitives ───────────────────────────────────────────────────────

def _try_lock(fd: int) -> bool:
    """Non-blocking exclusive lock. True = got it, False = contention. Other errors raise."""
    try:
        if _USE_MSVCRT:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in _CONTENTION:
            return False
        raise


def _unlock(fd: int) -> None:
    if _USE_MSVCRT:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


# ── fork lifecycle (v4 §4, v5 §3) ────────────────────────────────────────────

_LIFECYCLE = threading.Lock()
# id(admission) -> admission, for after-fork invalidation and the re-entrancy guard. Weak: a dropped
# Admission must be collectable so its finalizer can free the slot.
_LIVE: "weakref.WeakValueDictionary[int, Admission]" = weakref.WeakValueDictionary()
# Admissions held by the current execution context (thread, or asyncio task plus tasks spawned from it).
# Immutable tuple of weakrefs, re-set on admit; release flips Admission._valid, so no per-context cleanup.
_HELD: "contextvars.ContextVar[tuple]" = contextvars.ContextVar("local_gate_held", default=())
_PENDING_CLOSE: list[int] = []  # fds a GC finalizer could not close without the lifecycle lock
_OPEN_FDS: set[int] = set()  # every gate fd currently open (admissions and in-flight attempts)


def _before_fork() -> None:
    _LIFECYCLE.acquire()


def _after_fork_parent() -> None:
    _LIFECYCLE.release()


def _after_fork_child() -> None:
    global _LIFECYCLE
    # Plain close: never LOCK_UN, which would release the parent's lock (shared open file description).
    for fd in list(_OPEN_FDS):
        with contextlib.suppress(OSError):
            os.close(fd)
    _OPEN_FDS.clear()
    _PENDING_CLOSE.clear()   # deferred fd numbers belong to the parent; the child may reuse them
    for adm in list(_LIVE.values()):
        adm._invalidate()
    _LIVE.clear()
    _LIFECYCLE = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_before_fork, after_in_parent=_after_fork_parent,
                        after_in_child=_after_fork_child)


def _open(path: Path) -> int:
    fd = os.open(str(path), _O_FLAGS, 0o600)
    _OPEN_FDS.add(fd)  # caller holds _LIFECYCLE
    return fd


def _close_fds(fds) -> None:
    with _LIFECYCLE:
        for fd in fds:
            if fd in _OPEN_FDS:
                _OPEN_FDS.discard(fd)
                with contextlib.suppress(OSError):
                    os.close(fd)


def _drain_pending() -> None:
    """Close fds that GC finalizers deferred. Caller holds _LIFECYCLE."""
    while _PENDING_CLOSE:
        fd = _PENDING_CLOSE.pop()
        if fd in _OPEN_FDS:
            _OPEN_FDS.discard(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


def _gc_close(fds: list) -> None:
    """Finalizer for an Admission dropped without release(): free its slot. Never blocks: GC can run
    while this same thread holds _LIFECYCLE, so on contention the fds are deferred to the next attempt."""
    held, fds[:] = list(fds), []
    if not held:
        return
    if _LIFECYCLE.acquire(blocking=False):
        try:
            for fd in held:
                if fd in _OPEN_FDS:
                    _OPEN_FDS.discard(fd)
                    with contextlib.suppress(OSError):
                        os.close(fd)
        finally:
            _LIFECYCLE.release()
    else:
        _PENDING_CLOSE.extend(held)


class Admission:
    """One held admission (one slot, or all slots when exclusive). Closing the fds releases the locks."""

    def __init__(self, url: str, fds, slots, exclusive: bool, wait_ms: int, caller: str, disabled: bool = False):
        self.url, self.slots, self.exclusive, self.disabled = url, tuple(slots), exclusive, disabled
        self.wait_ms, self.caller = wait_ms, caller
        self._fds = list(fds)
        self._valid = True
        self._t0 = time.monotonic()
        self._finalizer = None
        if self._fds:
            _LIVE[id(self)] = self
            live = tuple(r for r in _HELD.get() if (a := r()) is not None and a._valid)
            _HELD.set(live + (weakref.ref(self),))
            # Shares the fd list (never rebound) so the finalizer holds no reference to self.
            self._finalizer = weakref.finalize(self, _gc_close, self._fds)

    def _invalidate(self) -> None:
        self._valid = False
        if self._finalizer is not None:
            self._finalizer.detach()
        self._fds[:] = []  # child side of a fork: the fds were already closed

    def release(self) -> None:
        if not self._valid:
            return
        self._valid = False
        if self._finalizer is not None:
            self._finalizer.detach()
        fds = list(self._fds)
        self._fds[:] = []
        with _LIFECYCLE:
            _LIVE.pop(id(self), None)
        _close_fds(fds)
        if not self.disabled:
            _log_row(self.url, "released", kind="exclusive" if self.exclusive else "slot",
                     caller=self.caller, wait_ms=self.wait_ms,
                     held_ms=int((time.monotonic() - self._t0) * 1000))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.release()


def _disabled(url: str, why: str, caller: str, **fields) -> Admission:
    _log_row(url, why, caller=caller, **fields)
    return Admission(url, [], [], False, 0, caller, disabled=True)


def _off(url: str, caller: str) -> Admission:
    """Queue switched off: a true no-op, no log row and no filesystem access."""
    return Admission(url, [], [], False, 0, caller, disabled=True)


def _guard_reentrant(url: str, exclusive: bool) -> None:
    """admit_exclusive needs every slot; if the caller's own context (thread, or task and its children)
    already holds one it would wait out its deadline. Other threads/tasks holding a slot just wait."""
    if not exclusive:
        return
    key = canonical_url(url)
    for ref in _HELD.get():
        adm = ref()
        if adm is not None and adm._valid and not adm.disabled and canonical_url(adm.url) == key:
            raise LocalQueueReentrant(
                f"local_queue_reentrant: this execution context already holds an admission on {key}; "
                "release it before admit_exclusive", url)


# ── config ───────────────────────────────────────────────────────────────────

def _read_config(path: Path):
    """Return int slots, or None when missing/corrupt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        n = data["slots"]
        if isinstance(n, int) and not isinstance(n, bool) and n >= 1:
            return n
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return -1
    return -1


def _write_config(d: Path, n: int) -> None:
    tmp = d / f"slots.json.tmp.{os.getpid()}"
    with state_files.open_private(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"slots": n}))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, d / "slots.json")
    if sys.platform != "win32":
        dfd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _all_slot_files(d: Path) -> list[Path]:
    return [Path(p) for p in sorted(glob.glob(str(d / "slot-*.lock")))]


def _take_all_present(d: Path, taken: list[int]) -> bool:
    """Take every slot-*.lock present (glob), or nothing. Caller holds _LIFECYCLE."""
    for p in _all_slot_files(d):
        fd = _open(p)
        taken.append(fd)
        if not _try_lock(fd):
            return False
    return True


# ── one attempt ──────────────────────────────────────────────────────────────

class _Flags:
    corrupt_logged = False


def _attempt(url: str, d: Path, exclusive: bool, caller: str, wait_ms: int, flags: _Flags):
    """One admission attempt. Returns Admission, or None on contention. Nothing is held on None."""
    mine = _wanted_slots()
    fds: list[int] = []
    keep = False
    with _LIFECYCLE:
        _drain_pending()
        try:
            cfd = _open(d / "config.lock")
            fds.append(cfd)
            if not _try_lock(cfd):
                return None
            n = _read_config(d / "slots.json")
            if n is None:
                _write_config(d, mine)
                n = mine
            elif n == -1:
                taken: list[int] = []
                try:
                    got = _take_all_present(d, taken)
                    if got:
                        _write_config(d, mine)
                        n = mine
                        _log_row(url, "config_recovered", caller=caller)
                finally:
                    for fd in taken:
                        _OPEN_FDS.discard(fd)
                        with contextlib.suppress(OSError):
                            os.close(fd)
                if not got:
                    if not flags.corrupt_logged:
                        flags.corrupt_logged = True
                        _log_row(url, "config_corrupt_holders", caller=caller)
                    return None
            if n != mine:
                _log_row(url, "slots_conflict", caller=caller, file_slots=n, my_slots=mine)
                raise LocalQueueConfigConflict(
                    f"local_queue_config_conflict: {canonical_url(url)} uses {n} slots, this process asked for {mine}. "
                    "Run `llm-router local-gate reset` after stopping holders.", url)
            if _hook_after_validate is not None:
                _hook_after_validate()
            got_idx: list[int] = []
            for k in range(n):
                sfd = _open(d / f"slot-{k}.lock")
                fds.append(sfd)
                if _try_lock(sfd):
                    got_idx.append(k)
                    if not exclusive:
                        break
                elif exclusive:
                    return None  # release everything, retry with a fresh read
            if not got_idx:
                return None
            slot_fds = fds[1:] if exclusive else [fds[-1]]
            # Close config.lock and any slot fd we did not keep.
            for fd in fds:
                if fd not in slot_fds:
                    _OPEN_FDS.discard(fd)
                    with contextlib.suppress(OSError):
                        os.close(fd)
            keep = True
            return Admission(url, slot_fds, got_idx, exclusive, wait_ms, caller)
        finally:
            if not keep:
                for fd in fds:
                    if fd in _OPEN_FDS:
                        _OPEN_FDS.discard(fd)
                        with contextlib.suppress(OSError):
                            os.close(fd)


def _prepare(url: str, caller: str):
    """Return (dir, None) to proceed or (None, disabled Admission) to fail open."""
    if not queue_enabled():
        return None, _off(url, caller)
    d = gate_dir(url)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe_ok = os.access(d, os.W_OK)
    except OSError as exc:
        failopen.record("CHZ-FO-LOCAL-GATE-DIR", exc)
        return None, _disabled(url, "gate_dir_unwritable", caller)
    if not probe_ok:
        return None, _disabled(url, "gate_dir_unwritable", caller)
    if not _fs_ok(d):
        return None, _disabled(url, "gate_disabled_fs", caller)
    return d, None


def _timeout(url: str, caller: str, t0: float, exclusive: bool) -> LocalQueueTimeout:
    wait_ms = int((time.monotonic() - t0) * 1000)
    _log_row(url, "timeout", kind="exclusive" if exclusive else "slot", caller=caller, wait_ms=wait_ms)
    return LocalQueueTimeout(
        f"local_queue_timeout: no slot on {canonical_url(url)} within the deadline (waited {wait_ms} ms)", url)


def _run_sync(url, deadline, exclusive, caller):
    d, off = _prepare(url, caller)
    if off is not None:
        return off
    _guard_reentrant(url, exclusive)
    t0, flags = time.monotonic(), _Flags()
    while True:
        try:
            adm = _attempt(url, d, exclusive, caller, int((time.monotonic() - t0) * 1000), flags)
        except LocalModelConflict:
            raise
        except OSError as exc:
            failopen.record("CHZ-FO-LOCAL-GATE-LOCK", exc)
            return _disabled(url, "lock_error", caller)
        if adm is not None:
            return adm
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timeout(url, caller, t0, exclusive)
        time.sleep(min(POLL_S, remaining))


async def _run_async(url, deadline, exclusive, caller):
    d, off = _prepare(url, caller)
    if off is not None:
        return off
    _guard_reentrant(url, exclusive)
    t0, flags = time.monotonic(), _Flags()
    while True:
        try:
            adm = _attempt(url, d, exclusive, caller, int((time.monotonic() - t0) * 1000), flags)
        except LocalModelConflict:
            raise
        except OSError as exc:
            failopen.record("CHZ-FO-LOCAL-GATE-LOCK", exc)
            return _disabled(url, "lock_error", caller)
        if adm is not None:
            return adm
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timeout(url, caller, t0, exclusive)
        await asyncio.sleep(min(POLL_S, remaining))


def admit(url: str, deadline: float, *, caller: str = "") -> Admission:
    """Take one slot. ``deadline`` is an absolute time.monotonic() value."""
    return _run_sync(url, deadline, False, caller)


def admit_exclusive(url: str, deadline: float, *, caller: str = "") -> Admission:
    """Take every slot in one attempt (all or nothing) for load/unload."""
    return _run_sync(url, deadline, True, caller)


async def aadmit(url: str, deadline: float, *, caller: str = "") -> Admission:
    return await _run_async(url, deadline, False, caller)


async def aadmit_exclusive(url: str, deadline: float, *, caller: str = "") -> Admission:
    return await _run_async(url, deadline, True, caller)


def reset(url: str, deadline: float, *, slots: int | None = None) -> int:
    """Rewrite slots.json. Takes config.lock plus every slot-*.lock present, or refuses.

    Deliberate: a live holder makes this raise LocalQueueResetRefused immediately, without waiting for
    ``deadline``. ``deadline`` only bounds contention on config.lock itself (another reset/attempt).
    Raises ValueError when ``slots`` < 1."""
    if slots is not None and slots < 1:
        raise ValueError(f"slots must be >= 1, got {slots}")
    d = gate_dir(url)
    d.mkdir(parents=True, exist_ok=True)
    new = slots if slots is not None else _wanted_slots()
    while True:
        with _LIFECYCLE:
            _drain_pending()   # a GC-deferred fd is not a live holder
            fds: list[int] = []
            try:
                cfd = _open(d / "config.lock")
                fds.append(cfd)
                if _try_lock(cfd):
                    if not _take_all_present(d, fds):
                        _log_row(url, "reset_refused")
                        raise LocalQueueResetRefused(
                            f"local_queue_reset_refused: holders are live on {canonical_url(url)}", url)
                    _write_config(d, new)
                    _log_row(url, "reset", slots=new)
                    return new
            finally:
                for fd in fds:
                    if fd in _OPEN_FDS:
                        _OPEN_FDS.discard(fd)
                        with contextlib.suppress(OSError):
                            os.close(fd)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timeout(url, "reset", time.monotonic(), True)
        time.sleep(min(POLL_S, remaining))
