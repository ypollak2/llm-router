"""Verification gates — lightweight output validators for routed responses.

Gates run automatically after a model returns a response but before the
response is accepted. If any gate fails, the router retries with the next
model in the chain (existing fallback behavior).

Design principles:
- Gates must be FAST (< 50ms each) — no subprocess calls, no network.
- Gates verify structure, not correctness — they catch garbage, not wrong answers.
- Gate failure triggers retry, not error — the user never sees gate failures.

v8.8.0: Contract-as-Infrastructure.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass

from llm_router.contract import GateType, RoutingContract


@dataclass(frozen=True)
class GateResult:
    """Result of running a single verification gate."""

    gate: GateType
    passed: bool
    reason: str = ""


def run_gates(contract: RoutingContract, response_text: str) -> tuple[bool, list[GateResult]]:
    """Run all gates defined in the contract against the response.

    Returns:
        (all_passed, results) — all_passed is True only if every gate passed.

    Gates are skipped entirely when:
    - No gates defined in contract
    - LLM_ROUTER_GATES=off (explicit disable)
    - Running under pytest (PYTEST_CURRENT_TEST set)
    """
    if not contract.gates:
        return True, []

    gates_env = os.environ.get("LLM_ROUTER_GATES", "").lower()
    # Explicit disable
    if gates_env == "off":
        return True, []
    # Auto-skip under pytest unless explicitly forced on
    if gates_env != "on" and os.environ.get("PYTEST_CURRENT_TEST"):
        return True, []

    results: list[GateResult] = []
    for gate in contract.gates:
        result = _GATE_RUNNERS[gate](contract, response_text)
        results.append(result)

    all_passed = all(r.passed for r in results)
    return all_passed, results


def _check_syntax(contract: RoutingContract, text: str) -> GateResult:
    """Verify that code output is syntactically valid Python.

    Only checks if the response contains a code block or looks like code.
    Non-code responses pass automatically.
    """
    # Extract code blocks if present
    code_blocks = re.findall(r"```(?:python|py)?\n(.*?)```", text, re.DOTALL)

    if not code_blocks:
        # If no fenced code block, check if the whole response looks like code
        lines = text.strip().split("\n")
        code_indicators = sum(
            1 for line in lines[:10]
            if line.strip().startswith(("def ", "class ", "import ", "from ", "if ", "for ", "return "))
        )
        if code_indicators < 2:
            # Not code — pass (this gate doesn't apply)
            return GateResult(gate=GateType.SYNTAX, passed=True, reason="non-code response")
        code_blocks = [text]

    # Check each code block for syntax errors
    for block in code_blocks:
        try:
            ast.parse(block)
        except SyntaxError as e:
            return GateResult(
                gate=GateType.SYNTAX,
                passed=False,
                reason=f"SyntaxError at line {e.lineno}: {e.msg}",
            )

    return GateResult(gate=GateType.SYNTAX, passed=True)


def _check_length(contract: RoutingContract, text: str) -> GateResult:
    """Verify response meets minimum length threshold."""
    min_len = contract.constraints.min_output_length
    actual = len(text.strip())

    if actual < min_len:
        return GateResult(
            gate=GateType.LENGTH,
            passed=False,
            reason=f"too short: {actual} < {min_len} chars",
        )
    return GateResult(gate=GateType.LENGTH, passed=True)


def _check_structure(contract: RoutingContract, text: str) -> GateResult:
    """Verify response has structural elements (headings, sections, lists).

    For analysis tasks, a wall of unstructured text is a quality signal.
    """
    structural_markers = (
        text.count("\n## ") +
        text.count("\n### ") +
        text.count("\n- ") +
        text.count("\n* ") +
        text.count("\n1. ")
    )

    # At least 2 structural elements for moderate+ analysis
    if structural_markers < 2 and len(text) > 200:
        return GateResult(
            gate=GateType.STRUCTURE,
            passed=False,
            reason=f"no structure: {structural_markers} markers in {len(text)} chars",
        )
    return GateResult(gate=GateType.STRUCTURE, passed=True)


def _check_format(contract: RoutingContract, text: str) -> GateResult:
    """Verify response matches required format (JSON, markdown, code)."""
    fmt = contract.constraints.required_format
    if fmt is None:
        return GateResult(gate=GateType.FORMAT, passed=True, reason="no format required")

    if fmt == "json":
        import json
        try:
            json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return GateResult(gate=GateType.FORMAT, passed=False, reason="invalid JSON")

    return GateResult(gate=GateType.FORMAT, passed=True)


def _check_citation(contract: RoutingContract, text: str) -> GateResult:
    """Verify research responses include references/sources.

    Looks for URLs, citation patterns, or explicit source mentions.
    """
    has_url = bool(re.search(r"https?://[^\s]+", text))
    has_citation = bool(re.search(r"\[\d+\]|\[source\]|according to|per\s", text, re.IGNORECASE))
    has_reference = "reference" in text.lower() or "source" in text.lower()

    if has_url or has_citation or has_reference:
        return GateResult(gate=GateType.CITATION, passed=True)

    # Only fail for longer responses where citations are expected
    if len(text) > 300:
        return GateResult(
            gate=GateType.CITATION,
            passed=False,
            reason="no citations found in research response",
        )
    return GateResult(gate=GateType.CITATION, passed=True)


# Registry mapping gate types to their runner functions.
_GATE_RUNNERS: dict[GateType, callable] = {
    GateType.SYNTAX: _check_syntax,
    GateType.LENGTH: _check_length,
    GateType.STRUCTURE: _check_structure,
    GateType.FORMAT: _check_format,
    GateType.CITATION: _check_citation,
}
