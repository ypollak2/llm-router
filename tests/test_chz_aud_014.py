"""Regression test for CHZ-AUD-014.

Complex/premium tasks silently emergency-fallback to Ollama when the length
gate trips. The gate now emits a visible WARNING log when a premium-complexity
task fails the length gate, so the downshift is observable rather than silent.

The existing zero_claude behavior (CHZ-AUD-005) is unaffected — this only adds
a log line on an already-failing gate; it does not change pass/fail outcomes.
"""

from __future__ import annotations

import logging

import pytest

from llm_router.contract import GateType, build_contract
from llm_router.gates import _check_length, run_gates
from llm_router.types import Complexity, TaskType


@pytest.fixture(autouse=True)
def _enable_gates(monkeypatch):
    """Force gates to run even under pytest (mirrors real premium routing)."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("LLM_ROUTER_GATES", "on")


def test_complex_length_gate_failure_logs_visible_warning(caplog):
    """A short response on a COMPLEX task must emit an observable warning."""
    contract = build_contract("c1", TaskType.CODE, Complexity.COMPLEX, "openai/o3")
    # 29 chars < 50 min for complex — reproduces the audited log line.
    short = "x = 1  # brief valid answer.."
    assert len(short.strip()) < contract.constraints.min_output_length

    with caplog.at_level(logging.WARNING, logger="llm_router.gates"):
        result = _check_length(contract, short)

    assert not result.passed
    assert any(
        rec.levelno == logging.WARNING and "premium task" in rec.getMessage()
        for rec in caplog.records
    ), f"expected a visible premium-downshift warning, got: {caplog.records}"


def test_deep_reasoning_length_gate_failure_logs_warning(caplog):
    contract = build_contract("c2", TaskType.ANALYZE, Complexity.DEEP_REASONING, "openai/o3")
    with caplog.at_level(logging.WARNING, logger="llm_router.gates"):
        result = _check_length(contract, "too short")
    assert not result.passed
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_simple_length_gate_failure_stays_quiet(caplog):
    """Non-premium tasks must NOT emit the premium warning (avoid log noise).

    Uses GENERATE, not QUERY: CHZ-AUD-C-03 makes QUERY a terse-answer task
    (min length capped to the empty-guard), so a 1-char QUERY answer now passes
    the length gate. GENERATE keeps the complexity floor, so "x" still fails —
    exercising the non-premium quiet path this test guards.
    """
    contract = build_contract("c3", TaskType.GENERATE, Complexity.MODERATE, "ollama/gemma4")
    with caplog.at_level(logging.WARNING, logger="llm_router.gates"):
        result = _check_length(contract, "x")  # 1 < 20 for moderate
    assert not result.passed
    assert not any(
        "premium task" in rec.getMessage() for rec in caplog.records
    )


def test_passing_length_gate_logs_nothing(caplog):
    contract = build_contract("c4", TaskType.CODE, Complexity.COMPLEX, "openai/o3")
    long_enough = "y" * 100
    with caplog.at_level(logging.WARNING, logger="llm_router.gates"):
        result = _check_length(contract, long_enough)
    assert result.passed
    assert not any("premium task" in rec.getMessage() for rec in caplog.records)


def test_run_gates_complex_short_still_fails_and_warns(caplog):
    """End-to-end via run_gates: outcome unchanged (still fails), now observable."""
    contract = build_contract("c5", TaskType.CODE, Complexity.COMPLEX, "openai/o3")
    with caplog.at_level(logging.WARNING, logger="llm_router.gates"):
        passed, results = run_gates(contract, "x = 1")
    assert not passed
    assert GateType.LENGTH in {r.gate for r in results}
    assert any("premium task" in rec.getMessage() for rec in caplog.records)
