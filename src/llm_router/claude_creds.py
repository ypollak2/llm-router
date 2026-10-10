"""The one module that talks to the OS keychain (P1.8-c: no other file in src/,
hooks/ or .claude/ may name the macOS `find-generic-password` call).

This branch holds only the generic keychain query that key discovery and the
keychain secrets backend share. ``read_oauth()`` for Claude's credentials lands
in the readers branch, in this same file.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Callable

Run = Callable[..., "subprocess.CompletedProcess"]


def keychain_query(service: str, *, want_secret: bool, platform: str | None = None,
                   run: Run = subprocess.run) -> tuple[bool, str | None]:
    """(found, secret). ``secret`` is only returned when ``want_secret`` is true.

    macOS: ``security find-generic-password -s <service>`` (``-w`` adds the
    secret). Linux: ``secret-tool lookup service <service>`` (prints the secret,
    which is dropped unless wanted). Other platforms: not found.
    """
    plat = platform or sys.platform
    if plat == "darwin":
        argv = ["security", "find-generic-password", "-s", service] + (["-w"] if want_secret else [])
    elif plat.startswith("linux"):
        argv = ["secret-tool", "lookup", "service", service]
    else:
        return False, None
    try:
        proc = run(argv, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if proc.returncode != 0:
        return False, None
    secret = (proc.stdout or "").strip() or None if want_secret else None
    return True, secret
