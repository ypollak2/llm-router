"""The hook must not apply the complexity floor. Measured, not preferred.

The open question from A4: `classify.HOOK_POLICY` sets `apply_floor=True`, so
`apply_complexity_floor` clamps complexity UP to a task-type floor. The hook has
never applied it. Which is correct was left open on 2026-09-15 because it is a
routing question and needed a measurement rather than an opinion.

Here is the measurement. Over 150 real prompts x 3 task types, comparing the chain
`build_chain` produces with and without the floor:

    chain unchanged by the floor   297  (66%)
    chain CHANGED by the floor     153  (34%)

And the change is not subtle. Every example puts a premium model at the HEAD of
the chain for a prompt that plainly does not need one:

    "OK, let's do that"        analyze: moderate -> complex
        ['qwen3.8:latest', 'qwen3-coder:30b'] -> ['claude-opus-4-6', 'qwen3.8:latest']

The floor's purpose — stopping under-routing — is real, and on a metered API it
would be the right default. On a flat-rate subscription where the whole point is
to spend local capacity first, routing "OK, so do as you want" to Opus defeats the
tool. The answer is no, and the reason is the deployment, not the algorithm.

This test exists so the question is not reopened by someone reading HOOK_POLICY's
name and assuming the hook should match it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from llm_router.classify import HOOK_LIVE_POLICY, HOOK_POLICY, complexity_for
from llm_router.hooks.chain_builder import build_chain

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location(
        "_floor_hook", ROOT / "src/llm_router/hooks/auto-route.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


TRIVIAL = [
    "OK, let's do that",
    "OK, so do as you want",
    "yes please",
    "why those drafts weren't used?",
]


@pytest.mark.parametrize("prompt", TRIVIAL)
def test_a_trivial_prompt_does_not_reach_a_premium_model(hook, prompt):
    complexity = hook.classify_complexity(prompt, "analyze")
    chain = [m.model for m in build_chain(complexity, "green", "analyze")]
    assert chain, "empty chain"
    assert "claude" not in chain[0].lower(), (
        f"{prompt!r} leads with {chain[0]} — a premium model at the head of the "
        "chain for a prompt that needs nothing of the sort. This is what applying "
        "the complexity floor does on 34% of real prompts."
    )


@pytest.mark.parametrize("prompt", TRIVIAL)
def test_the_floor_is_what_would_put_it_there(prompt):
    """Names the mechanism, so a regression points at its cause."""
    without = complexity_for(prompt, policy=HOOK_LIVE_POLICY, task_type="analyze").value
    with_floor = complexity_for(prompt, policy=HOOK_POLICY, task_type="analyze").value
    assert without != with_floor, (
        f"{prompt!r} is no longer affected by the floor. If HOOK_POLICY changed, "
        "re-run the measurement in this module's docstring before trusting it."
    )
    head_without = [m.model for m in build_chain(without, "green", "analyze")][0]
    head_with = [m.model for m in build_chain(with_floor, "green", "analyze")][0]
    assert "claude" not in head_without.lower()
    assert "claude" in head_with.lower(), (
        "the floor no longer promotes to a premium model; the cost argument in "
        "this module may no longer hold"
    )


def test_the_hook_still_declines_the_floor(hook):
    """The decision itself. Flip this and 34% of prompts change tier."""
    for prompt in TRIVIAL:
        for task in ("query", "code", "analyze"):
            assert hook.classify_complexity(prompt, task) == complexity_for(
                prompt, policy=HOOK_LIVE_POLICY, task_type=task).value, (
                "the hook has started applying the complexity floor. That is a "
                "routing change, not a refactor: measure it before shipping."
            )
