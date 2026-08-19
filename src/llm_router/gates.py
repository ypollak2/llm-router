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
import logging
import os
import re
from dataclasses import dataclass

from llm_router.contract import GateType, RoutingContract
from llm_router.types import Complexity

log = logging.getLogger(__name__)

# Complexity tiers whose length-gate failure forces a downshift to a cheaper
# (potentially budget/Ollama) model. When these trip, the downgrade must be
# observable rather than silent (CHZ-AUD-014).
_PREMIUM_COMPLEXITY = (Complexity.COMPLEX, Complexity.DEEP_REASONING)


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


# CHZ-AUD-C-03: bare answers that are complete despite being short. The length
# gate exists to catch empty/truncated garbage, NOT to reject a legitimately
# terse answer and force a silent post-dispatch re-route.
_VALID_SHORT_ANSWERS = frozenset({
    "yes", "no", "true", "false", "n/a", "na", "none", "null", "nil",
    "ok", "okay", "done", "pass", "fail", "unknown",
})
_NUMERIC_ANSWER_RE = re.compile(r"[-+]?[\$€£]?\d[\d,]*(?:\.\d+)?\s?%?")


def _is_valid_short_answer(text: str) -> bool:
    """CHZ-AUD-C-03: recognise a legitimately short, complete answer (yes/no,
    a boolean, a bare number/currency/percentage, or a single short token) so
    the length gate passes it instead of triggering a silent re-dispatch."""
    s = text.strip().rstrip(".!").strip()
    if not s:
        return False
    if s.lower() in _VALID_SHORT_ANSWERS:
        return True
    if _NUMERIC_ANSWER_RE.fullmatch(s):
        return True
    # NOTE: a bare single WORD is intentionally NOT allow-listed here. For terse
    # factual Q&A (TaskType.QUERY) the contract already caps the length floor to
    # the empty-guard, so "London"/"Python" pass without this gate rejecting them;
    # for CODE/ANALYZE/RESEARCH a one-word "answer" is almost certainly truncated
    # or wrong, so it must still trip the gate rather than be waved through.
    return False


def _check_length(contract: RoutingContract, text: str) -> GateResult:
    """Verify response meets minimum length threshold."""
    min_len = contract.constraints.min_output_length
    actual = len(text.strip())

    if actual < min_len:
        # CHZ-AUD-C-03: allow-list legitimately short valid answers so a terse
        # but complete reply is not rejected and silently re-dispatched.
        if _is_valid_short_answer(text):
            return GateResult(gate=GateType.LENGTH, passed=True)
        # CHZ-AUD-014: A length-gate failure on a complex/premium task is what
        # forces the router to abandon the premium chain and (potentially)
        # emergency-fallback to a budget Ollama model. Make that downshift
        # observable — a brief but valid premium answer must not silently
        # downgrade quality without a trace. (Non-premium length rejections are
        # still surfaced by the router's `gate_verification_failed` structured
        # log and the route-quality ledger's fallback_reason — CHZ-AUD-C-03 — so
        # WHICH gate triggered the fallback is always recoverable; this WARNING
        # is the extra human-visible signal reserved for premium downshifts.)
        if contract.complexity in _PREMIUM_COMPLEXITY:
            log.warning(
                "Length gate failed on premium task (%s/%s): %d < %d chars — "
                "premium response rejected, router will downshift to a cheaper "
                "model. A brief valid answer may be forcing this downgrade.",
                contract.task_type.value,
                contract.complexity.value,
                actual,
                min_len,
            )
        return GateResult(
            gate=GateType.LENGTH,
            passed=False,
            reason=f"too short: {actual} < {min_len} chars",
        )
    return GateResult(gate=GateType.LENGTH, passed=True)


def _check_structure(contract: RoutingContract, text: str) -> GateResult:
    """Verify a long response is legible, not a single undifferentiated blob.

    This gate exists to catch **garbage** — a wall of text with no internal
    structure — not to force Markdown onto valid answers. Prose *is* structure:
    a multi-sentence, multi-paragraph answer is perfectly legible without a
    single ``##`` heading or ``-`` bullet.

    The original implementation required ≥2 Markdown markers and rejected
    everything else >200 chars. That is a false-positive machine: it discarded
    valid frontier prose (e.g. a 466-char multi-sentence answer with 0 Markdown
    markers), forcing the router down its fallback chain and — when the chain
    was exhausted — all the way to a failed dispatch. A model that *answered*
    the prompt should never be rejected for lacking bullet points.

    So a response counts as structured if ANY of these hold:
    - it has ≥2 Markdown markers (headings / list items), OR
    - it has ≥2 paragraphs (blank-line-separated blocks), OR
    - it has ≥3 sentences (sentence-terminating punctuation).

    Only a long body with none of the above — a true wall of text — fails.
    """
    stripped = text.strip()
    markers = (
        text.count("\n## ") +
        text.count("\n### ") +
        text.count("\n- ") +
        text.count("\n* ") +
        text.count("\n1. ")
    )
    # Blank-line-separated blocks → paragraphs; sentence-terminating punctuation
    # followed by whitespace/end → sentences. Both are structure in prose.
    paragraphs = sum(1 for block in re.split(r"\n\s*\n", stripped) if block.strip())
    sentences = len(re.findall(r"[.!?](?:\s|$)", stripped))

    structured = markers >= 2 or paragraphs >= 2 or sentences >= 3

    # Only long bodies are gated; a short answer can't be a "wall of text".
    if not structured and len(stripped) > 200:
        return GateResult(
            gate=GateType.STRUCTURE,
            passed=False,
            reason=(
                f"unstructured wall: {markers} markers, {paragraphs} paragraphs, "
                f"{sentences} sentences in {len(stripped)} chars"
            ),
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
