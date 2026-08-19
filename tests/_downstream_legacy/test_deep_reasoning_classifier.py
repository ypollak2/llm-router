"""Tests for the deep_reasoning complexity classifier patterns.

Validates both the formal (academic) triggers and the new natural-language
chain-of-thought triggers added to COMPLEXITY_DEEP_REASONING in auto-route.py
and the RouterArena submission router.

Tests are parameterized so adding new patterns to the regex is one line here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Import the regex directly from auto-route.py (it's a standalone hook file,
# not a regular module) by loading it via importlib.
_HOOK_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "llm_router" / "hooks" / "auto-route.py"
)


def _load_hook_regex() -> re.Pattern:
    """Load COMPLEXITY_DEEP_REASONING from auto-route.py without side-effects."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("auto_route", _HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # auto-route.py imports many things; stub them out so we don't need the
    # full runtime environment just for the regex constant.
    for stub in ("json", "os", "sys", "re", "time", "subprocess", "pathlib"):
        if stub not in sys.modules:
            sys.modules[stub] = __import__(stub)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        pass  # some imports may fail — COMPLEXITY_DEEP_REASONING is defined early
    return mod.COMPLEXITY_DEEP_REASONING  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def deep_reason_pattern() -> re.Pattern:
    return _load_hook_regex()


# ── RouterArena router: same regex must be kept in sync ──────────────────────

