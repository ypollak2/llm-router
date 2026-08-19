"""G-F class C1, part 2 — the remaining fail-open sites.

Part 1 (`test_gf_c1_failopen_codes.py`) covered the three router sites and closed 11 of
11 targeted mutants once it asserted the recorded EXCEPTION TYPE alongside the code.
This file applies the same three-part assertion to the rest:

    1. the code recorded is THIS site's code
    2. the exception TYPE reached the store
    3. the caller sees the documented degraded value

Each of these functions fails open to a value that looks like an ordinary answer, which
is exactly why the record matters. Their own comments say so:

* `_get_team_identity` -> ("", "")   "rows are attributed to nobody. Team reporting then
  shows a plausible, quietly incomplete picture."
* `_coverage_counts`   -> zeros      "Zero denominators make every rate render Unknown
  downstream (correct), but nothing said the telemetry itself was broken."
* `_host_opus_rates`   -> (0.0, 0.0) "makes every baseline read as free, so savings
  compute as zero and the ledger reports a quiet, plausible nothing."

A silent degrade that returns a plausible number is the RED2-02 shape this campaign
exists to prevent. The fail-open record is the only thing separating "no savings" from
"savings could not be computed".
"""

from __future__ import annotations

import json

import pytest

from llm_router import failopen
from llm_router.paths import is_isolated


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """An isolated fail-open store, proven isolated before anything writes to it."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
    failopen.reset_cache()
    failopen.clear()
    yield failopen
    failopen.reset_cache()


def _codes(store) -> dict[str, int]:
    store.reset_cache()
    return dict(store.snapshot().by_code)


def _exception_types(store) -> list[str]:
    """Recorded exception type names, parsed rather than substring-matched.

    The store serialises with `separators=(",", ":")`, so `'"e": "X"' in raw_text` finds
    nothing — a mistake made once already in part 1. Parsing states the claim.
    """
    store.reset_cache()
    return [
        json.loads(line)["e"]
        for line in store.store_path().read_text().splitlines()
        if line.strip() and "e" in json.loads(line)
    ]


class TestTeamIdentity:
    """`CHZ-FO-COST-IDENTITY` — degrades to ("", ""), attributing rows to nobody."""

    def _break_identity(self, monkeypatch):
        import llm_router.team as team
        monkeypatch.setattr(
            team, "get_user_id",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("identity backend down")),
        )

    def test_records_its_own_code_and_degrades_to_empty_identity(self, store, monkeypatch):
        from llm_router import cost

        self._break_identity(monkeypatch)
        assert cost._get_team_identity() == ("", "")
        assert _codes(store) == {"CHZ-FO-COST-IDENTITY": 1}

    def test_the_exception_type_is_recorded(self, store, monkeypatch):
        from llm_router import cost

        self._break_identity(monkeypatch)
        cost._get_team_identity()
        assert _exception_types(store) == ["RuntimeError"]

    def test_a_working_identity_records_nothing(self, store):
        """Pins that the record marks a DEGRADE, not every call.

        A mutant recording unconditionally would satisfy both tests above.
        """
        from llm_router import cost

        cost._get_team_identity()
        assert _codes(store) == {}


class TestCoverageCounts:
    """`CHZ-FO-COST-COVERAGE-COUNTS` — degrades to zero denominators.

    WP-07's point: a rate without its denominator "silently redefines itself when
    routing degrades". Zeros render as Unknown downstream, which is correct — but only
    the fail-open record distinguishes "no traffic" from "could not read the telemetry".
    """

    def test_records_its_own_code_and_degrades_to_zeros(self, store, monkeypatch):
        import llm_router.coverage as coverage
        from llm_router import cost

        monkeypatch.setattr(
            coverage, "snapshot",
            lambda: (_ for _ in ()).throw(OSError("coverage store unreadable")),
        )
        assert cost._coverage_counts() == {"observed_n": 0, "unobserved_n": 0}
        assert _codes(store) == {"CHZ-FO-COST-COVERAGE-COUNTS": 1}

    def test_the_exception_type_is_recorded(self, store, monkeypatch):
        import llm_router.coverage as coverage
        from llm_router import cost

        monkeypatch.setattr(
            coverage, "snapshot",
            lambda: (_ for _ in ()).throw(OSError("coverage store unreadable")),
        )
        cost._coverage_counts()
        assert _exception_types(store) == ["OSError"]

    def test_a_working_snapshot_records_nothing(self, store):
        from llm_router import cost

        counts = cost._coverage_counts()
        assert set(counts) == {"observed_n", "unobserved_n"}
        assert _codes(store) == {}


class TestLedgerHostRates:
    """`CHZ-FO-LEDGER-HOST-RATES` — degrades to (0.0, 0.0).

    The most consequential of the three: a zero baseline makes every routed call look
    like it saved nothing, so the ledger reports "a quiet, plausible nothing" rather
    than an error. Savings collapsing to zero needs a cause an operator can find.
    """

    def test_records_its_own_code_and_degrades_to_zero_rates(self, store, monkeypatch):
        import sys

        import llm_router.execution_ledger as el

        # `from llm_router.cost import _HOST_INPUT_PER_M` resolves the SUBMODULE via
        # sys.modules; setting the entry to None makes the import raise. The package
        # attribute does not need deleting here because `llm_router.cost` is imported as a
        # module object, not rebound — unlike the `from llm_router import calibration` case
        # in part 1, where the attribute lookup succeeded first. Checked, not assumed.
        monkeypatch.setitem(sys.modules, "llm_router.cost", None)
        assert el._host_opus_rates() == (0.0, 0.0)
        assert _codes(store) == {"CHZ-FO-LEDGER-HOST-RATES": 1}

    def test_the_exception_type_is_recorded(self, store, monkeypatch):
        import sys

        import llm_router.execution_ledger as el

        monkeypatch.setitem(sys.modules, "llm_router.cost", None)
        el._host_opus_rates()
        # ModuleNotFoundError, not ImportError: a None entry in sys.modules raises the
        # SUBCLASS. Part 1 of this class made the same wrong assumption and corrected
        # it; writing part 2 reproduced it. The concrete type is what an operator reads
        # out of the store, so the test pins the concrete type.
        assert _exception_types(store) == ["ModuleNotFoundError"]

    def test_working_rates_are_positive_and_record_nothing(self, store):
        import llm_router.execution_ledger as el

        in_rate, out_rate = el._host_opus_rates()
        assert in_rate > 0 and out_rate > 0, (
            "a zero baseline is the degraded value; the healthy path must not return it"
        )
        assert _codes(store) == {}

    def test_the_two_rates_are_not_swapped(self, store):
        """B1 in this audit was Opus input/output rates INVERTED.

        Output tokens cost more than input on every Claude model, so the relationship
        is a real invariant rather than a property of today's numbers.
        """
        import llm_router.execution_ledger as el

        in_rate, out_rate = el._host_opus_rates()
        assert out_rate > in_rate


class TestCodesAreDistinctAcrossSites:
    """Three different degradations must not report as one.

    Every single-site test above would still pass if a mutant swapped this site's code
    for another site's — each asserts only its own code in isolation.
    """

    def test_three_sites_record_three_different_codes(self, store, monkeypatch):
        import sys

        import llm_router.coverage as coverage
        import llm_router.execution_ledger as el
        import llm_router.team as team
        from llm_router import cost

        monkeypatch.setattr(
            team, "get_user_id",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("x")),
        )
        monkeypatch.setattr(
            coverage, "snapshot", lambda: (_ for _ in ()).throw(OSError("y")),
        )
        monkeypatch.setitem(sys.modules, "llm_router.cost", None)

        cost._get_team_identity()
        cost._coverage_counts()
        el._host_opus_rates()

        assert _codes(store) == {
            "CHZ-FO-COST-IDENTITY": 1,
            "CHZ-FO-COST-COVERAGE-COUNTS": 1,
            "CHZ-FO-LEDGER-HOST-RATES": 1,
        }
