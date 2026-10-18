"""P3.1-2: door tags at sdk / gateway / mcp / hook / agent, the agent-route PreToolUse
no-call pin, and the F80 reader cases of ``judge_sampled``."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_router import call_identity, counter_registry, judge_sampler
from llm_router.hooks.direct_executor import DirectResult, ModelSpec

_ANSWER = "The capital of France is Paris, a city on the Seine river."


@pytest.fixture
def judged(temp_db, tmp_path, monkeypatch):
    """Judge every eligible call; returns the queue path (LLM_ROUTER_HOME = tmp_path)."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    return tmp_path / "judge_queue.jsonl"


def _doors(queue) -> list[str]:
    assert queue.exists(), "nothing was queued: the door never reached the sampler"
    return [json.loads(x)["door"] for x in queue.read_text().splitlines()]


def _direct_result() -> DirectResult:
    return DirectResult(text=_ANSWER, model=ModelSpec(provider="ollama", model="qwen3.5:latest"),
                        latency_ms=10, input_tokens=10, output_tokens=10)


@pytest.mark.parametrize("classifier_type,door", [
    ("sdk", "sdk"),
    ("heuristic", "hook"),       # main-session DIRECT hook: any classifier method
    ("agent-route", "agent"),
    ("agent-route-cli", "agent"),
])
def test_direct_path_queue_entry_carries_the_door(judged, classifier_type, door):
    from llm_router.hooks.savings_logger import log_direct_to_db

    log_direct_to_db(_direct_result(), prompt="what is the capital of France", task_type="query",
                     complexity="simple", classifier_type=classifier_type)
    assert _doors(judged) == [door]
    assert judge_sampler.counters()[door]["seen"] == 1


def test_sdk_route_tags_its_db_write_sdk(monkeypatch):
    """sdk._route hands log_direct_to_db door="sdk" (the call site, not the mapping)."""
    from llm_router import sdk
    from llm_router.hooks import chain_builder, direct_executor, savings_logger

    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 5))
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(chain_builder, "build_chain", lambda *a, **k: ["m"])
    monkeypatch.setattr(direct_executor, "execute_chain", lambda *a, **k: _direct_result())
    seen = {}
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **k: seen.update(k))
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **k: None)
    sdk.route("what is the capital of France", task_type="query", complexity="simple")
    assert seen["door"] == "sdk"


def test_log_routing_decision_door_lands_in_queue_and_unknown_when_untagged(judged):
    from llm_router import cost

    async def _log(door):
        await cost.log_routing_decision(
            prompt="q " + str(door), task_type="query", profile="balanced",
            classifier_type="heuristic", classifier_model=None, classifier_confidence=None,
            classifier_latency_ms=None, complexity="simple", recommended_model="m", base_model="m",
            was_downshifted=None, budget_pct_used=None, quality_mode=None, final_model="m",
            final_provider="ollama", success=True, input_tokens=1, output_tokens=1, cost_usd=0.0,
            latency_ms=1.0, reason_code="direct", response=_ANSWER, door=door)

    for d in ("gateway", "mcp", None):
        asyncio.run(_log(d))
    assert _doors(judged) == ["gateway", "mcp", "unknown"]


