"""CHZ-AUD-C-03: the length gate must not silently reject legitimately short
valid answers (which caused a silent post-dispatch re-route), and a length-gate
fallback must be observable for ALL complexities, not only premium.
"""

import pytest

from llm_router.contract import build_contract, _TERSE_ANSWER_TASKS
from llm_router.gates import run_gates, _is_valid_short_answer
from llm_router.contract import GateType
from llm_router.types import Complexity
from llm_router.router import TaskType


@pytest.fixture(autouse=True)
def _force_gates_on(monkeypatch):
    # run_gates auto-skips under pytest unless LLM_ROUTER_GATES=on — force it so these
    # tests actually exercise the length gate rather than the pytest skip.
    monkeypatch.setenv("LLM_ROUTER_GATES", "on")


def _run(task, complexity, text):
    c = build_contract("cid", task, complexity, "ollama/x")
    return run_gates(c, text)


def test_query_terse_answer_passes_at_moderate():
    # "Yes" (3 chars) at MODERATE would fail the old 20-char floor -> silent re-route.
    passed, results = _run(TaskType.QUERY, Complexity.MODERATE, "Yes")
    assert passed, [r.reason for r in results if not r.passed]
    assert TaskType.QUERY in _TERSE_ANSWER_TASKS


def test_yes_no_boolean_number_allowlisted_for_analyze():
    for ans in ["Yes.", "No", "True", "42", "$5", "100%", "3.14", "N/A"]:
        passed, results = _run(TaskType.ANALYZE, Complexity.MODERATE, ans)
        assert passed, f"{ans!r} should be allow-listed, got {[r.reason for r in results if not r.passed]}"


def test_genuinely_short_garbage_still_rejected():
    # A 4-char non-answer for ANALYZE at MODERATE must still trip the length gate.
    passed, results = _run(TaskType.ANALYZE, Complexity.MODERATE, "blah")
    assert not passed
    length_fail = [r for r in results if r.gate == GateType.LENGTH and not r.passed]
    assert length_fail, "length gate should have failed"
    assert "too short" in length_fail[0].reason


def test_single_word_not_waved_through_for_code():
    # A one-word "code" answer is almost certainly wrong — must still be rejected.
    assert _is_valid_short_answer("plausibleword") is False


def test_length_fallback_names_the_gate_in_result_reason():
    """C-03 observability: which gate triggered the fallback must be recoverable.
    The failing GateResult carries gate=LENGTH and a 'too short' reason — this is
    what the router forwards into its `gate_verification_failed` structured log
    and the route-quality ledger's fallback_reason, for ALL complexities."""
    passed, results = _run(TaskType.ANALYZE, Complexity.MODERATE, "blah")  # non-premium
    assert not passed
    length_fail = [r for r in results if r.gate == GateType.LENGTH and not r.passed]
    assert length_fail and "too short" in length_fail[0].reason


def test_empty_is_not_a_valid_short_answer():
    assert _is_valid_short_answer("") is False
    assert _is_valid_short_answer("   ") is False
