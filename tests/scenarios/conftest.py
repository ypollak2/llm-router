"""Pytest plumbing for scenario collection + end-of-run report generation.

Uses a module-global singleton collector — pytest hook discovery via
fixture cache walking was unreliable across pytest 8/9, so we lean on
a plain global.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.scenarios.core import ScenarioCollector
from tests.scenarios.reporter import write_report


_REPORT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "Docs"
    / "SCENARIO_REPORT.md"
)

# Module-global collector — pytest fixture wraps this so tests get
# dependency injection while pytest_sessionfinish can still read it.
_COLLECTOR = ScenarioCollector()


@pytest.fixture(scope="session")
def scenario_collector() -> ScenarioCollector:
    """One collector for the whole session — every scenario test feeds it."""
    return _COLLECTOR


def pytest_sessionfinish(session, exitstatus):
    """At session end, dump all collected scenarios into Docs/SCENARIO_REPORT.md."""
    if len(_COLLECTOR) == 0:
        return
    write_report(_COLLECTOR.scenarios, _REPORT_PATH)
    print(
        f"\n📜 Scenario report written: {_REPORT_PATH} "
        f"({len(_COLLECTOR)} scenarios, "
        f"{sum(1 for s in _COLLECTOR.scenarios if s.passed)} passed)"
    )
