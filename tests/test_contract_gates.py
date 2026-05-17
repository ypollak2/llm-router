"""Tests for v8.8.0 Contract-as-Infrastructure: contracts, gates, receipts."""

from __future__ import annotations

import pytest

from llm_router.contract import (
    GateType,
    build_contract,
)
from llm_router.gates import (
    GateResult,
    _check_citation,
    _check_length,
    _check_structure,
    _check_syntax,
    run_gates,
)
from llm_router.receipt_store import compute_receipt
from llm_router.types import Complexity, TaskType


class TestContractGeneration:
    """Test automatic contract generation from task metadata."""

    def test_code_task_gets_syntax_and_length_gates(self):
        contract = build_contract("c1", TaskType.CODE, Complexity.MODERATE, "openai/gpt-4o")
        assert GateType.SYNTAX in contract.gates
        assert GateType.LENGTH in contract.gates

    def test_query_task_gets_length_gate_only(self):
        contract = build_contract("c2", TaskType.QUERY, Complexity.SIMPLE, "ollama/gemma4")
        assert contract.gates == [GateType.LENGTH]

    def test_research_task_gets_citation_gate(self):
        contract = build_contract("c3", TaskType.RESEARCH, Complexity.MODERATE, "perplexity/online")
        assert GateType.CITATION in contract.gates

    def test_media_tasks_have_no_gates(self):
        for tt in [TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO]:
            contract = build_contract("c4", tt, Complexity.SIMPLE, "fal/flux")
            assert contract.gates == []

    def test_simple_complexity_has_lowest_min_length(self):
        contract = build_contract("c5", TaskType.QUERY, Complexity.SIMPLE, "ollama/gemma4")
        assert contract.constraints.min_output_length == 1

    def test_complex_has_higher_min_length(self):
        contract = build_contract("c6", TaskType.ANALYZE, Complexity.COMPLEX, "openai/o3")
        assert contract.constraints.min_output_length == 50

    def test_contract_serialization(self):
        contract = build_contract("c7", TaskType.CODE, Complexity.MODERATE, "codex/gpt-5.4")
        d = contract.to_dict()
        assert d["contract_id"] == "c7"
        assert d["task_type"] == "code"
        assert d["complexity"] == "moderate"
        assert "syntax" in d["gates"]


class TestGates:
    """Test individual verification gates."""

    def _make_contract(self, task_type=TaskType.CODE, complexity=Complexity.MODERATE):
        return build_contract("test", task_type, complexity, "test/model")

    def test_syntax_gate_passes_valid_python(self):
        contract = self._make_contract()
        code = "```python\ndef hello():\n    return 'world'\n```"
        result = _check_syntax(contract, code)
        assert result.passed

    def test_syntax_gate_fails_invalid_python(self):
        contract = self._make_contract()
        code = "```python\ndef hello(\n    return 'world'\n```"
        result = _check_syntax(contract, code)
        assert not result.passed
        assert "SyntaxError" in result.reason

    def test_syntax_gate_passes_non_code(self):
        contract = self._make_contract()
        result = _check_syntax(contract, "This is just a text explanation.")
        assert result.passed

    def test_length_gate_passes_long_enough(self):
        contract = self._make_contract(complexity=Complexity.MODERATE)
        result = _check_length(contract, "x" * 30)
        assert result.passed

    def test_length_gate_fails_too_short(self):
        contract = self._make_contract(complexity=Complexity.MODERATE)
        result = _check_length(contract, "hi")
        assert not result.passed
        assert "too short" in result.reason

    def test_structure_gate_passes_with_headings(self):
        contract = self._make_contract(task_type=TaskType.ANALYZE)
        text = "# Analysis\n\n## Section 1\n\n- Point 1\n- Point 2\n\n## Section 2\n\nDetails here."
        result = _check_structure(contract, text)
        assert result.passed

    def test_structure_gate_fails_unstructured_wall(self):
        contract = self._make_contract(task_type=TaskType.ANALYZE)
        text = "a " * 150  # 300 chars with no structure
        result = _check_structure(contract, text)
        assert not result.passed

    def test_citation_gate_passes_with_url(self):
        contract = self._make_contract(task_type=TaskType.RESEARCH)
        text = "According to research, the answer is X. Source: https://example.com/paper " * 5
        result = _check_citation(contract, text)
        assert result.passed

    def test_citation_gate_fails_no_sources(self):
        contract = self._make_contract(task_type=TaskType.RESEARCH)
        text = "The answer is simply this and nothing more. " * 10
        result = _check_citation(contract, text)
        assert not result.passed


class TestRunGatesIntegration:
    """Test the run_gates orchestrator.

    Gates auto-skip under pytest (PYTEST_CURRENT_TEST set). To test gates
    themselves, we set LLM_ROUTER_GATES=on which overrides the pytest skip.
    """

    @pytest.fixture(autouse=True)
    def _enable_gates(self, monkeypatch):
        """Force gates to run even under pytest."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setenv("LLM_ROUTER_GATES", "on")

    def test_all_gates_pass(self):
        contract = build_contract("t1", TaskType.CODE, Complexity.MODERATE, "test/m")
        code = "```python\ndef add(a, b):\n    return a + b\n```\n\nThis function adds two numbers."
        passed, results = run_gates(contract, code)
        assert passed

    def test_gate_failure_returns_false(self):
        contract = build_contract("t2", TaskType.CODE, Complexity.MODERATE, "test/m")
        passed, results = run_gates(contract, "x")
        assert not passed

    def test_no_gates_always_passes(self):
        contract = build_contract("t3", TaskType.IMAGE, Complexity.SIMPLE, "test/m")
        passed, results = run_gates(contract, "")
        assert passed
        assert results == []

    def test_env_disable_skips_gates(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_GATES", "off")
        contract = build_contract("t4", TaskType.CODE, Complexity.COMPLEX, "test/m")
        passed, results = run_gates(contract, "x")  # Would normally fail length
        assert passed


class TestReceipts:
    """Test receipt computation."""

    def test_compute_receipt_calculates_savings(self):
        contract = build_contract("r1", TaskType.CODE, Complexity.MODERATE, "ollama/gemma4")
        gate_results = [GateResult(gate=GateType.SYNTAX, passed=True)]

        receipt = compute_receipt(
            contract=contract,
            gate_results=gate_results,
            latency_ms=500.0,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.0,  # Ollama is free
        )

        assert receipt.savings_usd > 0  # Free model vs Opus pricing
        assert receipt.tokens_reclaimed == 1500  # input + output
        assert receipt.all_passed is True
        assert "syntax" in receipt.gates_passed

    def test_receipt_no_savings_when_expensive(self):
        contract = build_contract("r2", TaskType.CODE, Complexity.COMPLEX, "openai/o3")
        gate_results = []

        receipt = compute_receipt(
            contract=contract,
            gate_results=gate_results,
            latency_ms=2000.0,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.10,  # Expensive model
        )

        # Opus would cost: (1000/1M)*15 + (500/1M)*75 = 0.015 + 0.0375 = 0.0525
        # Actual cost: 0.10 — more expensive than Opus, so no savings
        assert receipt.savings_usd < 0  # Actually cost MORE than Opus
        assert receipt.tokens_reclaimed == 0
