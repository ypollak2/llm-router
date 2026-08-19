"""G-F class C1 — a fail-open site records ITS OWN code, and degrades to a known value.

WHY THIS FILE EXISTS
--------------------
The G-F baseline left 40 mutants alive across eleven `failopen.record("CHZ-FO-…", exc)`
call sites. A mutant can swap the code for another site's, upper-case it, wrap it in
mutmut's `XX…XX` marker, or replace it with `None`, and no test objects — because no test
drives these error paths at all.

This matters beyond the score. The fail-open store is the operator-facing record of which
behaviour silently degraded. A site that records the WRONG code sends an operator to the
wrong subsystem, and a site that records nothing is indistinguishable from a healthy run.
That is the RED2-02 shape the codebase already treats as a defect class elsewhere.

RELATION TO OPEN FINDING #32
----------------------------
The repo's fail-open linter (see finding #32) cannot detect a fail-open that logs, so
adding these modules to its PROTECTED set yields zero violations — it is blind here by
construction. WP-13 ("fail-open triage") used that gate as its instrument. These tests
are the independent check it cannot be: they assert observable behaviour rather than
source shape.

The linter is referred to by description rather than by its script name deliberately.
Amendment 1's exclusion rule (`scripts/gf_excluded_tests.py`, rule B) matches a gate
script's name as a plain substring anywhere in the module-level segment, so NAMING it
here — even in prose, even to explain why these tests exist — excluded this entire file
from the G-F run. A test excluded from the run cannot kill anything, so the mention would
have silently cancelled the work it was describing. Recorded in
`evidence/gf_phase3_c1_failopen.md`; the rule itself is not changed here, because
changing it requires amending the pre-registered protocol.

WHAT MAKES THE ASSERTIONS BEHAVIOURAL
-------------------------------------
Each test asserts TWO things a user or operator could observe:
  1. the exact code in `failopen.snapshot().by_code`, and
  2. the DEGRADED RETURN VALUE the caller now sees (0.0, "unknown", tier 2).

Neither is a re-implementation of production logic: the codes and the fallbacks are the
documented contract of each site. A test asserting only "something was recorded" would
pass under a mutant that swaps one site's code for another's, which is precisely the
failure being guarded against.

HERMETICITY
-----------
`failopen` writes under `state_path()`, i.e. `LLM_ROUTER_HOME`. Every test points that at
`tmp_path` and asserts `is_isolated()` before writing, so a misconfigured environment
fails loudly instead of appending to the developer's real store. `reset_cache()` is
required because `snapshot()` memoises.
"""

from __future__ import annotations

import json

import pytest

