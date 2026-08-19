"""Repo-root conftest — downstream-only test behaviour.

WHY THIS FILE IS AT THE ROOT AND NOT IN tests/
==============================================

It was in ``tests/conftest.py`` first. The next upstream sync overwrote it,
because ``tests/conftest.py`` exists upstream too and the sync owns every file
it carries. The hook vanished silently and 45 tests went back to failing.

So: anything downstream-specific must live somewhere the sync does not write.
The repo root is outside both synced trees (``src/`` and ``tests/``), and pytest
picks up a root ``conftest.py`` automatically. Same effect, and it survives.

That is a general rule for this repository now, not a one-off: **do not put
downstream-only code in a file the sync carries.** It will be silently
reverted, and the revert looks like nothing happened.

WHAT IT DOES
============

llm-routing deliberately does not ship ``enterprise``, ``admin_api``,
``invoice_reconciliation``, ``tenant_policy_sidecar`` or the agoragentic tools
(see the upstream sync tooling's exclusion set, and the downstream sync plan §1).

The sync skips test FILES whose module-level imports name one of those. It
cannot skip a test whose import is DEFERRED — inside a fixture or a function
body — because the file imports fine and only the individual test raises.

A failure is the wrong signal there: nothing is broken. The capability is
absent by design, so the test is not applicable to this package, and
``skipped`` is the status that says exactly that. Marking it passed would be a
lie; leaving it failed trains people to ignore a red suite.

Deliberately NARROW: only ``ModuleNotFoundError``, and only for these exact
module roots. Any other ``ImportError`` still fails, because "the module is
absent by design" and "the module is broken" must never look the same.
"""

from __future__ import annotations

import pytest

_EXCLUDED_MODULE_ROOTS = (
    "llm_router.enterprise",
    "llm_router.admin_api",
    "llm_router.commands.admin_api",
    "llm_router.invoice_reconciliation",
    "llm_router.tenant_policy_sidecar",
    "llm_router.tools.agoragentic",
    "llm_router.control_plane.audit",
)


def _missing_excluded_module(exc: BaseException | None) -> str | None:
    """The excluded module root this exception is about, or None.

    Walks the ``__cause__`` / ``__context__`` chain: a fixture that wraps the
    import in its own error still has the ModuleNotFoundError underneath, and
    only checking the outermost exception misses those.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ModuleNotFoundError):
            name = getattr(exc, "name", None) or ""
            for root in _EXCLUDED_MODULE_ROOTS:
                if name == root or name.startswith(root + "."):
                    return root
        exc = exc.__cause__ or exc.__context__
    return None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.excinfo is None:
        return
    root = _missing_excluded_module(call.excinfo.value)
    if root is None:
        return
    report.outcome = "skipped"
    report.longrepr = (
        __file__,
        0,
        f"Skipped: {root} is deliberately not shipped in llm-routing "
        f"(the downstream sync plan §1). This test exercises an excluded "
        f"capability, so it is not applicable here rather than failing.",
    )
