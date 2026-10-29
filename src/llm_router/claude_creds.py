"""The one module that talks to the OS keychain (P1.8-c: no other file in src/,
hooks/ or .claude/ may name the macOS `find-generic-password` call).

``keychain_query`` is the generic query that key discovery and the keychain
secrets backend share. ``read_oauth`` reads Claude Code's own OAuth credentials
(macOS keychain, then ``~/.claude/.credentials.json``) for the usage readers.

Stdlib only: the installer copies this file next to the hooks as
``llm_router_claude_creds.py`` so a hook can load it by path when ``llm_router``
is not importable. Nothing here logs, prints or persists a credential value.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

Run = Callable[..., "subprocess.CompletedProcess"]

CLAUDE_SERVICE = "Claude Code-credentials"


def keychain_query(service: str, *, want_secret: bool, platform: str | None = None,
                   run: Run | None = None, timeout: float = 3,
                   env: dict | None = None) -> tuple[bool, str | None]:
    """(found, secret). ``secret`` is only returned when ``want_secret`` is true.

    macOS: ``security find-generic-password -s <service>`` (``-w`` adds the
    secret). Linux: ``secret-tool lookup service <service>`` (prints the secret,
    which is dropped unless wanted). Other platforms: not found.
    """
    plat = platform or sys.platform
    run = run or subprocess.run  # resolved at call time so tests can patch it
    if plat == "darwin":
        argv = ["security", "find-generic-password", "-s", service] + (["-w"] if want_secret else [])
    elif plat.startswith("linux"):
        argv = ["secret-tool", "lookup", "service", service]
    else:
        return False, None
    try:
        kw = {} if env is None else {"env": env}
        proc = run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                   stdin=subprocess.DEVNULL, **kw)  # P212-4: never read the MCP stdio stream
    except (OSError, subprocess.SubprocessError):
        return False, None
    if proc.returncode != 0:
        return False, None
    secret = (proc.stdout or "").strip() or None if want_secret else None
    return True, secret


def _oauth_block(data: object) -> dict | None:
    """The OAuth info from parsed credentials JSON.

    Nested shape ``{"claudeAiOauth": {...}}`` -> the inner dict (``{}`` when the
    key is absent or not an object); flat shape (``accessToken`` at the top
    level) -> the dict itself. Anything that is not a JSON object -> None.
    """
    if not isinstance(data, dict):
        return None
    inner = data.get("claudeAiOauth")
    if isinstance(inner, dict):
        return inner
    if "accessToken" in data:
        return data
    return {}


def read_oauth(*, platform: str | None = None, run: Run | None = None,
               home: Path | None = None, timeout: float = 4,
               env: dict | None = None) -> dict | None:
    """Claude Code's OAuth info (``accessToken``, ``expiresAt``,
    ``subscriptionType`` ... whatever the file holds) or None when unreadable.

    Order: macOS keychain (service ``Claude Code-credentials``) first, then
    ``<home>/.claude/.credentials.json`` on every platform. A source that is
    missing, empty or malformed falls through to the next. The returned dict may
    lack ``accessToken`` (credentials present, no token): callers decide.
    The caller owns the value; this function never logs, prints or stores it.
    """
    plat = platform or sys.platform
    if plat == "darwin":
        found, raw = keychain_query(CLAUDE_SERVICE, want_secret=True, platform=plat,
                                    run=run, timeout=timeout, env=env)
        if found and raw:
            try:
                block = _oauth_block(json.loads(raw))
            except ValueError:
                block = None
            if block is not None:
                return block
    path = (home if home is not None else Path.home()) / ".claude" / ".credentials.json"
    try:
        block = _oauth_block(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    return block