from llm_router import failopen
from llm_router.paths import is_isolated


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """An isolated fail-open store, proven isolated before any test writes to it."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
    failopen.reset_cache()
    failopen.clear()
    yield failopen
    failopen.reset_cache()


def codes(store) -> dict[str, int]:
    store.reset_cache()
    return dict(store.snapshot().by_code)


class TestRouterBaselineEstimate:
    def test_records_its_own_code_and_degrades_to_zero(self, store, monkeypatch):
        from llm_router import router

        monkeypatch.setattr(
            "llm_router.session_spend._estimate_cost",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pricing table gone")),
        )
        result = router._baseline_cost(None, None, input_tokens=1000, output_tokens=500)

        # The documented fallback: 0.0, so the comparison reads "saved nothing" rather
        # than crashing the route.
        assert result == 0.0
        assert codes(store) == {"CHZ-FO-ROUTER-BASELINE-ESTIMATE": 1}


def _break_calibration_import(monkeypatch) -> None:
    """Make `from llm_router import calibration` raise, whatever ran before this test.

    Two steps, and the second one is why this is a helper rather than one line.

    `from llm_router import calibration` first tries `getattr(llm_router, "calibration")`. Once
    ANY earlier test has imported the submodule, it is bound as an attribute of the
    package and that getattr succeeds — `sys.modules` is never consulted, no exception is
    raised, and the fail-open path is never entered.

    Setting only the `sys.modules` entry therefore passes in isolation and fails in a
    full run. That is exactly the order-dependence class `63cbc8c` was written to remove
    from this suite, reintroduced here by a test added afterwards; the full suite caught
    it, running the file alone did not.
    """
    import sys

    import llm_router

    monkeypatch.delattr(llm_router, "calibration", raising=False)
    monkeypatch.setitem(sys.modules, "llm_router.calibration", None)


class TestRouterPriceTableVersion:
    def test_records_its_own_code_and_degrades_to_unknown(self, store, monkeypatch):
        """The EXCEPTION path, not the missing-attribute path.

        `getattr(calibration, "PRICE_TABLE_VERSION", "unknown")` already returns
        "unknown" when the attribute is absent, without raising — so deleting the
        attribute would exercise the happy path and assert the same value. The import
        itself has to fail.
        """
        from llm_router import router

        _break_calibration_import(monkeypatch)
        result = router._price_table_version()

        assert result == "unknown"
        assert codes(store) == {"CHZ-FO-ROUTER-PRICE-TABLE-VERSION": 1}


class TestRouterProviderParse:
    def test_records_its_own_code_and_degrades_to_mid_tier(self, store, monkeypatch):
        from llm_router import router

        monkeypatch.setattr(
            router, "provider_from_model",
            lambda m: (_ for _ in ()).throw(ValueError("unparseable model id")),
        )
        result = router._model_tier("some::weird::model")

        # Tier 2 = "mid external API": the conservative assumption when the model
        # cannot be parsed. Tier 0 (local, free) would understate cost.
        assert result == 2
        assert codes(store) == {"CHZ-FO-ROUTER-PROVIDER-PARSE": 1}

    def test_the_offending_model_is_carried_as_detail(self, store, monkeypatch):
        """`detail=str(model)` is what makes the record actionable — without it an
        operator knows a model failed to parse but not which one."""
        from llm_router import router

        monkeypatch.setattr(
            router, "provider_from_model",
            lambda m: (_ for _ in ()).throw(ValueError("nope")),
        )
        router._model_tier("vendor::model-9000")

        store.reset_cache()
        raw = store.store_path().read_text()
        assert "vendor::model-9000" in raw


class TestTheExceptionTypeIsRecorded:
    """The second half of the record, and the half these tests originally missed.

    Asserting only the code left four mutants alive, every one of them dropping the
    exception: `record("CHZ-FO-…", )`, `record("CHZ-FO-…", None)`. `record()` writes
    `payload["e"] = type(exc).__name__` only when `exc` is not None, and
    `snapshot().by_code` aggregates by code alone — so a record that lost its exception
    is invisible through that API and every assertion above still passed.

    It matters operationally for the same reason the code does. "CHZ-FO-ROUTER-
    BASELINE-ESTIMATE fired 40 times" says a site degraded; "…40 times, all
    ConnectionError" says why. Without the type, the record names a symptom and nothing
    else.

    The store is read directly because `FailOpenCounts` deliberately exposes only
    per-code counts. That is a file this test's own fixture created inside `tmp_path`,
    not a private detail of another component.
    """

    def _exception_types(self, store) -> list[str]:
        """Recorded exception type names, parsed rather than string-matched.

        The first version asserted `'"e": "ValueError"' in <raw text>` and failed on
        every case: the store serialises with `separators=(",", ":")`, so the bytes are
        `"e":"ValueError"` with no space. Parsing states the claim — *this exception
        type was recorded* — instead of a claim about whitespace, and keeps the test out
        of the way of the serialisation format, which is separately a place where
        mutants are equivalent by construction.
        """
        store.reset_cache()
        return [
            json.loads(line)["e"]
            for line in store.store_path().read_text().splitlines()
            if line.strip() and "e" in json.loads(line)
        ]

    def test_baseline_estimate_records_the_exception_type(self, store, monkeypatch):
        from llm_router import router

        monkeypatch.setattr(
            "llm_router.session_spend._estimate_cost",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pricing table gone")),
        )
        router._baseline_cost(None, None)
        assert self._exception_types(store) == ["RuntimeError"]

    def test_price_table_version_records_the_exception_type(self, store, monkeypatch):
        from llm_router import router

        _break_calibration_import(monkeypatch)
        router._price_table_version()
        # ModuleNotFoundError, not ImportError: a None entry in sys.modules raises the
        # subclass. Asserting the exact recorded name is the point — an operator reading
        # the store gets the concrete type, so the test should pin the concrete type.
        assert self._exception_types(store) == ["ModuleNotFoundError"]

    def test_provider_parse_records_the_exception_type(self, store, monkeypatch):
        from llm_router import router

        monkeypatch.setattr(
            router, "provider_from_model",
            lambda m: (_ for _ in ()).throw(ValueError("unparseable model id")),
        )
        router._model_tier("weird::model")
        assert self._exception_types(store) == ["ValueError"]


class TestCodesAreDistinctPerSite:
    """The point of a per-SITE code: two different failures must not be conflated.

    A mutant that replaces one site's code with another's would leave every
    single-site test above passing if each only asserted "one record exists".
    """

    def test_two_different_sites_record_two_different_codes(self, store, monkeypatch):
        from llm_router import router

        monkeypatch.setattr(
            "llm_router.session_spend._estimate_cost",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
        )
        monkeypatch.setattr(
            router, "provider_from_model",
            lambda m: (_ for _ in ()).throw(ValueError("y")),
        )
        router._baseline_cost(None, None)
        router._model_tier("weird")

        assert codes(store) == {
            "CHZ-FO-ROUTER-BASELINE-ESTIMATE": 1,
            "CHZ-FO-ROUTER-PROVIDER-PARSE": 1,
        }

    def test_repeated_failures_at_one_site_increment_that_site(self, store, monkeypatch):
        from llm_router import router

        monkeypatch.setattr(
            router, "provider_from_model",
            lambda m: (_ for _ in ()).throw(ValueError("y")),
        )
        for _ in range(3):
            router._model_tier("weird")

        assert codes(store) == {"CHZ-FO-ROUTER-PROVIDER-PARSE": 3}
