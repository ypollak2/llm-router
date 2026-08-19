"""G-F — `cost.refresh_baseline_pricing_from_api` had NO test executing it.

20 mutants, all 🫥 no-coverage. The function overwrites the BASELINE prices that every
savings figure is computed against, so a mutant here does not crash anything: it makes
the reported savings wrong by a multiple and leaves them looking entirely plausible. The
function's own comment names the precedent — RED2-01, which shipped a 3x overstatement.

WHY THE GLOBALS ARE HANDLED SO CAREFULLY
----------------------------------------
This function assigns to `_HOST_INPUT_PER_M` and `_HOST_OUTPUT_PER_M` and mutates
`_OPUS_PRICING` in place. A test that lets those escape would leave every later test
pricing against fabricated numbers — the precise pollution class that made this suite
order-dependent (`63cbc8c`) and that C6's own fixture reintroduced once already.

`monkeypatch.setattr` restores the two scalars. The dict needs `monkeypatch.setitem`,
because setattr would rebind the name while an in-place `dict[key] = value` would still
have mutated the original object.

The suite asserts the restoration rather than trusting it: `test_globals_are_restored…`
fails if this file leaks.

`import anthropic` is a plain import, so `sys.modules` alone controls it — unlike the
`from llm_router import calibration` case, where the package attribute had to be deleted too.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from llm_router import cost


def _fake_anthropic(pricing) -> types.ModuleType:
    """A stand-in `anthropic` whose `models.retrieve()` returns the given pricing.

    `pricing` is attached as the model's `.pricing` attribute exactly as the SDK would
    expose it; passing `None` models an SDK response that carries no pricing at all.
    """
    mod = types.ModuleType("anthropic")

    class _Models:
        def __init__(self):
            self.retrieved: list[str] = []

        def retrieve(self, model_id):
            self.retrieved.append(model_id)
            return types.SimpleNamespace(pricing=pricing)

    class _Anthropic:
        instances: list["_Anthropic"] = []

        def __init__(self, *a, **k):
            self.models = _Models()
            _Anthropic.instances.append(self)

    mod.Anthropic = _Anthropic
    return mod


@pytest.fixture()
def pinned(monkeypatch):
    """Pin the pricing globals to known values and guarantee their restoration."""
    monkeypatch.setattr(cost, "_HOST_INPUT_PER_M", 1.0)
    monkeypatch.setattr(cost, "_HOST_OUTPUT_PER_M", 2.0)
    monkeypatch.setitem(cost._OPUS_PRICING, cost.LATEST_OPUS_MODEL, (1.0, 2.0))
    return cost


class TestSuccessfulRefresh:
    def test_returns_true_and_adopts_both_rates(self, pinned, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "anthropic",
            _fake_anthropic({"input_per_mtok": 15.0, "output_per_mtok": 75.0}),
        )
        assert cost.refresh_baseline_pricing_from_api() is True
        assert cost._HOST_INPUT_PER_M == 15.0
        assert cost._HOST_OUTPUT_PER_M == 75.0

    def test_the_two_rates_are_not_swapped(self, pinned, monkeypatch):
        """Input and output are asserted against DIFFERENT values on purpose.

        B1 in this audit was Opus input/output rates inverted. With equal fixtures a
        swap is invisible; 15 vs 75 makes it a failure.
        """
        monkeypatch.setitem(
            sys.modules, "anthropic",
            _fake_anthropic({"input_per_mtok": 15.0, "output_per_mtok": 75.0}),
        )
        cost.refresh_baseline_pricing_from_api()
        assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == (15.0, 75.0)

    def test_the_static_table_is_updated_for_the_latest_model(self, pinned, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "anthropic",
            _fake_anthropic({"input_per_mtok": 15.0, "output_per_mtok": 75.0}),
        )
        cost.refresh_baseline_pricing_from_api()
        assert cost._OPUS_PRICING[cost.LATEST_OPUS_MODEL] == (15.0, 75.0)

    def test_it_asks_the_api_for_the_latest_opus_model(self, pinned, monkeypatch):
        fake = _fake_anthropic({"input_per_mtok": 15.0, "output_per_mtok": 75.0})
        monkeypatch.setitem(sys.modules, "anthropic", fake)
        cost.refresh_baseline_pricing_from_api()
        assert fake.Anthropic.instances[-1].models.retrieved == [cost.LATEST_OPUS_MODEL]


class TestRefusalLeavesTheBaselineIntact:
    """Every failure mode must return False AND leave the static prices untouched.

    Returning False while having half-applied a price is worse than either outcome
    alone: the caller believes nothing changed.
    """

    @pytest.mark.parametrize(
        "pricing, why",
        [
            (None, "no pricing attribute at all"),
            ({}, "empty pricing block"),
            ({"input_per_mtok": 15.0}, "output rate missing"),
            ({"output_per_mtok": 75.0}, "input rate missing"),
            ({"input_per_mtok": 0, "output_per_mtok": 75.0}, "input rate zero"),
            ({"input_per_mtok": 15.0, "output_per_mtok": 0}, "output rate zero"),
        ],
    )
    def test_incomplete_pricing_is_refused(self, pinned, monkeypatch, pricing, why):
        monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(pricing))
        assert cost.refresh_baseline_pricing_from_api() is False, why
        assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == (1.0, 2.0), why
        assert cost._OPUS_PRICING[cost.LATEST_OPUS_MODEL] == (1.0, 2.0), why

    def test_an_sdk_failure_is_refused_and_recorded(self, pinned, monkeypatch, tmp_path):
        from llm_router import failopen
        from llm_router.paths import is_isolated

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
        failopen.reset_cache()
        failopen.clear()

        broken = types.ModuleType("anthropic")

        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("no credentials")

        broken.Anthropic = _Boom
        monkeypatch.setitem(sys.modules, "anthropic", broken)

        assert cost.refresh_baseline_pricing_from_api() is False
        assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == (1.0, 2.0)

        failopen.reset_cache()
        assert dict(failopen.snapshot().by_code) == {"CHZ-FO-COST-PRICING-REFRESH": 1}

        # The EXCEPTION TYPE, not only the code. Asserting the code alone left two
        # mutants alive here — `record(code, None)` and `record(code, )` — and this is
        # the same class already diagnosed and fixed for the three router sites in the
        # C1 work an hour earlier. Fixing a class in one file does not fix it in the
        # next one written; the check has to be part of how a fail-open site is tested,
        # not a patch applied once.
        recorded = [
            json.loads(line)
            for line in failopen.store_path().read_text().splitlines()
            if line.strip()
        ]
        assert [r.get("e") for r in recorded] == ["RuntimeError"]
        failopen.reset_cache()

    def test_a_model_with_no_pricing_attribute_is_refused_silently(self, pinned, monkeypatch,
                                                                   tmp_path):
        """No `pricing` attribute at all is a REFUSAL, not an error.

        `getattr(model, "pricing", None)` has a default; the two-argument form raises
        `AttributeError` instead. Both spellings return False overall, so the return
        value cannot tell them apart — but the mutant reaches the `except` branch and
        records a fail-open, while the original never does. The absence of a record is
        what distinguishes them.

        Every other fixture in this file sets `.pricing` (sometimes to None), so the
        attribute was never actually missing and this path was untested.
        """
        from llm_router import failopen
        from llm_router.paths import is_isolated

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
        failopen.reset_cache()
        failopen.clear()

        bare = types.ModuleType("anthropic")

        class _Models:
            def retrieve(self, model_id):
                return types.SimpleNamespace()      # no `pricing` attribute

        class _Anthropic:
            def __init__(self, *a, **k):
                self.models = _Models()

        bare.Anthropic = _Anthropic
        monkeypatch.setitem(sys.modules, "anthropic", bare)

        assert cost.refresh_baseline_pricing_from_api() is False
        assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == (1.0, 2.0)

        failopen.reset_cache()
        assert dict(failopen.snapshot().by_code) == {}, (
            "a missing pricing attribute is an ordinary refusal; recording a fail-open "
            "here would report a degradation that did not happen"
        )
        failopen.reset_cache()


class TestTheFixtureItselfIsHonest:
    def test_globals_are_restored_after_a_successful_refresh(self, monkeypatch):
        """Guards this FILE, not the product.

        A test that overwrites the baseline prices and leaves them overwritten is the
        pollution class this campaign spent a day removing. Asserting the restoration
        beats assuming monkeypatch handled the in-place dict mutation — it does not,
        without `setitem`.
        """
        before = (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M,
                  dict(cost._OPUS_PRICING))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(cost, "_HOST_INPUT_PER_M", 1.0)
            mp.setattr(cost, "_HOST_OUTPUT_PER_M", 2.0)
            mp.setitem(cost._OPUS_PRICING, cost.LATEST_OPUS_MODEL, (1.0, 2.0))
            mp.setitem(sys.modules, "anthropic",
                       _fake_anthropic({"input_per_mtok": 99.0, "output_per_mtok": 199.0}))
            cost.refresh_baseline_pricing_from_api()
            assert cost._HOST_INPUT_PER_M == 99.0  # it really did change

        assert (cost._HOST_INPUT_PER_M, cost._HOST_OUTPUT_PER_M) == before[:2]
        assert dict(cost._OPUS_PRICING) == before[2]
