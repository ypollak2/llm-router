"""The hook decides with the one engine; it must not grow a classifier of its own again.

History (N8 / A4): `hooks/auto-route.py` carried its own AST-extracted copy of the
classifier and the copy drifted from `llm_router.classify`. It was first made a
delegation (`HOOK_LIVE_POLICY`, 1000/1000 against the old copy), and with PLAN v16 P1.6
the hook calls `engine.decide(door="hook")`. What this pins now:

  * the hook has no `classify_complexity`, no layer-2/3 LLM classifier call in
    `classify_prompt`, and does not read `DISABLE_LLM_CLASSIFIERS` there;
  * for every prompt that no hook gate takes, `classify_prompt` returns exactly the
    engine's `(task_type, complexity)`;
  * the policy does not apply the complexity floor (see
    `test_hook_does_not_apply_the_complexity_floor.py`).
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from llm_router import engine

ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = ROOT / "src/llm_router/hooks/auto-route.py"


@pytest.fixture(scope="module")
def hook():
    """Load the hook module. NOT skipped on failure: a hook that cannot be imported is
    the bug, and skipping is how an earlier version of this test stayed green for a month
    while asserting nothing."""
    assert HOOK_PATH.exists(), f"hook not found at {HOOK_PATH}"
    spec = importlib.util.spec_from_file_location("_equiv_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


CORPUS = [
    "what is the capital of France?",
    "fix the retry ordering in the client",
    "refactor the session store to take an explicit project root",
    "keep going",
    "Design a caching layer with eviction, metrics and a migration plan, "
    "covering the read path, the write path and the failure modes." * 3,
    "ok",
    "why did that draft get rejected?",
    "run the tests and tell me what broke",
    "explain how the retry loop works",
    "summarize the incident report for the team",
]
GATES = ("coordination-fast-path", "introspection-fast-path")


@pytest.mark.parametrize("prompt", CORPUS)
def test_the_hook_returns_the_engines_decision_unless_a_gate_takes_the_prompt(hook, prompt):
    r = hook.classify_prompt(prompt)
    d = engine.decide(prompt, door="hook")
    if r is None or r["method"] in GATES or r["task_type"] == "coordination":
        pytest.skip("a hook gate took this prompt")
    assert (r["task_type"], r["complexity"]) == (d.task_type, d.complexity), (
        "the hook disagrees with engine.decide for an ungated prompt: a second classifier has crept back in"
    )


def test_enough_of_the_corpus_is_ungated_for_the_check_above_to_mean_something(hook):
    ungated = [p for p in CORPUS
               if (r := hook.classify_prompt(p)) is not None
               and r["method"] not in GATES and r["task_type"] != "coordination"]
    assert len(ungated) >= 6, "an empty comparison passes everything"


def test_the_hook_has_no_classifier_of_its_own(hook):
    assert not hasattr(hook, "classify_complexity")
    tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "classify_prompt")
    called = {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert not called & {"classify_with_ollama", "classify_with_gemini", "classify_with_openai",
                         "classify_complexity", "complexity_for"}, called
    assert "DISABLE_LLM_CLASSIFIERS" not in {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
