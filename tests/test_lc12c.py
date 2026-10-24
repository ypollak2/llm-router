"""LC-12b / D-277: flag unset = the passed local model ALONE, driven through llm_text_job -> route_and_call.

Only the provider call layer (``router._call_text``), the chain builder and, where a test needs a step to remove
the local model, the capability filter's verdict are replaced. Tmp homes, fake digests, no network, no model.
"""
# ruff: noqa: F811  (the `world` fixture is imported from test_lc12 and used as an argument)
from __future__ import annotations

import pytest

from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.text_jobs import MAX_JOB_ATTEMPTS
from llm_router.tools import text as T
from llm_router.types import Complexity, LLMResponse, TaskType
from tests.test_lc12 import FLAG, call_job, passed, world  # noqa: F401
from tests.test_lc8_routing import BARE, JOB

LOCAL = f"ollama/{BARE}"
CLOUD = ["gemini/gemini-2.5-flash", "anthropic/claude-sonnet-5"]
GOOD = "feat: touch src/a.py"
BAD_TEXT = "fix bug in one spot"  # names no changed file, so the acceptance check rejects it


@pytest.fixture
def e2e(world, tmp_path, mock_env, temp_db, monkeypatch):
    """The real route_and_call under llm_text_job; ``_call_text`` is the only model call, recorded in ``tried``."""
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "provider_reset.json"))
    monkeypatch.setattr(router, "is_codex_available", lambda: False)
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setattr(T, "route_and_call", router.route_and_call)
    state = {"chain": [LOCAL, *CLOUD], "script": {}, "tried": []}

    async def build(*a, **k):
        return list(state["chain"])

    async def call_text(model, *a, **k):
        state["tried"].append(model)
        out = state["script"].get(model, GOOD)
        if isinstance(out, Exception):
            raise out
        return LLMResponse(content=out, model=model, input_tokens=5, output_tokens=5, cost_usd=0.0,
                           latency_ms=1.0, provider=model.split("/", 1)[0])

    monkeypatch.setattr(router, "_build_and_filter_chain", build)
    monkeypatch.setattr(router, "_call_text", call_text)
    from unittest.mock import AsyncMock
    monkeypatch.setattr("llm_router.cost.get_daily_spend", AsyncMock(return_value=0.0))
    monkeypatch.setattr("llm_router.cost.get_monthly_spend", AsyncMock(return_value=0.0))
    world.state = state
    return world


def _drain():
    import asyncio
    asyncio.run(router.drain_bg_tasks(timeout_s=5.0))


def test_accepted_on_the_passed_model_alone(e2e):
    passed(e2e)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL]
    assert "status=accepted, attempts=1" in out


def test_unset_local_error_hands_back_and_calls_no_cloud_model(e2e):
    passed(e2e)
    e2e.state["script"][LOCAL] = RuntimeError("ollama down")
    before = router._pending_spend
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL]  # the one local attempt; no cloud entry was called
    assert "status=fallback, attempts=1" in out and "Do this job yourself." in out
    assert "no cloud model was called" in out
    assert abs(router._pending_spend - before) < 1e-9


def test_unset_rejected_answers_retry_on_the_local_model_only_then_hand_back(e2e):
    passed(e2e)
    e2e.state["script"][LOCAL] = BAD_TEXT
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL] * MAX_JOB_ATTEMPTS
    assert f"status=rejected, attempts={MAX_JOB_ATTEMPTS}" in out and "do this job yourself" in out.lower()


def test_unset_local_error_after_a_rejection_hands_back_with_the_real_attempt_count(e2e, monkeypatch):
    passed(e2e)
    seq = iter([BAD_TEXT, RuntimeError("ollama went away")])

    async def flaky(model, *a, **k):
        e2e.state["tried"].append(model)
        x = next(seq)
        if isinstance(x, Exception):
            raise x
        return LLMResponse(content=x, model=model, input_tokens=1, output_tokens=1, cost_usd=0.0,
                           latency_ms=1.0, provider="ollama")

    monkeypatch.setattr(router, "_call_text", flaky)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL, LOCAL]
    assert "status=fallback, attempts=2" in out


