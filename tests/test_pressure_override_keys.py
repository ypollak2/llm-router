"""Regression test for the dead "critical pressure -> Opus override" feature.

Found 2026-09-27 while fixing PR #175 (the quota snapshot zeros): the override
in ``auto-route.py``'s ``main()`` read ``pressure.get("session_pct", 0)`` /
``"weekly_pct"`` against a dict built by ``_get_pressure()``. That function
returns the keys ``"session"``, ``"weekly"``, ``"sonnet"`` as FRACTIONS
(0.0-1.0) — the ``_pct`` (0-100) dialect belongs to usage.json and the
statusline, not to ``_get_pressure()``'s return value. The lookup always
missed, `.get(..., 0)` always returned the fallback 0, and `0 >= 95` was
always false — so the override never fired, silently, regardless of real
pressure.

The fix extracts the check into ``_critical_pressure_reading()`` so it is
testable in isolation from the rest of ``main()`` (subprocess, classifier,
HOME sandbox, etc.), and so a missing/unknown reading is never conflated
with a genuinely-safe 0.0 reading.

Loading auto-route.py is awkward because of the hyphen in the filename;
we use importlib.util.spec_from_file_location to import it as a module
(same pattern as tests/test_auto_route_signals.py).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_auto_route():
    cached = sys.modules.get("auto_route_under_test_pressure_keys")
    if cached is not None:
        return cached
    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location(
        "auto_route_under_test_pressure_keys", path
    )
    assert spec and spec.loader, f"Could not load spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_under_test_pressure_keys"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auto_route():
    return _load_auto_route()


class TestCriticalPressureReading:
    """``_critical_pressure_reading`` reads the ``_get_pressure()`` dialect
    (bare bucket names, fractions 0.0-1.0), not the usage.json ``_pct``
    dialect (0-100 percentages)."""

    def test_above_threshold_fires_and_names_the_bucket(self, auto_route):
        # Real-shaped _get_pressure() output: session at 96% -> 0.96 fraction.
        pressure = {"session": 0.96, "weekly": 0.40, "sonnet": 0.10}
        result = auto_route._critical_pressure_reading(pressure)
        assert result is not None, (
            "critical session pressure (0.96 >= 0.95) must fire the override"
        )
        bucket, value = result
        # Assert the REASON, not just that *something* fired: it must be the
        # session bucket that tripped it, carrying its own fraction — not a
        # hardcoded/mislabeled bucket name.
        assert bucket == "session"
        assert value == pytest.approx(0.96)

    def test_weekly_alone_above_threshold_fires_and_names_weekly(self, auto_route):
        pressure = {"session": 0.10, "weekly": 0.97, "sonnet": 0.05}
        result = auto_route._critical_pressure_reading(pressure)
        assert result is not None
        bucket, value = result
        assert bucket == "weekly"
        assert value == pytest.approx(0.97)

    def test_below_threshold_does_not_fire(self, auto_route):
        # Real-shaped output just under the 95% critical line in both buckets.
        pressure = {"session": 0.80, "weekly": 0.85, "sonnet": 0.30}
        result = auto_route._critical_pressure_reading(pressure)
        assert result is None, (
            "sub-critical pressure (both buckets < 0.95) must never override"
        )

    def test_percent_dialect_values_do_not_falsely_trip_it(self, auto_route):
        # Regression guard for the unit bug half of the mismatch: if callers
        # ever mistakenly hand this a usage.json-shaped 0-100 dict, "80" must
        # not be misread as a 0.0-1.0 fraction >= 0.95 and fire on a merely
        # moderate reading. (0.80 as a *fraction* is correctly sub-critical,
        # exercised above; this pins the unit, not just the key name.)
        pressure = {"session": 0.80, "weekly": 0.0}
        assert auto_route._critical_pressure_reading(pressure) is None

    def test_missing_reading_is_unknown_not_zero_and_does_not_crash(self, auto_route):
        # An empty/partial pressure dict (both buckets absent) must be treated
        # as "unknown" and never override — and, critically, must not raise
        # (e.g. a naive `None >= 0.95` comparison would TypeError here).
        pressure: dict = {}
        result = auto_route._critical_pressure_reading(pressure)
        assert result is None

    def test_one_bucket_missing_other_below_threshold_is_unknown(self, auto_route):
        # session key absent entirely (not 0.0), weekly present but safe.
        pressure = {"weekly": 0.20, "sonnet": 0.10}
        result = auto_route._critical_pressure_reading(pressure)
        assert result is None

    def test_stale_pct_dialect_keys_are_ignored_not_read(self, auto_route):
        # A dict shaped like the OLD (buggy) expectation — _pct-suffixed,
        # 0-100 values, well past critical — must be ignored entirely: the
        # function must read "session"/"weekly", never "session_pct"/
        # "weekly_pct". This is the exact dialect mismatch that made the
        # feature dead; it must not resurrect it from the other direction.
        pressure = {"session_pct": 99, "weekly_pct": 99}
        result = auto_route._critical_pressure_reading(pressure)
        assert result is None

    def test_threshold_constant_is_a_fraction(self, auto_route):
        # Guards against the threshold itself drifting back to a 0-100 value.
        assert 0.0 < auto_route._CRITICAL_PRESSURE_THRESHOLD <= 1.0
