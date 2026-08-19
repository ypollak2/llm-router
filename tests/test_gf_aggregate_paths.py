"""G-F Group B — `execution_ledger._aggregate`, the paths C6 did not reach.

C6 (`test_gf_c6_aggregate_multirow.py`) closed the accumulator class by giving the
function more than one row. Forty mutants survived it, clustered on four paths that
multi-row coverage alone does not touch:

    5   route_realization.setdefault(rid, "unknown")     the realization_unknown EVENT
    3   elif et == "realization_unknown":                 (as distinct from the STATUS field)
    8   hook token handling                              hook_overhead_usd, metered-only
    3   route_actual_tokens.get(rid, 0)                  quota derivation
    3   baseline_tokens                                  back-compat accounting
    4   host_mode / final_provider defaults              "unknown" and "" fallbacks
    2   round(..., 6)                                    money rounding

Each is a distinct behaviour of the same function, and each is money-facing: hook
overhead is a real dollar figure, quota tokens feed the "Claude tokens not consumed"
metric, and the realization fallback decides whether a saving may be called realized.
"""

from __future__ import annotations

import uuid

import pytest

from llm_router.execution_ledger import (
    LedgerEvent,
    get_route_accounting,
    record_event,
)


def _attempt(route_id: str, *, turn_id: str = "", measured: float = 0.01,
             baseline: float = 0.05, host_mode: str = "unknown",
             hook_in: int | None = None, hook_out: int | None = None,
             input_tokens: int | None = None, output_tokens: int | None = None,
             baseline_tokens: int | None = None, provider: str = "",
             terminal_state: str | None = None) -> LedgerEvent:
    return LedgerEvent(
        session_id="s-agg2", turn_id=turn_id, route_id=route_id,
        attempt_id=str(uuid.uuid4()), event_type="attempt_completed",
        measured_cost_usd=measured, baseline_equivalent_cost_usd=baseline,
        host_mode=host_mode, hook_input_tokens=hook_in, hook_output_tokens=hook_out,
        input_tokens=input_tokens, output_tokens=output_tokens,
        baseline_tokens=baseline_tokens, provider=provider,
        terminal_state=terminal_state,
    )


class TestRealizationUnknownEventType:
    """`elif et == "realization_unknown"` — the FALLBACK signal, not the status field.

    The comment is explicit: "the explicit status field wins; the `realization_unknown`
    event type is a fallback signal for unknown." C6 exercised the status field only, so
    every mutant on the fallback branch survived.

    This matters for Gate 18: a route marked unknown "contributes to potential but NEVER
    to realized", so a mutant that loses the unknown marking can promote an unverified
    saving into the realized figure.
    """

    @pytest.fixture()
    def db(self, tmp_path, monkeypatch):
        p = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(p))
        return p

    def test_a_realization_unknown_event_marks_the_route_unknown(self, db):
        record_event(_attempt("r-u"), path=db)
        record_event(LedgerEvent(session_id="s-agg2", route_id="r-u",
                                 event_type="realization_unknown"), path=db)
        acc = get_route_accounting("r-u", path=db)
        assert acc.realization_unknown_routes == 1
        assert acc.realized_routes == 0

    def test_an_unknown_route_contributes_potential_but_never_realized(self, db):
        """Gate 18's core rule, on the fallback path."""
        record_event(_attempt("r-u2", measured=0.01, baseline=0.05), path=db)
        record_event(LedgerEvent(session_id="s-agg2", route_id="r-u2",
                                 event_type="realization_unknown"), path=db)
        acc = get_route_accounting("r-u2", path=db)
        assert acc.potential_savings_usd == pytest.approx(0.04)
        assert acc.realized_savings_usd == 0.0

    def test_an_explicit_status_WINS_over_a_later_unknown_event(self, db):
        """`setdefault` is the whole point: an explicit status must not be overwritten.

        Mutating setdefault to a plain assignment would let a trailing
        `realization_unknown` event downgrade a verified route — silently converting a
        real saving into an unverified one.
        """
        record_event(_attempt("r-u3"), path=db)
        record_event(LedgerEvent(session_id="s-agg2", route_id="r-u3",
                                 event_type="route_realized",
                                 realization_status="verified_used",
                                 adoption_method="door_call", used_by_host=True,
                                 accepted=True), path=db)
        record_event(LedgerEvent(session_id="s-agg2", route_id="r-u3",
                                 event_type="realization_unknown"), path=db)
        acc = get_route_accounting("r-u3", path=db)
        assert acc.realized_routes == 1
        assert acc.realization_unknown_routes == 0

    def test_the_unknown_default_is_the_string_unknown(self, db):
        """`setdefault(rid, "unknown")` — the literal drives which counter increments.

        A mutated marker string falls through every `elif` and increments nothing, so
        the route silently vanishes from all three realization counters.
        """
        record_event(_attempt("r-a"), path=db)
        record_event(LedgerEvent(session_id="s-agg2", route_id="r-a",
                                 event_type="realization_unknown"), path=db)
        acc = get_route_accounting("r-a", path=db)
        assert acc.realization_unknown_routes == 1, (
            "a route with an unknown-realization event must be counted somewhere"
        )
        assert (acc.realized_routes, acc.overridden_routes) == (0, 0), (
            "a mutated marker string would fall through every elif and increment nothing"
        )


