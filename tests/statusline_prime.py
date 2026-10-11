"""Helper for tests that need the FULL statusline line.

Since P0.9-c repair round 2 the first render of a session never builds the segment
cache inline: it starts a detached build and prints ``segments pending``. A test of
what the segments SAY therefore renders once to start the build, waits for the cache
file, and renders again. The wait is on a file appearing (a condition, not a sleep).
"""

from __future__ import annotations

import time
from pathlib import Path


def wait_for_cache(home: Path, timeout: float = 30.0) -> bool:
    state = home / ".llm-router"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if any(state.glob("statusline_seg_*.kv")):
            return True
        time.sleep(0.05)
    return False


def render_full(run, home: Path):
    """``run()`` once (starts the detached build), wait for the cache, ``run()`` again."""
    run()
    wait_for_cache(home)
    return run()
