"""G-F Group B — `router._emit_ledger_attempt`, the row it writes to the ledger.

22 mutants survived. Almost all mutate a single field of the `LedgerEvent` being
constructed — the model, the provider, the token counts, the route id — and nothing
asserted what actually lands in the row.

WHY EACH FIELD MATTERS
----------------------
This function records EVERY billable attempt, including gate-rejected and
quality-rejected ones, because `cost.log_usage` is called only for the winning attempt
and structurally omits the rest. The aggregation layer derives route and session totals
from these rows, so a wrong field here is a wrong number on the dashboard — not a crash.

    model      7 mutants   attribution: which model was billed
    provider   3           the quota-saved derivation keys off this
    route_id   1           the join key the adoption row is matched on (Phase 0.5)
    tokens     2           the quota figure itself

`route_id = ledger_route_id or correlation_id or ""` is the subtlest: when the hook
minted a route_directive_id, the billable row MUST use it so the ledger join with the
adoption row fires. A mutant reversing that precedence silently breaks realized-savings
attribution — the rows exist, they simply never match.

The whole function is FAIL-OPEN by design ("ledger emission must never break routing"),
so every mutation here is invisible to the caller. Only the emitted row can catch it.
"""

from __future__ import annotations

import pytest

from llm_router import router
from llm_router.types import RoutingProfile, TaskType


class _Response:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture()
def captured(monkeypatch):
    """Capture the LedgerEvent instead of writing it, and keep host_mode deterministic."""
    events = []

    import llm_router.execution_ledger as el

    monkeypatch.setattr(el, "record_event", lambda ev, **kw: events.append(ev) or True)
    monkeypatch.setattr(router.cost, "_host_is_metered", lambda: True)
    return events


def _emit(captured, response, **kw):
    router._emit_ledger_attempt(
        response, "fallback/model", TaskType.CODE, RoutingProfile.BALANCED,
        event_type=kw.pop("event_type", "attempt_completed"),
        correlation_id=kw.pop("correlation_id", "corr-1"),
        **kw,
    )
    assert captured, "no ledger event was emitted"
    return captured[-1]


class TestTheModelField:
    """`model=getattr(response, "model", "") or model` — 7 mutants, the largest cluster.

    The response's model wins; the passed-in `model` is the fallback. Getting this
    backwards attributes the row to the model that was ASKED for rather than the one
    that answered — which is exactly the confusion the routing dashboard exists to
    resolve.
    """

    def test_the_responses_model_wins_over_the_argument(self, captured):
        ev = _emit(captured, _Response(model="openai/gpt-4o"))
        assert ev.model == "openai/gpt-4o"

    def test_the_argument_is_used_when_the_response_has_no_model(self, captured):
        ev = _emit(captured, _Response())
        assert ev.model == "fallback/model"

    def test_an_empty_response_model_falls_back_rather_than_recording_blank(self, captured):
        """The `or` spelling, not a `.get` default: an EMPTY model must fall back too,
        or the row is attributed to nothing at all."""
        ev = _emit(captured, _Response(model=""))
        assert ev.model == "fallback/model"


class TestTheProviderField:
    """`provider=getattr(response, "provider", "") or ""`.

    The quota-tokens-saved derivation keys off provider, and an UNKNOWN provider
    deliberately counts as zero saved rather than assuming a non-Claude win.
    """

    def test_the_responses_provider_is_recorded(self, captured):
        assert _emit(captured, _Response(provider="ollama")).provider == "ollama"

    def test_a_missing_provider_becomes_empty_string_not_None(self, captured):
        """Empty string is the sentinel the aggregation layer checks for. `None` would
        take a different path and could be read as a valid provider name."""
        ev = _emit(captured, _Response())
        assert ev.provider == ""

    def test_provider_and_model_are_read_from_DIFFERENT_attributes(self, captured):
        """A mutant reading `model` where it should read `provider` is invisible unless
        the two carry different values."""
        ev = _emit(captured, _Response(provider="ollama", model="hermes3:8b"))
        assert (ev.provider, ev.model) == ("ollama", "hermes3:8b")


class TestTheRouteIdPrecedence:
    """`route_id = ledger_route_id or correlation_id or ""` — the Phase 0.5 join key.

    When the hook minted a route_directive_id, the billable row must use it so the
    ledger join with the adoption row fires. Reversing the precedence leaves both rows
    present and never matching — realized savings silently collapse to zero.
    """

    def test_the_explicit_ledger_route_id_wins(self, captured):
        ev = _emit(captured, _Response(), ledger_route_id="route-directive-9",
                   correlation_id="corr-1")
        assert ev.route_id == "route-directive-9"

    def test_correlation_id_is_the_fallback(self, captured):
        ev = _emit(captured, _Response(), correlation_id="corr-7")
        assert ev.route_id == "corr-7"

    def test_neither_yields_an_empty_string_not_None(self, captured):
        ev = _emit(captured, _Response(), correlation_id=None)
        assert ev.route_id == ""