def _load_arena_regex() -> re.Pattern:
    """Load _COMPLEXITY_DEEP_REASONING from the RouterArena submission router."""
    import importlib.util
    arena_path = (
        Path(__file__).resolve().parent.parent
        / "bench" / "routerarena" / "submission" / "router" / "llm_router_router.py"
    )
    spec = importlib.util.spec_from_file_location("llm_router_router_arena", arena_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Stub the BaseRouter import
    import types
    fake_base = types.ModuleType("router_inference.router.base_router")
    fake_base.BaseRouter = object  # type: ignore[attr-defined]
    sys.modules.setdefault("router_inference", types.ModuleType("router_inference"))
    sys.modules.setdefault("router_inference.router", types.ModuleType("router_inference.router"))
    sys.modules["router_inference.router.base_router"] = fake_base
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        pass
    return mod._COMPLEXITY_DEEP_REASONING  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def arena_deep_reason_pattern() -> re.Pattern:
    return _load_arena_regex()


# ── Formal / academic prompts — must trigger deep_reasoning ──────────────────

FORMAL_TRIGGERS = [
    "Prove that sqrt(2) is irrational.",
    "Give a formal proof of the fundamental theorem of calculus.",
    "Mathematically derive the Black-Scholes equation.",
    "State and prove the theorem about prime factorisation.",
    "This argument uses proof by contradiction.",
    "Prove this by induction on n.",
    "Provide a lemma for the convergence case.",
    "Use the axiom of choice to show that…",
    "This follows from a corollary of the mean value theorem.",
    "Formally specify the invariants of this data structure.",
    "Derive Euler's formula from first principles.",
    "Rigorous analysis of the time complexity.",
    "Philosophical analysis of free will.",
    "What does it mean fundamentally for a program to terminate?",
    "Synthesize the research on transformer attention mechanisms.",
    "This requires proof by reductio ad absurdum.",
    "Derive from fundamentals how TCP ensures reliability.",
]

# ── Natural-language chain-of-thought triggers — must trigger deep_reasoning ─

COT_TRIGGERS = [
    "Explain this step by step.",
    "Walk me through the reasoning behind quicksort.",
    "Think through this carefully before answering.",
    "Reason about why this algorithm is O(n log n).",
    "I need a chain-of-thought explanation here.",
    "Show your work when solving this.",
    "Think step-by-step through the proof.",
    "Walk me through the logic of this design decision.",
    "Explain your reasoning for choosing this architecture.",
    "Think out loud as you solve this.",
    "Reason out loud: why does this race condition occur?",
    "I need you to think carefully and deeply about this.",
    "Do a deep-dive into the memory model.",
    "Root cause analysis of the production incident.",
    "I want to understand exactly why this fails.",
    "Trace through the logic of the parser.",
    "What is the underlying reason for this latency spike?",
    "Trace through the chain of events leading to the crash.",
]

# ── Prompts that must NOT trigger deep_reasoning ─────────────────────────────

NON_TRIGGERS = [
    "What is 2 + 2?",
    "List files in the current directory.",
    "Summarize this paragraph.",
    "Write a hello world in Python.",
    "Translate this to French.",
    "Fix the typo in this function name.",
    "What does this variable do?",
    "Briefly explain how HTTP works.",
    "What time is it in Tokyo?",
    "Show me the syntax for a for-loop in Rust.",
]


@pytest.mark.parametrize("prompt", FORMAL_TRIGGERS)
def test_formal_trigger_detected(deep_reason_pattern: re.Pattern, prompt: str) -> None:
    assert deep_reason_pattern.search(prompt), (
        f"Expected FORMAL deep_reasoning trigger not detected: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", COT_TRIGGERS)
def test_cot_trigger_detected(deep_reason_pattern: re.Pattern, prompt: str) -> None:
    assert deep_reason_pattern.search(prompt), (
        f"Expected COT deep_reasoning trigger not detected: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", NON_TRIGGERS)
def test_non_triggers_not_detected(deep_reason_pattern: re.Pattern, prompt: str) -> None:
    assert not deep_reason_pattern.search(prompt), (
        f"False-positive deep_reasoning trigger on: {prompt!r}"
    )


# ── RouterArena router: same triggers must fire there too ────────────────────

@pytest.mark.parametrize("prompt", FORMAL_TRIGGERS)
def test_arena_formal_trigger_detected(arena_deep_reason_pattern: re.Pattern, prompt: str) -> None:
    assert arena_deep_reason_pattern.search(prompt), (
        f"RouterArena regex missed formal trigger: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", COT_TRIGGERS)
def test_arena_cot_trigger_detected(arena_deep_reason_pattern: re.Pattern, prompt: str) -> None:
    assert arena_deep_reason_pattern.search(prompt), (
        f"RouterArena regex missed COT trigger: {prompt!r}"
    )


@pytest.mark.parametrize("prompt", NON_TRIGGERS)
def test_arena_non_triggers_not_detected(arena_deep_reason_pattern: re.Pattern, prompt: str) -> None:
    assert not arena_deep_reason_pattern.search(prompt), (
        f"RouterArena false-positive on: {prompt!r}"
    )


class TestAutoRouteClassifyComplexity:
    """End-to-end: the classify_complexity function in auto-route.py must return
    'deep_reasoning' for formal and COT prompts."""

    @pytest.fixture(scope="class")
    def classify(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("auto_route_classify", _HOOK_PATH)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        except Exception:
            pass
        return getattr(mod, "_classify_complexity", None)

    def test_classify_complexity_returns_deep_reasoning_for_formal(self, classify) -> None:
        if classify is None:
            pytest.skip("_classify_complexity not importable from auto-route.py")
        result = classify("Prove that there are infinitely many primes.", "analyze")
        assert result == "deep_reasoning"

    def test_classify_complexity_returns_deep_reasoning_for_step_by_step(self, classify) -> None:
        if classify is None:
            pytest.skip("_classify_complexity not importable from auto-route.py")
        result = classify("Explain this algorithm step by step.", "query")
        assert result == "deep_reasoning"

    def test_classify_complexity_returns_deep_reasoning_for_walk_through(self, classify) -> None:
        if classify is None:
            pytest.skip("_classify_complexity not importable from auto-route.py")
        result = classify("Walk me through the reasoning for this design decision.", "analyze")
        assert result == "deep_reasoning"
