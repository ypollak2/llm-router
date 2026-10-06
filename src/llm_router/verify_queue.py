"""Durable ``pending_verify`` queue + the detached-worker trigger (verifier PR C, SHADOW).

Stdlib only and cheap to import: ``hooks/agent-route.py`` calls into it on the Codex
delegation path, where the budget is p95 <= 50 ms (VERIFIER_PLAN section 6).

Layout under ``llm_router_home()/verify_queue/`` (directories 0700, files 0600):

    pending/<unit_id>.json    one marker per unit, written atomically (tmp + os.replace)
    claimed/<unit_id>.json    a marker a worker owns; the claim is an atomic rename
    patches/<unit_id>.patch   ``git diff <head>`` + untracked files, as captured at dispatch return
    worker.<n>.lock           flock slots: at most MAX_WORKERS workers run at once
    worker_spawn.txt(+.lock)  spawn cooldown, same shape as the usage-refresh spawn (#271)

A marker holds ``unit_id, session_id, ts, cwd (the repo toplevel), head, patch, created_at,
ttl_s``. The patch is the only place a user's source text lives; it is deleted after the verdict
and after TTL expiry (24 h, owner decision 2026-10-06), and an orphan patch (no marker) is swept
after the same TTL.

Nothing here writes the user's tree: capture only READS it (``git rev-parse``, ``ls-files``,
``diff HEAD`` with ``GIT_OPTIONAL_LOCKS=0``, so not even ``.git/index`` is refreshed; untracked
files are rendered to diff text in Python, no temporary index is involved). The whole capture runs
under one wall-clock budget (CAPTURE_BUDGET_S): a stalled read (network FS) yields no marker.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from llm_router import paths

try:  # POSIX only (macOS is the only sandboxed platform, VERIFIER_PLAN owner decision 4)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

TTL_S = 24 * 3600
MAX_WORKERS = 2
MAX_PATCH_BYTES = 2 * 1024 * 1024          # == toolkit.verify_unit.MAX_PATCH_BYTES (pinned by a test)
MAX_UNTRACKED = 500
STALE_CLAIM_S = 300 + 120                  # verify budget cap + slack: older claims are a dead worker's
MAX_ATTEMPTS = 3
SPAWN_COOLDOWN_S = 30.0
UNIT_KIND = "agent_route_codex"
TRUNCATION_MARKER = "[truncated: output cap]"   # codex_agent.TRUNCATION_MARKER (#283)
_UID_RX = re.compile(r"^u_[0-9a-f]{16}$")
_GIT_TIMEOUT_S = 5.0
CAPTURE_BUDGET_S = 3.0                    # wall clock for the whole capture, reads included
_HEAD_RX = re.compile(r"[0-9a-f]{40,64}")      # used with fullmatch: no trailing-newline slack
_OFF = ("0", "off", "false", "no")


def enabled() -> bool:
    """Kill switch: LLM_ROUTER_VERIFY=off stops capture and spawn (the worker only expires)."""
    return os.environ.get("LLM_ROUTER_VERIFY", "on").strip().lower() not in _OFF


# ── ids and paths ────────────────────────────────────────────────────────────

def unit_id(session_id: str | None, kind: str, ts: float | None) -> str | None:
    """Same formula as ``northstar.unit_id`` (copied: northstar is too heavy to import on the hook
    path; ``tests/test_verify_worker.py`` pins the two equal)."""
    if not session_id or ts is None:
        return None
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    raw = f"{session_id}\x1f{kind}\x1f{iso}".encode("utf-8", "replace")
    return "u_" + hashlib.sha256(raw).hexdigest()[:16]


def queue_dir() -> Path:
    return paths.llm_router_home() / "verify_queue"


def _sub(name: str) -> Path:
    return queue_dir() / name


def _failopen(code: str, exc: BaseException) -> None:
    try:
        from llm_router import failopen
        failopen.record(code, exc)
    except Exception:  # noqa: BLE001 -- recording a failure must never raise
        pass


def ensure_dirs() -> None:
    for d in (queue_dir(), _sub("pending"), _sub("claimed"), _sub("patches")):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def patch_path(uid: str) -> Path:
    return _sub("patches") / f"{uid}.patch"


# ── git (read-only on the user's tree) ───────────────────────────────────────

class GitError(RuntimeError):
    pass


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The repo's env allowlist (no secret can reach git) plus the settings that keep git from
    writing the user's repo (GIT_OPTIONAL_LOCKS=0) or prompting."""
    from llm_router.safe_subprocess import get_delegated_env
    return get_delegated_env(extra={"LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0",
                                    "GIT_TERMINAL_PROMPT": "0", **(extra or {})})


