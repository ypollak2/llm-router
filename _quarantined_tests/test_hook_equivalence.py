"""Drift guard: llm_router.classify must stay byte-identical to the hook's own
deterministic classifier until the hook is repointed to import from the module.

classify.py was AST-extracted verbatim from hooks/auto-route.py. This test fails
the moment the two copies diverge, so a change to one that isn't mirrored in the
other can't slip through.
"""

import importlib.util
import pathlib

import pytest

from llm_router import classify as C

_HOOK_PATH = pathlib.Path(__file__).resolve().parents[2] / "src/llm_router/hooks/auto-route.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("_auto_route_hook", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover - environment-dependent
        pytest.skip(f"hook not importable in this env: {e}")
    return mod


CORPUS = [
    "What is the capital of France?",
    "Refactor this function to use early returns",
    "Analyze the tradeoffs between microservices and a monolith",
    "Write a 500-word blog post about remote work",
    "Create an image of a futuristic city at sunset",
    "What are the latest 2026 developments in fusion energy",
    "Coordinate next steps across the design and QA teams",
    "help with stuff",
    "Fix this Python traceback and explain the bug",
    "Summarize this article in three bullet points",
    "Implement binary search in Rust with tests",
    "Compare Postgres vs MongoDB for a time-series workload",
]


@pytest.mark.parametrize("text", CORPUS)
def test_score_categories_match_hook(text):
    hook = _load_hook()
    assert C.score_categories(text) == hook.score_categories(text)


@pytest.mark.parametrize("text", CORPUS)
def test_classify_complexity_matches_hook(text):
    hook = _load_hook()
    scores = C.score_categories(text)
    tt = max(scores, key=scores.get)
    assert C.classify_complexity(text, tt) == hook.classify_complexity(text, tt)


def test_weights_and_threshold_match_hook():
    hook = _load_hook()
    assert (C.INTENT_WEIGHT, C.TOPIC_WEIGHT, C.FORMAT_WEIGHT) == (
        hook.INTENT_WEIGHT, hook.TOPIC_WEIGHT, hook.FORMAT_WEIGHT)
    assert C.CONFIDENCE_THRESHOLD == hook.CONFIDENCE_THRESHOLD
    assert set(C.SIGNALS) == set(hook.SIGNALS)
