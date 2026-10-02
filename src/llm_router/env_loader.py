"""One ``.env`` loader, shared by every process entry point.

WHY THIS EXISTS
================

Before this module, four hook scripts (``auto-route.py``, ``session-start.py``,
``agent-route.py``, ``stop-enforce.py``) each carried their own hand-copied
``_load_dotenv()``, and every other entry point — the MCP server
(``server.py``), the CLI (``cli.py``), the per-call proxy (``proxy/server.py``),
and roughly a dozen other hook scripts — had none at all. A setting such as
``LLM_ROUTER_SEMANTIC_HISTORY=shadow`` in ``~/.llm-router/.env`` therefore took
effect only in the handful of processes that happened to carry a copy-pasted
loader, and silently did nothing anywhere else — including ``llm-router
semantic status`` and the MCP server's own ``llm()``/``llm_route`` path, the
main production choke point for the semantic layer
(``semantic_audit/REPORT.md``, section 1).

This module is the single implementation. Every entry point that needs
``.env`` values in ``os.environ`` calls :func:`load_dotenv_files` instead of
rolling its own parser.

PRECEDENCE
==========

The real process environment always wins. A key already present in
``os.environ`` — set by the shell, a launchd/systemd unit, or Claude Code's own
MCP server config — is never overwritten. Within ``.env`` files, later paths in
:func:`candidate_env_paths` do not override earlier ones either; the first file
to set a key wins, same as the loaders this replaces.

SEC-002/003 — a project's own ``.env`` is repository content, not the user's
config. From the current working directory's ``.env`` only, keys are filtered
through :func:`project_env_may_set` so a cloned repo cannot inject
``PYTHONPATH``, ``DYLD_INSERT_LIBRARIES``, or an endpoint override that
redirects provider traffic. ``$LLM_ROUTER_HOME/.env`` and ``~/.env`` are the
user's own files and load unconditionally.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from llm_router import paths

# Mirrors config.py's SEC-002/003 rule exactly (kept independent rather than
# imported, so this module has no dependency on the pydantic settings stack
# and stays cheap to import from every hook's hot path).
_ENDPOINT_KEY = re.compile(
    r"(_URL|_BASE|_HOST|_ENDPOINT|_WEBHOOK)$|^(HTTPS?_PROXY|ALL_PROXY|NO_PROXY)$",
    re.IGNORECASE,
)
_PROJECT_ENV_ALLOWED = re.compile(r"(_API_KEY|_API_TOKEN)$|^LLM_ROUTER_", re.IGNORECASE)


def is_endpoint_key(name: str) -> bool:
    """True for a variable that decides WHERE requests go, not who pays."""
    return bool(_ENDPOINT_KEY.search(name.strip()))


def project_env_may_set(name: str) -> bool:
    """What a project ``.env`` may inject into a PROCESS environment.

    An allowlist, not the endpoint denylist: these values are copied into
    ``os.environ``, which every child process inherits, so ``PYTHONPATH``,
    ``NODE_OPTIONS``, ``DYLD_INSERT_LIBRARIES`` or a CA bundle from a cloned
    repo would run or intercept code. Provider API keys and non-endpoint
    ``LLM_ROUTER_*`` settings are all a project legitimately needs.
    """
    n = name.strip()
    return bool(_PROJECT_ENV_ALLOWED.search(n)) and not is_endpoint_key(n)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines. Blank lines, ``#`` comments, and lines with
    no ``=`` are skipped. Values are stripped of surrounding quotes."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError: a
        return out  # binary .env must not crash the CLI, server or proxy
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            out[key] = value
    return out


def candidate_env_paths(
    cwd: Path | None = None, extra_paths: "list[Path] | None" = None
) -> "list[Path]":
    """The ``.env`` files consulted, in priority order (first match per key wins).

    1. the working directory's ``.env`` — project content, filtered.
    2. ``$LLM_ROUTER_HOME/.env`` (``~/.llm-router/.env`` by default) — the
       user's own settings.
    3. ``~/.env`` — a user-level fallback some installs use.
    4. any ``extra_paths`` the caller adds (e.g. a dev-tree checkout path).
    """
    cwd = cwd if cwd is not None else Path.cwd()
    result = [cwd / ".env", paths.state_path(".env"), Path.home() / ".env"]
    if extra_paths:
        result.extend(extra_paths)
    return result


def load_dotenv_files(
    cwd: "Path | None" = None,
    extra_paths: "list[Path] | None" = None,
    target: "dict[str, str] | None" = None,
) -> "dict[str, str]":
    """Load ``.env`` files into ``target`` (default: ``os.environ``). Real env always wins.

    ``target`` lets a caller load into a plain dict instead of the real process
    environment — used by tests that exercise a hook's loader without mutating
    global state.

    Returns the keys this call actually applied (for logging/diagnostics —
    never log the values, some are credentials). Safe to call repeatedly: a
    key already in ``target``, whether from the real environment or a prior
    call here, is never touched again.
    """
    cwd = cwd if cwd is not None else Path.cwd()
    dest = os.environ if target is None else target
    project_env = cwd / ".env"
    applied: dict[str, str] = {}
    for env_path in candidate_env_paths(cwd, extra_paths):
        try:
            if not env_path.is_file():
                continue
        except OSError:
            continue
        untrusted = env_path == project_env
        for key, value in _parse_env_file(env_path).items():
            if untrusted and not project_env_may_set(key):
                continue
            if key in dest:
                continue
            dest[key] = value
            applied[key] = value
    return applied