def _popen(args: list[str], cwd: str, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


_UNUSUAL_PATH_RX = re.compile(rb'[\x00-\x1f"\\\x7f-\xff]|^ | $')


def _new_file_diff(top: bytes, rel: bytes, limit: int = MAX_PATCH_BYTES) -> bytes | None:
    """The ``git diff`` text for ONE untracked file, built here instead of with ``git add -N`` +
    ``git diff`` (two more git spawns on the hook path). None = cannot be represented (binary, or
    a path git would have to quote). Round-trips through ``git apply`` (pinned by a test)."""
    if _UNUSUAL_PATH_RX.search(rel):
        return None
    full = os.path.join(top, rel)
    st = os.lstat(full)
    head = b"diff --git a/" + rel + b" b/" + rel + b"\n"
    if stat.S_ISLNK(st.st_mode):
        body = os.readlink(full)
        body = body if isinstance(body, bytes) else os.fsencode(body)
        return (head + b"new file mode 120000\n--- /dev/null\n+++ b/" + rel + b"\n@@ -0,0 +1 @@\n+" + body
                + b"\n\\ No newline at end of file\n")
    if not stat.S_ISREG(st.st_mode):
        return None
    mode = b"100755" if st.st_mode & 0o111 else b"100644"
    with open(full, "rb") as fh:
        data = fh.read(limit + 1)
    if b"\0" in data:
        return None
    head += b"new file mode " + mode + b"\n"
    if not data:
        return head + b"index 0000000..e69de29\n"
    lines = data.split(b"\n")
    no_eol = lines[-1] != b""
    if not no_eol:
        lines.pop()
    out = head + b"--- /dev/null\n+++ b/" + rel + b"\n@@ -0,0 +" + (b"1" if len(lines) == 1 else b"1,%d" % len(lines)) \
        + b" @@\n" + b"".join(b"+" + ln + b"\n" for ln in lines)
    return out + (b"\\ No newline at end of file\n" if no_eol else b"")


def _capture(cwd: str) -> tuple[tuple[str, str, bytes] | None, str]:
    """``(toplevel, HEAD sha, patch)`` of the repo `cwd` is in, as ``git diff HEAD`` plus the
    untracked files, taken at the moment the delegation returned. ``(None, why)`` when there is
    nothing to verify (not a repo, no commit, empty diff) or it is too big; raises on a git failure
    (the hook records a failopen code). Three git processes run CONCURRENTLY (hook budget p95 <=
    50 ms); the user's repo is only read, with GIT_OPTIONAL_LOCKS=0 so not even the index is
    refreshed. HEAD moving between the three reads only makes the worker report apply_conflict."""
    env = _git_env()
    try:
        procs = [_popen(["rev-parse", "--show-toplevel", "HEAD"], cwd, env),
                 _popen(["ls-files", "-o", "--exclude-standard", "-z"], cwd, env),
                 _popen(["diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                         "HEAD", "--"], cwd, env)]
    except FileNotFoundError:
        return None, "no_git"
    timer = threading.Timer(_GIT_TIMEOUT_S, lambda: [p.kill() for p in procs])
    timer.start()
    try:
        rev = procs[0].communicate()[0].decode("utf-8", "replace").splitlines()
        untracked_raw = procs[1].communicate()[0]
        patch = procs[2].stdout.read(MAX_PATCH_BYTES + 1)
        if len(patch) > MAX_PATCH_BYTES:
            procs[2].kill()
        procs[2].stdout.close()
        procs[2].wait()
    finally:
        timer.cancel()
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
    if procs[0].returncode != 0 or len(rev) != 2 or not re.fullmatch(r"[0-9a-f]{40,64}", rev[1]):
        return None, "not_a_repo"
    top, head = rev
    if len(patch) > MAX_PATCH_BYTES:
        return None, "patch_too_large"
    if procs[1].returncode != 0 or procs[2].returncode != 0:
        raise GitError("git ls-files/diff failed")
    untracked = [p for p in untracked_raw.split(b"\0") if p and not p.endswith(b"/")]  # b"dir/" = nested repo
    if len(untracked) > MAX_UNTRACKED:
        return None, "too_many_untracked"
    topb, total = os.fsencode(top), len(patch)
    for rel in untracked:
        piece = _new_file_diff(topb, rel, max(MAX_PATCH_BYTES - total, 0))
        if piece is None:
            return None, "untracked_unrepresentable"
        total += len(piece)
        if total > MAX_PATCH_BYTES:
            return None, "patch_too_large"
        patch += piece
    if not patch.strip():
        return None, "empty_patch"
    try:
        patch.decode("utf-8")
    except UnicodeDecodeError:
        return None, "non_utf8_patch"          # the worker hands the verifier a str: no silent mangling
    return (top, head, patch), "ok"


# ── markers ──────────────────────────────────────────────────────────────────

def capture(cwd: str, budget_s: float | None = None) -> tuple[tuple[str, str, bytes] | None, str]:
    """``_capture`` under ONE wall-clock budget. The git processes have their own timer, but the
    ``lstat``/reads that render untracked files do not and can hang on a network filesystem: the
    work runs in a daemon thread and, past the budget, the answer is ``(None, "capture_timeout")``
    (no marker; the stray thread dies with the hook process)."""
    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = _capture(cwd)
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the caller's thread
            box["err"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(CAPTURE_BUDGET_S if budget_s is None else budget_s)
    if t.is_alive():
        return None, "capture_timeout"
    if "err" in box:
        raise box["err"]
    return box["ok"]


def enqueue(uid: str, repo: str, head: str, patch: bytes, *, now: float | None = None) -> Path:
    """Write the patch (0600) then the marker (atomic). No marker without its patch; a failed
    marker write removes the patch again."""
    if not _UID_RX.match(uid):
        raise ValueError("bad unit_id")
    ensure_dirs()
    pp = patch_path(uid)
    fd = os.open(pp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(patch)
    except BaseException:
        pp.unlink(missing_ok=True)
        raise
    created = time.time() if now is None else now
    marker = {"v": 1, "unit_id": uid, "cwd": repo, "head": head, "patch": pp.name,
              "created_at": created, "ttl_s": TTL_S, "attempts": 0}
    dest = _sub("pending") / f"{uid}.json"
    tmp = _sub("pending") / f".{uid}.tmp"
    try:
        _write_json(tmp, marker)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        pp.unlink(missing_ok=True)
        raise
    return dest


def _write_json(path: Path, obj: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def enqueue_from_run(cwd: str, *, session_id: str | None, ts: float | None,
                     truncated: bool = False, content: str = "") -> str:
    """The hook's one call after a delegation returned successfully (`cwd` is where Codex ran).
    Returns a status code ("queued" or why not); raises only on a real failure (the caller records
    a failopen code and leaves the turn untouched). A truncated/capped run NEVER gets a marker: a
    cut-off delegate may have left a half-written tree, and a patch of it must not be verified as
    a clean answer."""
    if not enabled():
        return "disabled"
    if truncated or TRUNCATION_MARKER in (content or ""):
        return "truncated"
    uid = unit_id(session_id, UNIT_KIND, ts)
    if uid is None:
        return "no_unit_id"
    got, why = capture(cwd)
    if got is None:
        return why
    top, head, patch = got
    enqueue(uid, top, head, patch)
    return "queued"


@dataclass
class Marker:
    path: Path
    data: dict

    @property
    def unit_id(self) -> str:
        return self.data["unit_id"]


def _valid(data) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("unit_id"), str)
            and _UID_RX.match(data["unit_id"]) is not None
            and isinstance(data.get("cwd"), str) and isinstance(data.get("head"), str)
            and _HEAD_RX.fullmatch(data["head"]) is not None      # a rev, never an option (--output=...)
            and isinstance(data.get("created_at"), (int, float))
            and not isinstance(data.get("created_at"), bool)
            and data.get("patch") == f"{data['unit_id']}.patch")


_GONE = object()


def _read_marker(path: Path) -> Marker | None:
    """The marker, or None when the file is unreadable/invalid. Use ``_read_marker_or_gone`` when the
    difference between "invalid" and "a worker just renamed it away" matters."""
    got = _read_marker_or_gone(path)
    return None if got is _GONE else got


def _read_marker_or_gone(path: Path):
    """``_GONE`` when the file vanished (another worker claimed or settled it between the directory
    listing and the read: NOT an invalid marker, and its patch is not ours to delete)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _GONE
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return Marker(path, data) if _valid(data) and data["unit_id"] == path.stem else None


def _names(d: Path) -> list[str]:
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".json") and _UID_RX.match(n[:-5]))
    except OSError:
        return []


def queue_nonempty() -> bool:
    return bool(_names(_sub("pending")) or _names(_sub("claimed")))


def pending() -> list[Marker]:
    """Valid pending markers, oldest first. Invalid ones are left for ``take_invalid``."""
    out = [m for name in _names(_sub("pending")) if (m := _read_marker(_sub("pending") / name)) is not None]
    return sorted(out, key=lambda m: m.data["created_at"])


def take_invalid() -> list[str]:
    """Delete every pending marker that fails validation (bad JSON, bad head, a patch ref that is
    not ``<unit_id>.patch``, a unit_id that is not its file name) together with its patch, and
    return their unit ids (the file stems) so the worker can record ``marker_invalid``."""
    out: list[str] = []
    for name in _names(_sub("pending")):
        p = _sub("pending") / name
        got = _read_marker_or_gone(p)
        if got is _GONE:
            continue                  # claimed by another worker meanwhile: not invalid, not ours
        if got is None:
            discard_files(name[:-5], p)
            out.append(name[:-5])
    return out


def is_expired(m: Marker, now: float) -> bool:
    """Past its TTL (never longer than TTL_S, whatever the marker says). A marker dated in the
    future (clock set back) cannot be trusted and counts as expired."""
    ttl = m.data.get("ttl_s")
    ttl = min(float(ttl), TTL_S) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else TTL_S
    age = now - float(m.data["created_at"])
    return age < 0 or age > ttl


def claim(m: Marker) -> Marker | None:
    """Take ownership of a pending marker: an atomic rename, so exactly one worker wins."""
    dest = _sub("claimed") / m.path.name
    try:
        os.rename(m.path, dest)
    except OSError:
        return None
    try:
        os.utime(dest)
    except OSError:
        pass
    return Marker(dest, m.data)


def recover_stale_claims(now: float) -> list[Marker]:
    """A claim older than STALE_CLAIM_S belongs to a dead worker: back to pending, with attempts+1.
    Returns the markers that ran out of attempts (the caller records them unavailable)."""
    exhausted: list[Marker] = []
    for name in _names(_sub("claimed")):
        p = _sub("claimed") / name
        try:
            if now - p.stat().st_mtime < STALE_CLAIM_S:
                continue
        except OSError:
            continue
        m = _read_marker_or_gone(p)
        if m is _GONE:
            continue                  # its owner just settled it
        if m is None:
            discard_files(name[:-5], p)
            continue
        m.data["attempts"] = int(m.data.get("attempts", 0)) + 1
        if m.data["attempts"] >= MAX_ATTEMPTS:
            exhausted.append(m)
            continue
        try:
            _write_json(p, m.data)
            os.rename(p, _sub("pending") / name)
        except OSError as exc:
            _failopen("CHZ-FO-VERIFY-QUEUE-RECOVER", exc)
    return exhausted


def discard(m: Marker) -> None:
    discard_files(m.unit_id, m.path)


def discard_files(uid: str, marker_path: Path) -> None:
    """Delete the marker and its patch (the patch holds the user's source text)."""
    marker_path.unlink(missing_ok=True)
    if _UID_RX.match(uid):
        patch_path(uid).unlink(missing_ok=True)


def sweep_orphan_patches(now: float) -> int:
    """Patches with no marker and older than the TTL (a crash between the two writes)."""
    live = {n[:-5] for n in _names(_sub("pending")) + _names(_sub("claimed"))}
    removed = 0
    try:
        names = os.listdir(_sub("patches"))
    except OSError:
        return 0
    for n in names:
        if not n.endswith(".patch") or n[:-6] in live:
            continue
        p = _sub("patches") / n
        try:
            if now - p.stat().st_mtime > TTL_S:
                p.unlink()
                removed += 1
        except OSError as exc:
            _failopen("CHZ-FO-VERIFY-QUEUE-SWEEP", exc)
    return removed


# ── worker slots and spawn ───────────────────────────────────────────────────

def acquire_slot():
    """An open, flock-ed file object (hold it for the worker's lifetime; the kernel releases it if
    the worker dies), or None when MAX_WORKERS are already running or flock is unavailable."""
    if fcntl is None:
        return None
    ensure_dirs()
    for i in range(MAX_WORKERS):
        fh = open(queue_dir() / f"worker.{i}.lock", "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            fh.close()
    return None


def _claim_spawn(cooldown_s: float) -> bool:
    """Atomically claim the right to start one worker: non-blocking flock + mtime cooldown (the
    cooldown is the staleness bound, so a hung worker cannot wedge future spawns). Same shape as
    the usage-refresh claim of #271."""
    from llm_router.file_lock import exclusive_lock
    marker = queue_dir() / "worker_spawn.txt"
    ensure_dirs()
    with exclusive_lock(Path(str(marker) + ".lock"), timeout=0.0) as locked:
        if not locked:
            return False
        try:
            age = time.time() - os.path.getmtime(marker)
        except OSError:
            age = None
        if age is not None and 0 <= age < cooldown_s:   # negative (future-dated) counts as expired
            return False
        with open(marker, "w") as fh:
            fh.write(str(time.time()))
        return True


#: LLM_ROUTER_* the worker is given on purpose. No secret, no provider key: everything else comes
#: from ``safe_subprocess.get_delegated_env`` (an allowlist).
CHILD_ENV_KEYS = ("LLM_ROUTER_HOME", "LLM_ROUTER_VERIFY", "LLM_ROUTER_VERIFY_BUDGET_S")


def worker_env() -> dict[str, str]:
    from llm_router.safe_subprocess import get_delegated_env
    return get_delegated_env(extra={k: os.environ[k] for k in CHILD_ENV_KEYS if k in os.environ})


def worker_argv() -> list[str]:
    return [sys.executable, "-m", "llm_router.verify_worker"]


def spawn_worker_if_needed(cooldown_s: float = SPAWN_COOLDOWN_S) -> bool:
    """Start ONE detached ``python -m llm_router.verify_worker`` when the queue is non-empty.
    Fixed argv, DEVNULL stdio, own session, allowlisted env, flock + cooldown. Never blocks and
    never waits for the child. True when a worker was started. Raises on a real failure; the
    hook wrapper records the failopen code."""
    if not enabled() or not queue_nonempty():
        return False
    if not _claim_spawn(cooldown_s):
        return False
    subprocess.Popen(worker_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, env=worker_env())
    return True
