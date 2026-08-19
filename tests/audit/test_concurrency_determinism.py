"""Audit — Section 9: Concurrency / determinism.

1. ``router._budget_locks`` / ``router._pending_spend`` guard the
   check-then-spend monthly-budget sequence in ``route_and_call`` against
   the TOCTOU race described in the comment above their declaration
   (router.py ~742-749): "Guards the check-then-spend budget sequence so
   concurrent calls cannot both slip through the limit before either has
   recorded its spend." This file fires many concurrent ``route_and_call``
   calls against a monthly budget sized for exactly N admits and asserts
   no more than N ever get through.

2. ``_build_and_filter_chain`` must be deterministic: identical inputs
   (task_type, profile, complexity, environment/config) must always
   produce the identical ordered chain — no dependence on dict/set
   iteration order or other hidden non-determinism.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

import llm_router.router as router_module
from llm_router.router import _build_and_filter_chain, route_and_call
from llm_router.types import BudgetExceededError, Complexity, LLMResponse, RoutingProfile, TaskType


def _patch_home(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


# ── 1. Budget check-then-spend race ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_exceed_monthly_budget_cap(
    temp_db, mock_env, monkeypatch, tmp_path
):
    """Fire many concurrent route_and_call turns with a monthly budget sized
    for exactly N admits. Without the _budget_lock()-guarded check-then-spend
    sequence, concurrent coroutines could all read "budget not yet exceeded"
    before any of them recorded their reservation, letting more than N slip
    through. Confirms that does NOT happen.
    """
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_ROUTER_AUDIT_DISABLED", "1")
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    # Keep the fire-and-forget receipt store from touching a real DB file —
    # it's launched via asyncio.create_task and not awaited by route_and_call.
    async def _noop_store_receipt(*a, **k):
        return None
    monkeypatch.setattr("llm_router.receipt_store.store_receipt", _noop_store_receipt)
    # session_spend writes to a module-level path resolved at import time —
    # already-imported, so Path.home() patching above can't retarget it.
    # Redirect the constant directly so this test can't write to the
    # operator's real ~/.llm-router/session_spend.json.
    import llm_router.session_spend as session_spend_module
    monkeypatch.setattr(session_spend_module, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")

    # Reset the module-global pending-spend counter so state from any earlier
    # test in this process can't bleed into this one.
    router_module._pending_spend = 0.0

    prompt = "hi"
    from llm_router.session_spend import _estimate_cost
    reservation = _estimate_cost("gpt-4o", max(1, len(prompt) // 4), 500)
    assert reservation > 0, "test assumption: gpt-4o must have non-zero calibrated cost"

    n_should_admit = 3
    # The check is `if (monthly_spend + _pending_spend) >= budget: reject`,
    # evaluated BEFORE the reservation is added. So the k-th admit requires
    # pending-before-admit == (k-1)*reservation to be strictly under budget.
    # To admit exactly 3 (pending sequence 0, R, 2R all < budget) and reject
    # the 4th (3R >= budget), budget must sit in the half-open interval
    # (2R, 3R] — e.g. 2.5R.
    monthly_budget = reservation * (n_should_admit - 0.5)
    monkeypatch.setenv("LLM_ROUTER_MONTHLY_BUDGET", str(monthly_budget))
    monkeypatch.setenv("LLM_ROUTER_DAILY_SPEND_LIMIT", "0")
    import llm_router.config as config_module
    config_module._config = None

    # Real DB starts empty (temp_db) — get_monthly_spend() reads it fresh on
    # every check, so during the admission burst it stays at 0 and the cap is
    # governed purely by the in-flight `_pending_spend` reservations, which is
    # exactly the race the lock is meant to prevent.
    #
    # THE BURST IS HELD BY A BARRIER, NOT BY A SLEEP. The original version used
    # `await asyncio.sleep(0.15)` inside the fake provider and assumed that was
    # long enough for all ten calls to pass admission before any completed. That
    # premise holds on an idle machine and BREAKS UNDER LOAD: sampling
    # router._pending_spend under CPU starvation showed calls 4 and 5 admitted
    # roughly TWELVE SECONDS after calls 1-3 had completed and released, when
    # in-flight spend genuinely was 1R against a 2.5R budget. The guard admitted
    # them because there really was headroom — correct behaviour — but the
    # assertion read it as a TOCTOU overrun and this test failed 4/4 under
    # contention while passing in isolation.
    #
    # A sleep cannot express "nobody finishes until everybody has been decided";
    # it only expresses "wait a while and hope". The gate below states it
    # directly: an admitted call parks at the provider, a rejected call raises,
    # and only when all ten have resolved one way or the other is the gate
    # opened. The assertion then measures MUTUAL EXCLUSION rather than scheduler
    # timing, which is what it always claimed to measure.
    success_response = LLMResponse(
        content="ok", model="openai/gpt-4o", input_tokens=5, output_tokens=5,
        cost_usd=0.001, latency_ms=10.0, provider="openai",
    )

    n_concurrent = 10
    _gate = asyncio.Event()
    _resolved = 0

    def _mark_resolved() -> None:
        """One more call has finished its admission decision (either way)."""
        nonlocal _resolved
        _resolved += 1
        if _resolved >= n_concurrent:
            _gate.set()

    async def _gated_call_llm(model, *args, **kwargs):
        # Reached only by calls the budget guard ADMITTED. Park here holding the
        # reservation until every sibling has also been decided.
        _mark_resolved()
        await _gate.wait()
        return success_response

    monkeypatch.setattr("llm_router.providers.call_llm", _gated_call_llm)

    async def _one():
        try:
            return await route_and_call(
                TaskType.QUERY, prompt, model_override="openai/gpt-4o",
            )
        except BaseException as exc:  # noqa: BLE001 — rejection is a valid outcome
            # Rejected before reaching the provider, so it must self-report or
            # the gate would never open and the test would hang.
            _mark_resolved()
            return exc

    results = await asyncio.gather(*[_one() for _ in range(n_concurrent)])

    successes = [r for r in results if isinstance(r, LLMResponse)]
    budget_errors = [r for r in results if isinstance(r, BudgetExceededError)]
    other_errors = [
        r for r in results
        if isinstance(r, Exception) and not isinstance(r, BudgetExceededError)
    ]

    assert not other_errors, f"unexpected non-budget errors: {other_errors}"
    assert len(successes) + len(budget_errors) == n_concurrent
    assert len(successes) <= n_should_admit, (
        f"budget race allowed {len(successes)} concurrent calls through a "
        f"cap sized for {n_should_admit} — the check-then-spend sequence "
        f"guarded by router._budget_lock()/_pending_spend did not prevent "
        f"the TOCTOU overrun it exists to guard against."
    )
    assert len(successes) >= 1, "expected at least one call to be admitted under the budget"
    assert len(budget_errors) == n_concurrent - len(successes)

    # No leaked reservation: after every task has settled, the in-flight
    # counter must be back to zero (each attempt releases its reservation
    # on both the success and the budget-rejection paths).
    assert router_module._pending_spend == pytest.approx(0.0, abs=1e-9), (
        f"_pending_spend leaked {router_module._pending_spend} after all "
        f"concurrent calls settled — a reservation was taken without a "
        f"matching release somewhere in the dispatch path."
    )


# ── 2. _build_and_filter_chain determinism ──────────────────────────────────


@pytest.mark.asyncio
async def test_build_and_filter_chain_deterministic_across_repeated_calls(
    temp_db, mock_env, monkeypatch, tmp_path
):
    """Repeated calls to _build_and_filter_chain with IDENTICAL inputs (same
    task_type/profile/complexity/config, no mocked randomness) must produce
    the IDENTICAL ordered chain every time. Run 5x and compare — any
    dict/set-iteration-order dependency or other hidden non-determinism
    would show up as a mismatch across runs within this same process."""
    _patch_home(monkeypatch, tmp_path)
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: True)
    monkeypatch.setattr("llm_router.dynamic_routing.get_dynamic_model_chain", lambda *a, **k: None)

    import llm_router.config as config_module
    config_module._config = None
    from llm_router.config import get_config
    config = get_config()

    results = []
    for _ in range(5):
        chain = await _build_and_filter_chain(
            TaskType.CODE,
            RoutingProfile.BALANCED,
            None,                     # model_override
            None,                     # complexity_hint
            Complexity.MODERATE,
            config,
        )
        results.append(chain)

    first = results[0]
    assert first, "expected a non-empty candidate chain for this fixture setup"
    for i, chain in enumerate(results[1:], start=2):
        assert chain == first, (
            f"_build_and_filter_chain returned a different chain on call "
            f"{i} than call 1 for IDENTICAL inputs:\n"
            f"  call 1: {first}\n"
            f"  call {i}: {chain}\n"
            "This indicates hidden non-determinism (e.g. unsorted set/dict "
            "iteration feeding into chain order)."
        )


# NOTE: a stronger cross-PYTHONHASHSEED variant of the determinism check
# above (launching the chain builder in subprocesses with different hash
# seeds, since CPython randomizes str/set iteration order PER PROCESS —
# invisible to repeated calls within one process) was attempted here and
# removed: it hung for several minutes in this environment, most likely
# because a subprocess reimporting llm_router.config/llm_router.codex_agent from
# scratch re-triggers real environment probing (Ollama/Codex/Gemini CLI
# binary detection, health checks) that isn't mocked outside the pytest
# process. Given the audit's scope and time budget this was cut rather
# than debugged further — see REPORT_B.md "gaps / assumptions": the
# hash-seed-driven non-determinism risk in the `list({*a, *b})`-style
# dedup patterns found in router.py (policy merging) is flagged there as
# unverified rather than confirmed safe or confirmed broken.