class TestHookOverheadIsMeteredOnly:
    """`if (hook_in or hook_out) and hm == "metered"` — the marginal-$0 rule.

    Only a CONFIRMED metered row prices hook tokens. Subscription and unknown host
    modes must never fabricate a cost, because a fabricated dollar figure is the same
    class of lie as a fabricated zero.
    """

    @pytest.fixture()
    def db(self, tmp_path, monkeypatch):
        p = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(p))
        return p

    def test_metered_rows_price_hook_tokens(self, db):
        record_event(_attempt("r-m", host_mode="metered", hook_in=1_000_000,
                              hook_out=1_000_000), path=db)
        acc = get_route_accounting("r-m", path=db)
        assert acc.hook_overhead_usd > 0.0
        assert acc.hook_input_tokens == 1_000_000
        assert acc.hook_output_tokens == 1_000_000

    def test_subscription_rows_never_price_hook_tokens(self, db):
        record_event(_attempt("r-s", host_mode="subscription", hook_in=1_000_000,
                              hook_out=1_000_000), path=db)
        acc = get_route_accounting("r-s", path=db)
        assert acc.hook_overhead_usd == 0.0
        # The TOKENS are still counted — only the pricing is suppressed.
        assert acc.hook_input_tokens == 1_000_000

    def test_unknown_host_mode_never_prices_hook_tokens(self, db):
        record_event(_attempt("r-x", host_mode="unknown", hook_in=1_000_000,
                              hook_out=1_000_000), path=db)
        assert get_route_accounting("r-x", path=db).hook_overhead_usd == 0.0

    def test_a_metered_row_with_no_hook_tokens_costs_nothing(self, db):
        record_event(_attempt("r-n", host_mode="metered"), path=db)
        assert get_route_accounting("r-n", path=db).hook_overhead_usd == 0.0

    def test_input_and_output_hook_tokens_are_priced_at_DIFFERENT_rates(self, db):
        """B1 in this audit was Opus input/output rates inverted.

        Output costs more than input, so an all-output row must price higher than an
        all-input row of the same size. Equal fixtures make a swap invisible.
        """
        record_event(_attempt("r-in", host_mode="metered", hook_in=1_000_000), path=db)
        record_event(_attempt("r-out", host_mode="metered", hook_out=1_000_000), path=db)
        in_only = get_route_accounting("r-in", path=db).hook_overhead_usd
        out_only = get_route_accounting("r-out", path=db).hook_overhead_usd
        assert out_only > in_only > 0.0

    def test_hook_overhead_equals_the_EXACT_computed_dollar_figure(self, db):
        """The arithmetic itself, not its shape.

        Every other test in this class asserts `> 0.0` or a relative ordering. Those
        kill the LOGICAL mutants — the metered-only guard, an input/output swap — and
        leave every arithmetic constant alive: `/ 1_000_000` could become `/ 100_000`
        and all of them still pass.

        The expected value is computed from the rates the ledger itself publishes, so
        this is not a hardcoded number that goes stale when pricing changes; it is the
        same inputs run through the formula the caller is entitled to expect.
        """
        from llm_router.execution_ledger import _host_opus_rates

        in_pm, out_pm = _host_opus_rates()
        hook_in, hook_out = 3_000_000, 7_000_000
        expected = (hook_in * in_pm + hook_out * out_pm) / 1_000_000

        record_event(_attempt("r-exact", host_mode="metered",
                              hook_in=hook_in, hook_out=hook_out), path=db)
        acc = get_route_accounting("r-exact", path=db)
        assert acc.hook_overhead_usd == pytest.approx(round(expected, 6))

    def test_hook_overhead_accumulates_across_metered_rows(self, db):
        for _ in range(3):
            record_event(_attempt("r-acc", host_mode="metered", hook_out=1_000_000),
                         path=db)
        record_event(_attempt("r-one", host_mode="metered", hook_out=1_000_000), path=db)
        one = get_route_accounting("r-one", path=db).hook_overhead_usd
        assert get_route_accounting("r-acc", path=db).hook_overhead_usd == pytest.approx(
            one * 3
        )


