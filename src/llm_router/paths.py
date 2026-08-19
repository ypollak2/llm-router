"""RED2-07 — one resolver for the llm_router state directory, honouring LLM_ROUTER_HOME.

`LLM_ROUTER_HOME` looked like the way to point llm_router at a scratch directory. It was
not: nothing read it. ~149 call sites compute ``Path.home() / ".llm-router"``
directly, and ``config.llm_router_db_path`` was worse than that — a field default
evaluated at *class definition* time, so it froze the real home directory at
import and could not be redirected afterwards even by monkeypatching
``Path.home()``.

This is not a tidiness complaint. During the audit a test that believed it was
sandboxed by `LLM_ROUTER_HOME` wrote to the operator's real `~/.llm-router/usage.db` and
destroyed live data (`evidence/AUDITOR_INCIDENT.md`). A safety mechanism that
silently does nothing is more dangerous than no mechanism, because people rely
on it.

Resolution order:

1. ``LLM_ROUTER_HOME`` if set — read at CALL time, never cached, so a test that sets
   it after import still gets isolation. Caching here would reintroduce exactly
   the freeze that caused the incident.
2. ``~/.llm-router`` otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "LLM_ROUTER_HOME"


def llm_router_home() -> Path:
    """The llm_router state directory. Resolved on every call, deliberately."""
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".llm-router"


def state_path(*parts: str) -> Path:
    """A path inside the llm_router state directory, e.g. ``state_path("usage.db")``."""
    return llm_router_home().joinpath(*parts)


def is_isolated() -> bool:
    """True when ``LLM_ROUTER_HOME`` is set and this module will honour it.

    SCOPE — READ THIS BEFORE TRUSTING IT (audit #37).

    This asserts one thing only: that ``llm_router_home()`` and ``state_path()`` will
    resolve under ``LLM_ROUTER_HOME``. It does **not** certify that the process as a
    whole is sandboxed, because most of the codebase does not ask this module
    where state lives.

    Surveyed 2026-08-15: **120 sites in ``src/llm_router/`` compose ``~/.llm-router``
    directly**, plus 55 more in ``src/llm_router/hooks/`` which run as separate
    processes. ``usage.db`` alone is resolved ~23 different ways. Four modules
    honour an override, and each honours a *different* variable
    (``LLM_ROUTER_STATE_DIR``, ``LLM_ROUTER_EXECUTION_LEDGER_DB``, ``LLM_ROUTER_CP_AUDIT_PATH``,
    ``LLM_ROUTER_DB_PATH``); none honours ``LLM_ROUTER_HOME``.

    So a test that asserts ``is_isolated()`` and then exercises a module which
    resolves its own path is **not** protected. That is not hypothetical: it is
    exactly how ``session_store.py`` read the operator's real session content while
    a test believed it was sandboxed, and how the incident in
    ``evidence/AUDITOR_INCIDENT.md`` destroyed live data.

    Assert it to check YOUR OWN writes go through ``state_path()``. Do not read it
    as "nothing can escape". The honest name for what this returns is "the
    canonical resolver is redirected", and narrowing the claim is the point of this
    docstring — a guard that over-claims is how a local bug becomes a silent one.
    """
    return bool(os.environ.get(ENV_VAR, "").strip())


def private_opener(path: str, flags: int) -> int:
    """``open()`` opener that creates files at 0600 instead of the umask default.

    WHY THIS EXISTS
    ---------------
    The codebase's established idiom for a private state file is::

        with open(path, "a") as fh:
            fh.write(secret)
        os.chmod(path, 0o600)

    which is correct at rest and wrong in between. ``open`` creates the file
    with ``0666 & ~umask`` — 0644 on a default umask — so on FIRST creation the
    file is world-readable for the whole write, and only tightened afterwards.
    Anything that opens it inside that window keeps a readable handle even after
    the chmod, because permissions are checked at open time and not on each read.

    Measured, not assumed::

        open(path, "a") then chmod : mode while writing = 0o644
        open(..., opener=...)      : mode while writing = 0o600

    The window is short and needs local access, so this is a hardening fix rather
    than an urgent one — but it costs a keyword argument, and the files in
    question hold a dashboard auth token and scrubbed prompt transcripts.

    Usage::

        with open(path, "a", encoding="utf-8", opener=private_opener) as fh:
            ...

    NOTE: an opener sets the mode only when it CREATES the file. An existing file
    keeps its current mode, so this hardens first creation and is not a repair
    for a file already written at 0644 — keep the ``os.chmod`` alongside it where
    one is already present, which also fixes files created by older versions.
    """
    return os.open(path, flags, 0o600)
