import pytest

from llm_router.classify import (
    CONFIDENCE_THRESHOLD,
    ClassifySignal,
    classify_signals,
    score_categories,
)

CATEGORY_KEYS = {"query", "code", "research", "analyze", "generate", "image", "coordination"}

GOLDEN_PROMPTS = [
    ("What is the capital of France?", "query", True),
    ("Who wrote Pride and Prejudice?", "query", True),
    ("Refactor this function to use early returns", "code", True),
    ("Fix this Python traceback and explain the bug", "code", True),
    ("What are the latest 2026 developments in fusion energy", "research", True),
    ("Analyze the tradeoffs between microservices and a monolith", "analyze", True),
    ("Compare the risks and benefits of adopting Kubernetes for a small team", "analyze", True),
    ("Write a 500-word blog post about remote work", "generate", True),
    ("Create an image of a futuristic city at sunset", "image", True),
    ("Generate a logo concept for a neighborhood bakery", "image", True),
    # Pins ACTUAL production behavior (the hook routes "understand how ..." to analyze).
    ("Help me understand how recursion works", "analyze", True),
]


@pytest.mark.parametrize(("prompt", "expected_task_type", "expected_confident"), GOLDEN_PROMPTS)
def test_classify_signals_golden_routing(prompt, expected_task_type, expected_confident):
    signal = classify_signals(prompt)
    assert isinstance(signal, ClassifySignal)
    assert signal.task_type == expected_task_type, f"{prompt!r} -> {signal.task_type} (score {signal.score})"
    assert signal.complexity in {"simple", "moderate", "complex", "deep_reasoning"}
    assert isinstance(signal.score, int) and signal.score >= 0
    assert signal.confident == (signal.score >= CONFIDENCE_THRESHOLD)


@pytest.mark.parametrize("prompt", ["", "   \n\t  ", "x" * 5000,
    "Unicode: こんにちは \U0001f680 café", "```python\ndef add(a, b):\n    return a + b\n```"])
def test_never_raises_on_adversarial_input(prompt):
    signal = classify_signals(prompt)
    assert isinstance(signal, ClassifySignal)
    assert signal.task_type in CATEGORY_KEYS
    assert isinstance(signal.score, int) and signal.score >= 0
    assert isinstance(signal.confident, bool)


def test_score_categories_shape():
    scores = score_categories("Analyze and refactor this function")
    assert isinstance(scores, dict)
    assert set(scores) == CATEGORY_KEYS
    assert all(isinstance(v, int) and v >= 0 for v in scores.values())


def test_confidence_gate_high_signal():
    s = classify_signals("Refactor this Python function to use early returns")
    assert s.task_type == "code" and s.confident is True


def test_confidence_gate_vague():
    s = classify_signals("help with stuff")
    assert s.confident == (s.score >= CONFIDENCE_THRESHOLD)
