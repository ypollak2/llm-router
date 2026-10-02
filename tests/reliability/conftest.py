"""Keep this directory's real-concurrency tests off each other's CPU.

Every test here spawns real threads or processes on purpose — that is the
whole point; see each file's module docstring. `pytest -n auto` with the
default `--dist load` is free to schedule several of these files onto
DIFFERENT xdist workers at the same instant, so e.g. test_r14's 8-process
spawn and test_ledger_concurrency's own spawns end up competing for the same
handful of CI cores as EACH OTHER, on top of the rest of the ~11k-test suite
running elsewhere. That self-inflicted stacking is what turned a 30s budget
into three straight timeouts on one worker (PR #241, run 37046968434) for a
test that takes well under a second unloaded.

`xdist_group` pins every test collected from this directory onto the SAME
xdist worker, so at most one of these files' spawns is ever in flight at a
time — it cannot remove contention from the rest of the suite (that is
inherent to running CI under `-n auto` at all, and is what the per-test
`timeout` marks in test_r14 account for), but it removes the part this
directory was doing to itself. Requires `--dist loadgroup` on the pytest
invocation (ci.yml); plain `load` ignores the group.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_GROUP = pytest.mark.xdist_group(name="reliability_concurrency")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    # A conftest hook is handed EVERY collected item in the session, not just
    # this directory's. Tagging them all put the whole suite on one worker
    # (17 min instead of ~5), so filter by path. tryfirst: xdist appends the
    # group to the nodeid in its own hook, so the marker must exist by then.
    for item in items:
        if _HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(_GROUP)