class TestTokensAndCost:
    def test_token_counts_are_carried_through_distinctly(self, captured):
        ev = _emit(captured, _Response(input_tokens=1000, output_tokens=250))
        assert (ev.input_tokens, ev.output_tokens) == (1000, 250), (
            "different values on purpose — equal ones hide a swap"
        )

    def test_absent_token_counts_stay_None_rather_than_becoming_zero(self, captured):
        """None means 'not reported'; 0 means 'reported as none used'. The aggregation
        layer treats them differently and a fabricated zero is the RED2-02 shape."""
        ev = _emit(captured, _Response())
        assert ev.input_tokens is None and ev.output_tokens is None

    def test_cost_is_coerced_to_float_and_defaults_to_zero(self, captured):
        ev = _emit(captured, _Response(cost_usd="0.25"))
        assert ev.measured_cost_usd == 0.25
        assert isinstance(ev.measured_cost_usd, float)

    def test_a_None_cost_becomes_zero_not_None(self, captured):
        """`float(... or 0.0)` — the ledger column is numeric; None would break the
        aggregation rather than contribute nothing."""
        assert _emit(captured, _Response(cost_usd=None)).measured_cost_usd == 0.0


class TestTaskProfileAndRejection:
    def test_task_type_and_profile_are_recorded_as_their_values(self, captured):
        ev = _emit(captured, _Response())
        assert ev.task_type == TaskType.CODE.value
        assert ev.routing_profile == RoutingProfile.BALANCED.value

    def test_rejection_fields_are_carried_verbatim(self, captured):
        ev = _emit(captured, _Response(), accepted=False, rejected=True,
                   rejection_reason="quality_gate")
        assert (ev.accepted, ev.rejected, ev.rejection_reason) == (
            False, True, "quality_gate"
        )

    def test_an_accepted_attempt_carries_the_phase0_savings_fields(self, captured):
        """Only the ACCEPTED attempt should carry these, so the baseline is never
        credited twice (R6). The emitter must pass them through unchanged."""
        ev = _emit(captured, _Response(), accepted=True, classifier_cost_usd=0.002,
                   failed_attempt_cost_usd=0.01,
                   baseline_equivalent_cost_usd=0.05, baseline_tokens=1500)
        assert ev.classifier_cost_usd == 0.002
        assert ev.failed_attempt_cost_usd == 0.01
        assert ev.baseline_equivalent_cost_usd == 0.05
        assert ev.baseline_tokens == 1500

    def test_the_event_type_is_recorded_as_given(self, captured):
        assert _emit(captured, _Response(),
                     event_type="attempt_rejected").event_type == "attempt_rejected"


class TestFailOpenRecordsItsOwnCode:
    """`failopen.record("CHZ-FO-ROUTER-LEDGER-EMIT", exc)` — 2 mutants.

    WP-06 hardened the ledger against losing events under concurrency; this path loses
    them BEFORE they arrive. The record is what gives that loss a number instead of it
    being inferred later from a reconciliation gap.
    """

    def test_a_ledger_failure_is_recorded_and_never_raises(self, monkeypatch, tmp_path):
        import json as _json

        import llm_router.execution_ledger as el
        from llm_router import failopen
        from llm_router.paths import is_isolated

        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
        assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
        failopen.reset_cache()
        failopen.clear()

        monkeypatch.setattr(router.cost, "_host_is_metered", lambda: True)
        monkeypatch.setattr(
            el, "record_event",
            lambda ev, **kw: (_ for _ in ()).throw(RuntimeError("ledger down")),
        )

        # Must NOT raise: "ledger emission must never break routing".
        router._emit_ledger_attempt(
            _Response(), "m", TaskType.CODE, RoutingProfile.BALANCED,
            event_type="attempt_completed", correlation_id="c",
        )

        failopen.reset_cache()
        assert dict(failopen.snapshot().by_code) == {"CHZ-FO-ROUTER-LEDGER-EMIT": 1}

        recorded = [
            _json.loads(ln) for ln in failopen.store_path().read_text().splitlines()
            if ln.strip()
        ]
        assert [r.get("e") for r in recorded] == ["RuntimeError"]
        failopen.reset_cache()
