"""The hook's classifier and the shared engine must not drift apart.

N8 / A4, and the replacement for `_quarantined_tests/test_hook_equivalence.py`,
which asserted this for the pre-sync API and has been SKIPPING for a month: its
`_HOOK_PATH` used `parents[2]`, correct from `tests/` and pointing above the
repository from `_quarantined_tests/`, so the hook never loaded and all 25 cases
reported as skipped rather than failed.

`hooks/auto-route.py` still carries its own AST-extracted copy of the classifier
while `llm_router.classify` holds the shared engine. That duplication is real and
the consolidation is not done — but the dangerous part is not the duplication, it
is the two drifting silently. This pins them.

WHAT THE MEASUREMENT ACTUALLY SHOWED, because the first reading was wrong:

    hook vs classify(HOOK_POLICY)        74% agree
    hook vs classify(HOOK_LIVE_POLICY)  100% agree

The entire gap is one flag. HOOK_POLICY applies `apply_complexity_floor`, which
clamps complexity UP to a task-type floor; the hook never applies it. All 207
disagreements ran that way, 189 of them "moderate" becoming "complex".

So HOOK_POLICY — despite its name — is NOT what the hook does, and rewiring the
hook to it would route a quarter of all prompts to a more expensive tier.
HOOK_LIVE_POLICY is the behaviour-preserving target.

I earlier reported the opposite direction, that the hook was systematically MORE
expensive. That came from three hand-picked prompts compared against
ROUTER_POLICY — the MCP path's deliberately different policy — not against the
hook's own. Three cases and the wrong baseline.
"""
from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

from llm_router.classify import HOOK_LIVE_POLICY, HOOK_POLICY, complexity_for

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def hook():
    """Load the hook module. NOT skipped on failure — a hook that cannot be
    imported is the bug, and skipping is how the previous version of this test
    stayed green for a month while asserting nothing."""
    path = ROOT / "src/llm_router/hooks/auto-route.py"
    assert path.exists(), f"hook not found at {path}"
    spec = importlib.util.spec_from_file_location("_equiv_hook", path)
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
]
TASKS = ["query", "code", "analyze", "generate", "research"]


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize("prompt", CORPUS)
def test_the_shared_engine_reproduces_the_hook(hook, prompt, task):
    mine = hook.classify_complexity(prompt, task)
    shared = getattr(complexity_for(prompt, policy=HOOK_LIVE_POLICY, task_type=task),
                     "value", None)
    assert mine == shared, (
        f"hook={mine} shared={shared} for task={task}. The duplicated classifier "
        "has drifted from the shared engine; consolidating them is no longer "
        "behaviour-preserving."
    )


def test_hook_policy_is_not_what_the_hook_does(hook):
    """Pins the trap, so nobody rewires to the wrong policy on the strength of
    its name."""
    differing = [
        (p, t) for p in CORPUS for t in TASKS
        if hook.classify_complexity(p, t)
        != getattr(complexity_for(p, policy=HOOK_POLICY, task_type=t), "value", None)
    ]
    assert differing, (
        "HOOK_POLICY now reproduces the hook. If that is deliberate, delete this "
        "test and rewire the hook; if it is accidental, a routing change just "
        "shipped unnoticed."
    )


def test_the_difference_is_only_the_floor_and_only_upward(hook):
    rank = {"simple": 0, "moderate": 1, "complex": 2, "deep_reasoning": 3}
    for p in CORPUS:
        for t in TASKS:
            mine = hook.classify_complexity(p, t)
            floored = getattr(complexity_for(p, policy=HOOK_POLICY, task_type=t), "value", None)
            assert rank.get(floored, 0) >= rank.get(mine, 0), (
                f"the floor made {p[:30]!r} CHEAPER ({mine} -> {floored}); it is "
                "documented as clamping up only"
            )


@pytest.mark.slow
def test_equivalence_holds_across_the_real_corpus(hook):
    """The 8 prompts above are a smoke test. This is the claim."""
    path = Path("/tmp/corpus.json")
    if not path.exists():
        pytest.skip("real prompt corpus not present on this machine")
    corpus = json.loads(path.read_text())
    random.Random(11).shuffle(corpus)
    bad = []
    for p in corpus[:200]:
        for t in TASKS:
            mine = hook.classify_complexity(p, t)
            shared = getattr(complexity_for(p, policy=HOOK_LIVE_POLICY, task_type=t),
                             "value", None)
            if mine != shared:
                bad.append((p[:40], t, mine, shared))
    assert not bad, f"{len(bad)} of 1000 disagreed, e.g. {bad[:3]}"
