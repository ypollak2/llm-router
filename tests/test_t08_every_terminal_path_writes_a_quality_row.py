"""Every path that returns content must leave a quality-ledger row — T-08/T-10.

`_finalize_successful_route(served_from_cache=True)` was doing two jobs: "do not
re-bill this turn" and "do not write a quality row". Three terminal paths used
it, so three ways of returning real content to a caller left no trace in
`routing_quality.jsonl` at all. Reproduced during the audit: a floor-served
route returned 432 characters of content and the ledger file was never created.

The exhaustion floor is the one that matters. It is the case where EVERY
candidate was rejected by a dispatch gate and the best rejected answer was
served anyway — precisely what `mis_route` and `quality_escalation_occurred`
exist to measure, and the only case the ledger could not see.

T-10 is the same defect seen from the caller's side: that answer came back with
no degradation marker, rendered with the same success tick as a clean one, and
had `success` recomputed by `_response_is_usable()` on the very content the
router had rejected — feeding the bandit a win for producing rejectable output.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from llm_router import routing_quality as RQ
from llm_router.types import LLMResponse


# ── the enum ─────────────────────────────────────────────────────────────────

def test_the_outcome_enum_can_express_a_degraded_turn():
    """A field that cannot hold the value is not a measurement.

    `route_succeeded` was a boolean with exactly one reachable value for the
    ledger's whole history (0 of 16,869 rows were False). Adding rows for these
    paths is pointless if they all have to claim `success`.
    """
    assert "degraded" in RQ.ROUTE_OUTCOMES
    assert "deduplicated" in RQ.ROUTE_OUTCOMES
    assert RQ.DEGRADED_OUTCOMES == frozenset({"degraded"})
    assert "cache_hit" in RQ.REPLAYED_OUTCOMES and "deduplicated" in RQ.REPLAYED_OUTCOMES
    assert "success" not in RQ.DEGRADED_OUTCOMES | RQ.REPLAYED_OUTCOMES


def test_a_degraded_row_is_not_a_success_row(tmp_path):
    """Round-trip: what is written is what a reader gets back."""
    ledger = tmp_path / "routing_quality.jsonl"
    rec = RQ.RouteLedgerRecord(
        route_id="r1", route_kind="completion", task_type="query",
        chosen_model="m", final_model="m",
        route_outcome="degraded", route_succeeded=False,
    )
    assert RQ.record_route(rec, path=str(ledger)) is True
    row = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert row["route_outcome"] == "degraded"
    assert row["route_succeeded"] is False


# ── T-10: the response carries its own degradation ───────────────────────────

def test_a_clean_response_is_not_marked_degraded():
    """Anti-vacuity: the flag must default off, or every test below is trivial."""
    r = LLMResponse(content="an answer", model="m", provider="p",
        input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0)
    assert r.quality_degraded is False
    assert "DEGRADED" not in r.summary()


def test_a_degraded_response_is_distinguishable_by_field():
    r = LLMResponse(content="an answer", model="m", provider="p",
        input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0,
                    quality_degraded=True,
                    quality_degraded_reason="every candidate was rejected")
    assert r.quality_degraded is True
    assert r.quality_degraded_reason


def test_a_degraded_response_is_distinguishable_in_the_rendered_output():
    """The user must be able to see it, not only a caller branching on a field.

    It rendered with the same success tick as a clean answer, which is how a
    rejected answer reached a user looking exactly like an endorsed one.
    """
    clean = LLMResponse(content="x", model="m", provider="p",
        input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0)
    degraded = dataclasses.replace(
        clean, quality_degraded=True, quality_degraded_reason="gate-rejected")
    assert "DEGRADED" in degraded.summary()
    assert degraded.summary() != clean.summary()
    assert "gate-rejected" in degraded.summary()


# ── T-10: the bandit must not be told it won ─────────────────────────────────

def test_the_bandit_is_not_fed_success_for_a_degraded_answer():
    """The rule at the router's bandit feed, exercised directly.

    Re-deriving "usable" from rejected text and calling it a win teaches the
    bandit to prefer whichever model produces rejectable output most cheaply.
    """
    from llm_router.router import _response_is_usable

    text = "Here is a confident, well-formed, and wrong answer about the topic."
    assert _response_is_usable(text) is True, (
        "fixture text must pass the usability heuristic, or this proves nothing"
    )

    degraded = LLMResponse(content=text, model="m", provider="p",
        input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0, quality_degraded=True)
    clean = LLMResponse(content=text, model="m", provider="p",
        input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0)

    def bandit_success(response) -> bool:
        # The expression from router.py's log_routing_decision call.
        return (
            False if getattr(response, "quality_degraded", False)
            else _response_is_usable(getattr(response, "content", "") or "")
        )

    assert bandit_success(clean) is True
    assert bandit_success(degraded) is False, (
        "a gate-rejected answer scored as a bandit win"
    )


def test_the_router_actually_uses_that_expression():
    """Rule B: assert the call site, not a re-implementation.

    The test above builds the expression itself, which would pass even if
    router.py never adopted it. This pins the source.
    """
    import inspect
    from llm_router import router

    src = inspect.getsource(router)
    assert 'False if getattr(response, "quality_degraded", False)' in src, (
        "router.py's bandit feed no longer short-circuits on quality_degraded"
    )


def test_the_savings_logger_applies_the_same_rule():
    """The second place a response becomes a success signal.

    One of the two being fixed is how the scrubbers drifted four classes apart.
    """
    import inspect
    import sys

    before = set(vars(sys.modules["llm_router"]))
    try:
        from llm_router.hooks import savings_logger
        src = inspect.getsource(savings_logger)
    finally:
        # Importing a hooks submodule binds `hooks` on the package as a fileless
        # namespace module, which then answers for the real one in every test
        # that follows (T-01).
        pkg = sys.modules["llm_router"]
        for attr in set(vars(pkg)) - before:
            if getattr(getattr(pkg, attr, None), "__file__", "s") is None:
                delattr(pkg, attr)
    assert 'getattr(response, "quality_degraded", False)' in src


# ── T-08: the call sites pass a distinguishing outcome ───────────────────────

@pytest.mark.parametrize("outcome, marker", [
    ("degraded", "the exhaustion floor is a FOURTH success"),
    ("deduplicated", "idempotency dedupe is also a success path"),
    ("cache_hit", "semantic-cache hit is a bypassed terminal state"),
])
def test_each_terminal_path_names_its_own_outcome(outcome, marker):
    """All three previously wrote nothing. Each must now name what it is.

    Asserted against the source because these paths need a live provider, a
    populated semantic cache, or an exhausted candidate chain to reach — and a
    test that mocks all three proves less about the call site than reading it.
    """
    import inspect
    from llm_router import router

    lines = inspect.getsource(router).split("\n")
    idx = next(i for i, ln in enumerate(lines) if marker in ln)
    window = "\n".join(lines[idx:idx + 40])
    assert f'ledger_outcome="{outcome}"' in window, (
        f"the path at {marker!r} does not pass ledger_outcome={outcome!r}; "
        "it will write no quality row at all"
    )


def test_the_ledger_gate_admits_a_named_outcome():
    """The gate that skipped all three.

    `served_from_cache` must no longer be sufficient on its own to suppress the
    row — otherwise the three `ledger_outcome=` arguments above are inert.
    """
    import inspect
    from llm_router import router

    src = inspect.getsource(router._finalize_successful_route)
    assert "not served_from_cache or ledger_outcome" in src, (
        "the quality-ledger gate still suppresses every served_from_cache turn"
    )


def test_a_degraded_outcome_forces_route_succeeded_false():
    """The two fields must not be able to disagree."""
    import inspect
    from llm_router import router

    src = inspect.getsource(router._finalize_successful_route)
    assert "route_succeeded=(ledger_outcome not in _DEGRADED_LEDGER_OUTCOMES)" in src
    assert router._DEGRADED_LEDGER_OUTCOMES == RQ.DEGRADED_OUTCOMES, (
        "router's copy of the degraded-outcome set has drifted from the canonical one"
    )
