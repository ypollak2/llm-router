"""NS3: the first ollama model in execute_agent's chain must not be able to
consume the ENTIRE remaining deadline, starving both the other agent-loop
models behind it and the text-chain fallback the caller runs afterward.

Regression for 2026-09-27: `~/.llm-router/direct_samples.jsonl` held 20/20
samples at elapsed_s ~= 54-55s, all `timed_out: true` — the full hook budget,
every time. `auto-route-debug.log` on the same machine showed the mechanism: a
~55s gap between "DIRECT: zone=..." and "READ-ONLY DRAFT LOOP: nothing, text
chain next", immediately followed by 3x "DIRECT MODEL SKIPPED: ... out of hook
budget before the call" — one slow/cold-loading first model (qwen3-coder:30b,
sorted first by `_AGENT_PRIORITY`) ate the whole deadline on its own, and
nothing was left for anything that should have run after it.

`execute_chain` already reserves one fallback's worth of time
(`_FALLBACK_RESERVE_S`) for whatever is behind the current model in its chain
(see test_direct_executor_deadline.py). `execute_agent` had no equivalent
reserve at all — this is that same fix, applied to the same class of bug.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from llm_router.hooks.direct_executor import ModelSpec, execute_agent


def _spec(model: str) -> ModelSpec:
    return ModelSpec(provider="ollama", model=model)


def test_first_agent_model_cannot_consume_the_fallbacks_budget():
    """A 2-model ollama chain inside a 30s deadline must not hand model #1
    the full 30s — some of that has to be reserved for model #2."""
    seen: list[float] = []

    def _fake_run_agent_loop(*, deadline_s, **kwargs):
        seen.append(deadline_s)
        return None  # model #1 never answers; caller must still try #2

    with patch("llm_router.hooks.agent_loop.run_agent_loop", side_effect=_fake_run_agent_loop), \
         patch("llm_router.agentic_registry.get_registry", return_value={}):
        execute_agent(
            "q", [_spec("qwen3-coder:30b"), _spec("qwen3.5:latest")],
            deadline_s=time.monotonic() + 30.0,
        )

    assert len(seen) == 2, f"expected both models attempted, got {len(seen)} call(s)"
    assert seen[0] < 30.0, (
        f"model #1 was handed its full {seen[0]:.1f}s inside a 30s deadline — "
        "nothing was reserved for model #2"
    )
    assert seen[0] <= 30 - 18 + 0.5, (
        f"model #1 got {seen[0]:.1f}s, leaving under the 18s model #2 needs"
    )


def test_last_agent_model_gets_whatever_remains():
    """The LAST model in the chain has nothing behind it — no reserve applies,
    it should get everything left of the deadline (matches execute_chain's
    `_call_budget` for the same position)."""
    seen: list[float] = []

    def _fake_run_agent_loop(*, deadline_s, **kwargs):
        seen.append(deadline_s)
        return "the answer" if len(seen) == 2 else None

    with patch("llm_router.hooks.agent_loop.run_agent_loop", side_effect=_fake_run_agent_loop), \
         patch("llm_router.agentic_registry.get_registry", return_value={}):
        execute_agent(
            "q", [_spec("qwen3-coder:30b"), _spec("qwen3.5:latest")],
            deadline_s=time.monotonic() + 30.0,
        )

    assert len(seen) == 2
    # model #2 is last: no remaining ollama model behind it, no reserve.
    assert seen[1] > 30 - 18 - 0.5, (
        f"the last model in the chain got only {seen[1]:.1f}s — it should not "
        "have a reserve held back for a model that doesn't exist"
    )


def test_single_model_chain_gets_the_whole_deadline():
    """No fallback exists at all — a lone model must not be shortchanged by a
    reserve that has nothing behind it to protect."""
    seen: list[float] = []

    def _fake_run_agent_loop(*, deadline_s, **kwargs):
        seen.append(deadline_s)
        return "ok"

    with patch("llm_router.hooks.agent_loop.run_agent_loop", side_effect=_fake_run_agent_loop), \
         patch("llm_router.agentic_registry.get_registry", return_value={}):
        execute_agent("q", [_spec("qwen3-coder:30b")], deadline_s=time.monotonic() + 30.0)

    assert len(seen) == 1
    assert seen[0] > 29.0, f"the only model in the chain got only {seen[0]:.1f}s of a 30s deadline"


def test_a_model_that_uses_its_whole_budget_and_answers_nothing_logs_timeout():
    """NS3: execute_agent used to log NOTHING per model — only the caller's one
    blanket "READ-ONLY DRAFT LOOP: nothing" line. Reproduce the exact failure
    shape from auto-route-debug.log (model burns its whole `left`, returns
    None) and assert it is named a timeout, not silently dropped."""
    def _stalls_for_its_whole_budget(*, deadline_s, **kwargs):
        # A cold-load/drifting model: it burns (almost) its whole allotted
        # `left` and STILL returns nothing — the exact shape the real
        # qwen3-coder:30b calls showed in auto-route-debug.log. `left` must
        # clear `_MIN_CALL_S` (3.0s) or execute_agent skips the call entirely.
        time.sleep(max(0.0, deadline_s - 0.2))
        return None

    logged: list[str] = []
    with patch("llm_router.hooks.agent_loop.run_agent_loop", side_effect=_stalls_for_its_whole_budget), \
         patch("llm_router.agentic_registry.get_registry", return_value={}), \
         patch("llm_router.hooks.direct_executor._log_direct_reason", side_effect=logged.append):
        execute_agent("q", [_spec("qwen3-coder:30b")], deadline_s=time.monotonic() + 3.5)

    assert logged, "a model that answered nothing must log WHY, not disappear silently"
    assert any("timeout" in line.lower() for line in logged), (
        f"expected a timeout reason, got: {logged}"
    )