class TestMoneyIsRoundedToSixPlaces:
    """`round(..., 6)` on every money field.

    Not cosmetic: unrounded float accumulation produces values like
    0.30000000000000004, which render as a wrong number and break equality checks
    downstream. The rounding is part of the contract.
    """

    @pytest.fixture()
    def db(self, tmp_path, monkeypatch):
        p = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(p))
        return p

    def test_accumulated_costs_are_rounded_not_drifting(self, db):
        # 0.1 + 0.2 is the canonical float-drift case.
        for m in (0.1, 0.2):
            record_event(_attempt("r-r", measured=m, baseline=0.0), path=db)
        acc = get_route_accounting("r-r", path=db)
        assert acc.actual_cost_usd == 0.3, (
            f"expected exactly 0.3 after rounding, got {acc.actual_cost_usd!r}"
        )

    def test_rounding_keeps_six_decimal_places_not_fewer(self, db):
        """A mutant rounding to 0 or 1 places would destroy sub-cent costs entirely.

        Six places is what makes a $0.000015 call visible instead of $0.00.
        """
        record_event(_attempt("r-tiny", measured=0.000015, baseline=0.0), path=db)
        assert get_route_accounting("r-tiny", path=db).actual_cost_usd == 0.000015


class TestQuotaAndProviderDefaults:
    """`route_final_provider.get(rid, "")` and `route_actual_tokens.get(rid, 0)`.

    The empty-string provider default is load-bearing: an UNKNOWN provider "does NOT
    count as non-Claude; it counts as 0 quota saved rather than assuming a saving we
    can't actually verify."
    """

    @pytest.fixture()
    def db(self, tmp_path, monkeypatch):
        p = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(p))
        return p

    def _realize(self, rid: str, db) -> None:
        record_event(LedgerEvent(session_id="s-agg2", route_id=rid,
                                 event_type="route_realized",
                                 realization_status="verified_used",
                                 adoption_method="door_call", used_by_host=True,
                                 accepted=True), path=db)

    def test_a_route_with_no_provider_saves_no_quota(self, db):
        """The pre-migration case the comment names: never fabricate a saving."""
        record_event(_attempt("r-np", input_tokens=1000, output_tokens=500,
                              provider=""), path=db)
        self._realize("r-np", db)
        assert get_route_accounting("r-np", path=db).realized_quota_tokens_saved == 0

    def test_a_non_claude_provider_saves_its_tokens(self, db):
        record_event(_attempt("r-ol", input_tokens=1000, output_tokens=500,
                              provider="ollama"), path=db)
        self._realize("r-ol", db)
        assert get_route_accounting("r-ol", path=db).realized_quota_tokens_saved == 1500

    def test_a_claude_final_provider_saves_no_quota(self, db):
        """If the route's final model IS Claude, Claude ran — zero quota saved."""
        record_event(_attempt("r-cl", input_tokens=1000, output_tokens=500,
                              provider="anthropic"), path=db)
        self._realize("r-cl", db)
        assert get_route_accounting("r-cl", path=db).realized_quota_tokens_saved == 0

    def test_an_unrealized_route_saves_no_quota_however_cheap(self, db):
        record_event(_attempt("r-unreal", input_tokens=1000, output_tokens=500,
                              provider="ollama"), path=db)
        assert get_route_accounting("r-unreal", path=db).realized_quota_tokens_saved == 0
