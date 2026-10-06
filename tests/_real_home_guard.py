"""Hard safety net: a test run must never touch the operator's real state.

WHY THIS EXISTS (2026-10-06)
----------------------------
The operator's live ``~/.llm-router/usage.db`` held 1,681 ``routing_decisions`` rows
with one ``prompt_hash`` (sha256 of ``"test prompt"``) and the constants of
``tests/test_quality_guard.py::_create_routing_decision`` -- 294 of them written on
that single day, 222 naming ``openai/gpt-4o`` on a machine with no OpenAI key. The
rows carry no ``provenance`` and no ``session_id``, so no reader could tell them from
traffic.

The earlier defences were each *advisory*: ``LLM_ROUTER_HOME`` is set per test, and
``cost._refuse_unisolated_test_write`` refuses only when ``LLM_ROUTER_HOME`` is
UNSET -- so a leak is invisible exactly when it matters. Every one of them trusts the
code under test to resolve its path through the sandboxed variable. This module does
not trust that. It watches the *operation*:

* ``sqlite3.connect`` of any file under the real ``~/.llm-router`` -- refused;
* ``open()`` for writing, and ``os.remove`` / ``os.rename``, under the real
  ``~/.llm-router`` or ``~/.claude`` -- refused.

The real home is read from the password database, not from ``$HOME``, because
``$HOME`` is the very thing a test (or a CI wrapper) overrides.

A refusal raises ``PermissionError`` AND is recorded. Much of this codebase is
fail-open (``except Exception: pass`` around telemetry writes), so a raise alone can
be swallowed and the offending test would stay green; the conftest fixture
``_real_home_untouched`` fails the test from the record instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _true_home() -> Path:
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (ImportError, KeyError):  # Windows has no pwd
        return Path(os.path.expanduser("~")).resolve()


TRUE_HOME: Path = _true_home()

#: directories under the real home that a test run may not write to.
PROTECTED_WRITE: list[Path] = [TRUE_HOME / ".llm-router", TRUE_HOME / ".claude"]
#: directories in which even *opening a database* is refused.
PROTECTED_SQLITE: list[Path] = [TRUE_HOME / ".llm-router"]

#: every refusal this process has made, as (action, path).
VIOLATIONS: list[tuple[str, str]] = []

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


_ROOT_CACHE: dict[tuple[str, ...], list[str]] = {}


def _roots(roots: list[Path]) -> list[str]:
    """Lexical + resolved spelling of each root, cached: ``realpath`` lstat's, and the
    syscall-sequence tests must not see this module's own filesystem calls."""
    key = tuple(str(r) for r in roots)
    hit = _ROOT_CACHE.get(key)
    if hit is None:
        hit = []
        for root in key:
            hit.append(os.path.abspath(root))
            hit.append(os.path.realpath(root))
        _ROOT_CACHE[key] = hit
    return hit


def _inside(path: object, roots: list[Path], *, resolve_links: bool) -> bool:
    """Is ``path`` under one of ``roots``?

    ``resolve_links`` costs filesystem calls (``realpath`` lstat's every component), so
    it is used only for ``sqlite3.connect`` -- rare, and the call that matters. The
    ``open`` / ``os.remove`` / ``os.rename`` events fire on every file operation in the
    process, and some tests (``test_kpi_hook_latency``) assert the exact syscall
    sequence of a hot path, so those use a purely lexical check: ``abspath`` makes
    no filesystem call. A symlink INTO a protected dir is therefore only caught for
    databases, which is the failure that was observed.
    """
    if isinstance(path, int) or path is None:
        return False  # a file descriptor or ":memory:"-like; nothing to resolve
    try:
        raw = os.fsdecode(path)  # type: ignore[arg-type]
    except TypeError:
        return False
    if not raw or raw == ":memory:" or raw.startswith("file::memory:"):
        return False
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    cand = os.path.realpath(raw) if resolve_links else os.path.abspath(raw)
    for r in _roots(roots):
        if cand == r or cand.startswith(r + os.sep):
            return True
    return False


def _is_write(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _refuse(action: str, path: object) -> None:
    VIOLATIONS.append((action, os.fsdecode(path)))  # type: ignore[arg-type]
    raise PermissionError(
        f"test run refused: {action} {os.fsdecode(path)!r} is inside the operator's real "  # type: ignore[arg-type]
        "state directory. Tests must resolve state through LLM_ROUTER_HOME / tmp_path."
    )


def audit(event: str, args: tuple) -> None:
    """``sys.addaudithook`` callback. Cheap on the common path (string checks only)."""
    if event == "open":
        path, mode, flags = args[0], args[1], args[2] if len(args) > 2 else None
        if _is_write(mode, flags) and _inside(path, PROTECTED_WRITE, resolve_links=False):
            _refuse("open-for-write", path)
    elif event == "sqlite3.connect":
        if _inside(args[0], PROTECTED_SQLITE, resolve_links=True):
            _refuse("sqlite3.connect", args[0])
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.truncate"):
        if _inside(args[0], PROTECTED_WRITE, resolve_links=False):
            _refuse(event, args[0])
    elif event == "os.rename":  # also os.replace and shutil.move
        for target in args[:2]:  # source AND destination: atomic writes rename INTO the dir
            if _inside(target, PROTECTED_WRITE, resolve_links=False):
                _refuse(event, target)


_installed = False


def install() -> None:
    """Install the audit hook once per process (hooks cannot be removed)."""
    global _installed
    if not _installed:
        _roots(PROTECTED_WRITE)  # warm the cache outside any test's syscall trace
        _roots(PROTECTED_SQLITE)
        sys.addaudithook(audit)
        _installed = True
