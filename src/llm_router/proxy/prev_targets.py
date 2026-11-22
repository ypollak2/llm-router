"""``prev_tool_targets``: which file or command the previous tool calls targeted, as a salted hash.

The D3 redo rule (a locally served step is redone if Claude re-edits the same file or re-runs
the same command within 3 steps) needs target identity; ``prev_tools`` has tool names only.
Each entry is ``{"name": <tool name>, "h": <12 hex or None>}``.

``h`` is the first 12 hex chars of HMAC-SHA256(per-install salt, normalised target):

* file tools (Edit, Write, MultiEdit, NotebookEdit, Read): the ``file_path`` (``notebook_path``
  for NotebookEdit), ``~`` expanded, then ``posixpath.normpath`` (collapses ``//``, ``.``,
  ``..``). No symlink resolution and no cwd join: a relative path hashes as written.
* Bash: the ``command`` with every run of whitespace collapsed to one space and the ends
  stripped. Nothing else is normalised (``git  status`` equals ``git status``; quoting is not
  touched).
* any other tool, or a missing / non-string / empty input: ``h`` is ``None``. Absent is never
  coerced to a value.

The salt is 32 random bytes in ``<router home>/prev_target_salt`` (mode 0600), created once,
never rotated and never logged. Without it the hash of a short command like ``git status``
could be guessed. If the salt cannot be read or created, every ``h`` is ``None`` (the row still
carries the tool names). No raw path or command is returned or stored. The list holds at most
``MAX_TARGETS`` entries.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import posixpath
import stat
import threading
import time
from pathlib import Path

from llm_router.paths import llm_router_home

SALT_FILE = "prev_target_salt"
SALT_BYTES = 32
HASH_HEX = 12
MAX_TARGETS = 8
_FILE_KEYS = {"edit": "file_path", "write": "file_path", "multiedit": "file_path",
              "read": "file_path", "notebookedit": "notebook_path"}

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # absent on some platforms (as local_check/runner.py)
RETRY_S = 60.0  # a failed salt load is retried at most this often
_lock = threading.Lock()
_cache: dict[str, bytes] = {}
_failed_at: dict[str, float] = {}
_retrying: set[str] = set()
_logged: set[str] = set()


def _load_salt(path: Path) -> bytes | None:
    """The salt in ``path``, or None when absent, empty, short, not a regular file or not
    owner-only. Never creates."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            return None
        data = os.read(fd, SALT_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) == SALT_BYTES else None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _create_salt(path: Path) -> bytes | None:
    """Write the salt to a 0600 temp file in the same dir, fsync, then ``os.link`` it to the final
    name: the final file is never empty or partial. On EEXIST the winner's file is read."""
    salt = os.urandom(SALT_BYTES)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}.{threading.get_ident()}.{os.urandom(4).hex()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        try:
            _write_all(fd, salt)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _load_salt(path)
        return salt
    except OSError:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _load_or_create(path: Path) -> bytes | None:
    if path.exists() or path.is_symlink():
        return _load_salt(path)
    return _create_salt(path)


def salt() -> bytes | None:
    """The per-install salt (cached per router home once loaded), or None when unavailable.

    Steady state is one lock and one dict hit. File I/O never runs under the lock: after a
    failure exactly one thread retries (at most once per ``RETRY_S``, flagged under the lock)
    while the others return None at once. The first failure records one fail-open event
    (``LR-FO-PROXY-PREV-TARGETS-SALT``, no salt bytes)."""
    path = llm_router_home() / SALT_FILE
    key = str(path)
    with _lock:
        if key in _cache:
            return _cache[key]
        now = time.monotonic()
        last = _failed_at.get(key)
        if key in _retrying or (last is not None and now - last < RETRY_S):
            return None
        _retrying.add(key)
        first_failure = key not in _logged
    got = None
    try:
        got = _load_or_create(path)
    finally:
        with _lock:
            _retrying.discard(key)
            if got is not None:
                _cache[key] = got
                _failed_at.pop(key, None)
            else:
                _failed_at[key] = time.monotonic()
                _logged.add(key)
    if got is None and first_failure:
        try:
            from llm_router import failopen

            failopen.record("LR-FO-PROXY-PREV-TARGETS-SALT",
                            detail="salt file unavailable or invalid; hashes are null until it loads")
        except Exception:  # noqa: BLE001 - accounting must not fail a request
            pass
    return got


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
        _failed_at.clear()
        _retrying.clear()
        _logged.clear()


def normalise_command(command: str) -> str:
    return " ".join(command.split())


def normalise_path(path: str) -> str:
    return posixpath.normpath(os.path.expanduser(path.strip()))


def _target(name: str, inp) -> str | None:
    if not isinstance(inp, dict):
        return None
    lname = name.lower() if isinstance(name, str) else ""
    if lname == "bash":
        cmd = inp.get("command")
        text = normalise_command(cmd) if isinstance(cmd, str) else ""
    elif lname in _FILE_KEYS:
        p = inp.get(_FILE_KEYS[lname])
        text = normalise_path(p) if isinstance(p, str) and p.strip() else ""
    else:
        return None
    return text or None


def target_hash(name: str, inp, key: bytes | None) -> str | None:
    text = _target(name, inp)
    if text is None or key is None:
        return None
    return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:HASH_HEX]


def prev_tool_targets(tool_uses: list[dict]) -> list[dict]:
    """``[{"name", "h"}]`` for the first ``MAX_TARGETS`` tool_use blocks."""
    key = salt()
    return [{"name": b.get("name", ""), "h": target_hash(b.get("name"), b.get("input"), key)}
            for b in tool_uses[:MAX_TARGETS]]