def _finalize(monkeypatch):
    """Run the shared router finalizer with a fake cost module; return its mock."""
    from llm_router import router

    monkeypatch.setattr("llm_router.session_spend.get_session_spend", lambda: MagicMock())
    monkeypatch.setattr("llm_router.routing_quality.record_route", MagicMock())
    monkeypatch.setattr(router, "get_session_buffer", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(router, "_resolve_context_identity", lambda *_a, **_k: ("proj", "sess"))
    monkeypatch.setattr("llm_router.session_store.record_event", MagicMock())
    monkeypatch.setattr("llm_router.semantic_cache.store", AsyncMock())
    fake_cost = SimpleNamespace(
        log_routing_decision=AsyncMock(), log_claude_usage=AsyncMock(),
        log_codex_usage=AsyncMock(), log_gemini_usage=AsyncMock(),
        get_daily_spend=AsyncMock(return_value=0.0), fire_budget_alert=MagicMock())
    monkeypatch.setattr(router, "cost", fake_cost)
    resp = SimpleNamespace(
        input_tokens=1, output_tokens=1, cost_usd=0.0, model="ollama/m", provider="ollama",
        content=_ANSWER, latency_ms=1.0, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    cfg = SimpleNamespace(codex_daily_limit=100, llm_router_daily_spend_limit=0.0)
    asyncio.run(router._finalize_successful_route(
        response=resp, model="ollama/m", provider="ollama", task_type=router.TaskType.QUERY,
        profile=router.RoutingProfile.BALANCED, prompt="q",
        classification_data={"task_type": "query", "complexity": "simple"},
        chain_attempts=["ollama/m"], chain_errors=[], correlation_id="c",
        failed_attempt_cost=0.0, config=cfg, receipt=None, suppress_ledger=False))
    return fake_cost.log_routing_decision


def test_finalizer_door_is_mcp_inside_an_mcp_call(monkeypatch):
    flag = call_identity._IN_MCP_CALL.set(True)
    try:
        log = _finalize(monkeypatch)
    finally:
        call_identity._IN_MCP_CALL.reset(flag)
    assert log.call_args.kwargs["door"] == "mcp"


def test_finalizer_door_is_the_declared_door_and_none_otherwise(monkeypatch):
    with call_identity.door_scope("gateway"):
        assert _finalize(monkeypatch).call_args.kwargs["door"] == "gateway"
    assert call_identity.current_door() is None
    assert _finalize(monkeypatch).call_args.kwargs["door"] is None


def test_route_server_declares_the_gateway_door(monkeypatch):
    from llm_router import route_server

    seen = {}

    async def fake_route_and_call(*a, **k):
        seen["door"] = call_identity.current_door()
        return SimpleNamespace(content="x", model="m", provider="p", cost_usd=0.0, input_tokens=1,
                               output_tokens=1, complexity="simple", finish_reason="", tool_calls=[])

    monkeypatch.setattr("llm_router.router.route_and_call", fake_route_and_call)
    monkeypatch.setattr(route_server, "_log_route_savings", lambda *a, **k: None)
    asyncio.run(route_server.route_payload_async({"prompt": "hi"}))
    assert seen["door"] == "gateway"
    assert call_identity.current_door() is None  # scope closed


def test_agent_route_pretooluse_never_samples_or_writes(tmp_path):
    """The non-DIRECT agent-route path (PreToolUse approve/block) is not a door: it must
    not pick, count or queue anything even with every call sampled."""
    from tests.test_agent_route_hook import _load_hook_module, _run

    code, _out = _run("implement the foo module", tmp_path=tmp_path, subagent_direct=False,
                      extra_env={"LLM_ROUTER_JUDGE_SAMPLE_RATE": "1.0"})
    state = tmp_path / ".llm-router"
    assert code == 0
    assert any(state.iterdir()), "the hook wrote nothing at all: it did not run"
    assert not (state / "judge_sampler.json").exists()
    assert not (state / "judge_queue.jsonl").exists()


def test_agent_route_direct_gate_off_never_calls_the_sampler(monkeypatch):
    from tests.test_agent_route_hook import _load_hook_module

    def boom(*a, **k):
        raise AssertionError("sampler called from the non-DIRECT path")

    monkeypatch.setattr(judge_sampler, "pick", boom)
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "off")
    assert _load_hook_module()._try_direct_subagent("implement foo", "code", "simple", "s1") is None


# ── F80: judge_sampled reader ────────────────────────────────────────────────

def _reading(monkeypatch, state):
    monkeypatch.setattr(judge_sampler, "counters", lambda: state)
    return counter_registry._read_judge_sampler()


def test_f80_skip_only_door_alarms_not_unknown(monkeypatch):
    r = _reading(monkeypatch, {"hook": {"lock_skipped": 25}})
    assert r.value == 0.0 and r.alarming


def test_f80_skip_share_needs_a_minimum_n(monkeypatch):
    small = _reading(monkeypatch, {"hook": {"seen": 8, "picked": 1, "lock_skipped": 1}})
    assert small.value == 1.0 and not small.alarming          # 1 skip in 9: noise
    big = _reading(monkeypatch, {"hook": {"seen": 18, "picked": 2, "lock_skipped": 5}})
    assert big.alarming                                       # 5 of 23


def test_f80_nothing_seen_or_skipped_is_unknown(monkeypatch):
    r = _reading(monkeypatch, {"hook": {"seen": 0, "picked": 0}})
    assert r.value is None and r.unknown_reason


def test_f80_bool_is_not_a_count(monkeypatch):
    r = _reading(monkeypatch, {"sdk": {"seen": True, "picked": True, "lock_skipped": True}})
    assert r.value is None
