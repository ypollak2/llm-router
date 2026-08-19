"""SQLite's busy-timeout must stay strictly below pytest-timeout's deadline.

WHY THIS EXISTS
===============

Two settings, each sensible in isolation, set to the same number:

    execution_ledger._connect()   sqlite3.connect(..., timeout=30.0)
    pyproject.toml [pytest]       timeout = 30

Equal values mean a test that enters SQLite's busy-wait is killed at the precise
instant SQLite would still be waiting. The wait can never complete, so the test
can never recover from even a brief lock. It does not fail with `database is
locked` — it dies mid-wait, which looks like a hang:

    execution_ledger.py record_event -> conn.commit()
    Failed: Timeout (>30.0s) from pytest-timeout

Ten soak tests died exactly that way, identically, at setup, in one CI run, after
five consecutive green runs.

The busy-timeout had been raised 5s -> 30s deliberately, to survive "pathological
CI-runner load". That was a correct fix for a real problem. Raising it TO THE TEST
TIMEOUT is what made it unsafe, and nothing connected the two numbers, so nothing
could have noticed.

THE RULE
========

`_BUSY_TIMEOUT_S` must be strictly less than the pytest timeout, with real margin
— enough that after the longest legitimate lock wait, the test still has time to
do its work. Equality is the defect; "just under" is nearly as bad, because a
test that spends its entire budget waiting on a lock has none left to run in.

This is the third "two settings that must disagree" defect found this week, after
the CI/G-D provider-env mismatch and the backslash-escaping that means one thing
in bash and another in pwsh. The shape is always the same: two correct local
decisions, no shared constraint between them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from llm_router.execution_ledger import _BUSY_TIMEOUT_S

_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"

#: Minimum gap, seconds. A test whose entire budget is consumed by a lock wait has
#: nothing left to run in, so "strictly less" is not sufficient on its own.
_MIN_MARGIN_S = 5.0


def _pytest_timeout() -> float:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    value = ini.get("timeout")
    if value is None:
        pytest.fail(
            "no `timeout` under [tool.pytest.ini_options] in pyproject.toml. "
            "Either pytest-timeout is no longer configured — in which case this "
            "test cannot check the relationship it exists for — or it moved and "
            "this test needs updating. A silently vacuous guard is worse than none."
        )
    return float(value)


def test_busy_timeout_is_strictly_below_the_test_timeout():
    pytest_timeout = _pytest_timeout()
    assert _BUSY_TIMEOUT_S < pytest_timeout, (
        f"SQLite busy-timeout ({_BUSY_TIMEOUT_S}s) is not below pytest-timeout "
        f"({pytest_timeout}s).\n"
        f"Equal or greater means a test entering the busy-wait is killed while "
        f"SQLite is still legitimately waiting — it dies mid-commit rather than "
        f"failing usefully, and looks like a hang. That is what took ten soak "
        f"tests out at once.\n"
        f"Lower _BUSY_TIMEOUT_S, or raise the pytest timeout — but not to the "
        f"same number."
    )


def test_the_gap_is_large_enough_to_be_useful():
    pytest_timeout = _pytest_timeout()
    margin = pytest_timeout - _BUSY_TIMEOUT_S
    assert margin >= _MIN_MARGIN_S, (
        f"only {margin}s between the busy-timeout ({_BUSY_TIMEOUT_S}s) and the "
        f"test timeout ({pytest_timeout}s). A test that spends its whole budget "
        f"waiting on a lock has none left to do its work in, so 'strictly less' "
        f"is not enough — at least {_MIN_MARGIN_S}s of headroom is needed."
    )


def test_busy_timeout_still_exceeds_the_value_that_was_failing():
    """Guards the fix against being 'fixed' back into the original bug.

    The busy-timeout was 5s and errored with `database is locked` under CI load.
    Anyone resolving this test by lowering _BUSY_TIMEOUT_S far enough would
    reintroduce that, having satisfied the two assertions above.
    """
    assert _BUSY_TIMEOUT_S > 5.0, (
        f"_BUSY_TIMEOUT_S is {_BUSY_TIMEOUT_S}s, at or below the 5s that "
        f"originally failed with `database is locked` under CI-runner load. "
        f"Raise the pytest timeout instead of lowering this further."
    )