def test_flag_on_local_error_falls_through_to_the_next_entry(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)
    e2e.state["script"][LOCAL] = RuntimeError("ollama down")
    out = call_job()
    _drain()
    tried = e2e.state["tried"]
    assert tried[0] == LOCAL and len(tried) >= 2 and tried[1] != LOCAL  # fell through to the next entry
    assert "status=accepted" in out


def test_flag_on_still_raises_when_every_entry_fails(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)
    for m in (LOCAL, *CLOUD, "openai/gpt-4o-mini"):
        e2e.state["script"][m] = RuntimeError("down")
    with pytest.raises(Exception):  # noqa: B017,PT011 - the router's own exhaustion error, as before
        call_job()
    _drain()


def test_unset_passed_model_missing_from_the_chain_refuses_with_no_model_call_and_no_leak(e2e):
    passed(e2e)
    e2e.state["chain"] = list(CLOUD)
    before = router._pending_spend
    out = call_job()
    _drain()
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out and "not in the router's chain" in out
    assert router._pending_spend == before  # PR #705 review: the reservation was leaking here


def test_route_and_call_refusal_releases_the_reservation(e2e, monkeypatch):
    """Straight at route_and_call: the PassedModelNotInChain raise leaves _pending_spend as it was."""
    import asyncio
    passed(e2e)
    e2e.state["chain"] = list(CLOUD)
    seen = {}
    real = router._drop_failed_eligibility

    def spy(*a, **k):
        seen["pending_during"] = router._pending_spend
        return real(*a, **k)

    monkeypatch.setattr(router, "_drop_failed_eligibility", spy)
    t1 = er.set_text_job_context(JOB, 10)
    t2 = er.set_require_passed_first()
    before = router._pending_spend
    try:
        with pytest.raises(er.PassedModelNotInChain):
            asyncio.run(router.route_and_call(TaskType.GENERATE, "p", eligibility_exempt_reason=er.TEXT_JOB_REASON))
        asyncio.run(router.drain_bg_tasks(timeout_s=5.0))
    finally:
        er.reset_require_passed_first(t2)
        er.reset_text_job_context(t1)
    assert seen["pending_during"] > before  # the reservation WAS held at the refusal point
    assert router._pending_spend == before


def test_unset_capability_filter_removing_the_local_model_refuses_and_releases(e2e, monkeypatch):
    passed(e2e)
    from llm_router import capability_filter as cf
    monkeypatch.setattr(cf, "enabled", lambda: True)
    monkeypatch.setattr(cf, "filter_chain_report",
                        lambda models, needs: ([m for m in models if m != LOCAL], [(LOCAL, "window")]))
    before = router._pending_spend
    out = call_job()
    _drain()
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out
    assert router._pending_spend == before


def test_flag_on_capability_filter_keeps_the_rest_of_the_chain(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    from llm_router import capability_filter as cf
    monkeypatch.setattr(cf, "enabled", lambda: True)
    monkeypatch.setattr(cf, "filter_chain_report",
                        lambda models, needs: ([m for m in models if m != LOCAL], [(LOCAL, "window")]))
    call_job()
    _drain()
    assert e2e.state["tried"] and LOCAL not in e2e.state["tried"]


def test_unset_emergency_chain_is_never_walked(e2e):
    """A cloud entry in the BUDGET emergency chain must not be reached when the local model fails."""
    passed(e2e)
    e2e.state["script"][LOCAL] = RuntimeError("down")
    call_job()
    _drain()
    assert all(m == LOCAL for m in e2e.state["tried"])


def test_stream_door_call_never_raises_the_requirement():
    """_drop_failed_eligibility without an exempt reason (the stream door) ignores the pass-first requirement."""
    t2 = er.set_require_passed_first()
    try:
        assert router._drop_failed_eligibility(list(CLOUD), TaskType.GENERATE, Complexity.MODERATE) == CLOUD
    finally:
        er.reset_require_passed_first(t2)


def test_enable_with_an_empty_ollama_inventory_says_ollama_is_down(tmp_path, monkeypatch):
    from llm_router.commands import local_check as LCC
    from tests.test_lc10i_wiring import deps, run
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([], {}))
    d, sink, _ = deps(tmp_path)
    d.inventory = lambda: []
    assert run(["enable", "commit_message", "--model", "some-ollama:1b"], d) == 0
    assert LCC.OLLAMA_DOWN_NOTE in sink.text and LCC.RECONNECT_NOTE not in sink.text
