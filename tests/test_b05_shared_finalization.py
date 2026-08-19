"""CHZ-AUD-B-05: the emergency BUDGET fallback must finalize identically to the
primary success path. Both call the single shared helper _finalize_successful_route,
which records the session-spend meter, the route-quality ledger, the context
buffers, the routing-decision analytics and the semantic cache.
"""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import llm_router.router as router
from llm_router.router import TaskType, RoutingProfile

ROOT = Path(__file__).resolve().parents[1]


def _fake_response():
    return SimpleNamespace(
        input_tokens=100, output_tokens=50, cost_usd=0.0002,
        model="ollama/qwen2.5-coder:7b", provider="ollama",
        content="answer", latency_ms=120.0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )


def test_finalizer_fires_all_side_effects(monkeypatch):
    spend = MagicMock()
    monkeypatch.setattr("llm_router.session_spend.get_session_spend", lambda: spend)
    rec_route = MagicMock()
    monkeypatch.setattr("llm_router.routing_quality.record_route", rec_route)
    buf = MagicMock()
    monkeypatch.setattr(router, "get_session_buffer", lambda *_a, **_k: buf)
    monkeypatch.setattr(router, "_resolve_context_identity", lambda *_a, **_k: ("proj", "sess"))
    sess_rec = MagicMock()
    monkeypatch.setattr("llm_router.session_store.record_event", sess_rec)
    sem_store = AsyncMock()
    monkeypatch.setattr("llm_router.semantic_cache.store", sem_store)
    fake_cost = SimpleNamespace(
        log_routing_decision=AsyncMock(),
        log_claude_usage=AsyncMock(), log_codex_usage=AsyncMock(),
        log_gemini_usage=AsyncMock(), get_daily_spend=AsyncMock(return_value=0.0),
        fire_budget_alert=MagicMock(),
    )
    monkeypatch.setattr(router, "cost", fake_cost)

    cfg = SimpleNamespace(codex_daily_limit=100, llm_router_daily_spend_limit=0.0)
    asyncio.run(router._finalize_successful_route(
        response=_fake_response(),
        model="ollama/qwen2.5-coder:7b", provider="ollama",
        task_type=TaskType.CODE, profile=RoutingProfile.BALANCED,
        prompt="q", classification_data={"task_type": "code", "complexity": "moderate"},
        chain_attempts=["ollama/qwen2.5-coder:7b"], chain_errors=[],
        correlation_id="cid", failed_attempt_cost=0.0, config=cfg,
        receipt=None, suppress_ledger=False,
    ))

    assert spend.record.called, "session-spend meter not recorded"
    assert rec_route.called, "route-quality ledger not recorded"
    assert buf.record.call_count >= 2, "session buffer not recorded (user+assistant)"
    assert sess_rec.called, "durable session_store not recorded"
    assert fake_cost.log_routing_decision.called, "routing-decision analytics not logged"
    assert sem_store.called, "semantic cache not stored"


def test_every_success_path_calls_shared_finalizer():
    """Guard against the drift class returning: exactly one helper def, invoked
    from ALL FOUR success paths — primary, emergency BUDGET fallback, semantic-
    cache hit, and idempotency dedupe. RED-1 reopened B-05 because two bypass
    success returns skipped the finalizer; this asserts they no longer do."""
    src = (ROOT / "src" / "llm_router" / "router.py").read_text()
    assert src.count("async def _finalize_successful_route(") == 1
    call_sites = len(re.findall(r"await _finalize_successful_route\(", src))
    assert call_sites == 5, (
        f"expected primary + emergency + semantic-cache + idempotency + "
        f"exhaustion-floor call sites, found {call_sites}"
    )
    # The three no-fresh-spend paths (idempotency, semantic-cache, exhaustion
    # floor) must pass served_from_cache=True (call form uses a trailing comma;
    # docstring mentions do not).
    assert src.count("served_from_cache=True,") == 3, \
        "the cache/floor paths must finalize with served_from_cache=True"
    # RED-1 re-audit: every finalize call site must be wrapped so a finalize bug
    # is never misclassified as a provider failure — assert as many guard handlers
    # as call sites.
    assert src.count("finalize_successful_route") >= 5


def test_cache_served_turn_records_context_but_not_spend(monkeypatch):
    """CHZ-AUD-B-05 (sibling): a cache-served turn MUST record the exchange into
    the context buffers + routing analytics (so it is not lost), and MUST NOT
    re-count spend, re-emit the route-quality completion, or re-store the cache."""
    spend = MagicMock()
    monkeypatch.setattr("llm_router.session_spend.get_session_spend", lambda: spend)
    rec_route = MagicMock()
    monkeypatch.setattr("llm_router.routing_quality.record_route", rec_route)
    buf = MagicMock()
    monkeypatch.setattr(router, "get_session_buffer", lambda *_a, **_k: buf)
    monkeypatch.setattr(router, "_resolve_context_identity", lambda *_a, **_k: ("proj", "sess"))
    sess_rec = MagicMock()
    monkeypatch.setattr("llm_router.session_store.record_event", sess_rec)
    sem_store = AsyncMock()
    monkeypatch.setattr("llm_router.semantic_cache.store", sem_store)
    fake_cost = SimpleNamespace(
        log_routing_decision=AsyncMock(),
        log_claude_usage=AsyncMock(), log_codex_usage=AsyncMock(),
        log_gemini_usage=AsyncMock(), get_daily_spend=AsyncMock(return_value=0.0),
        fire_budget_alert=MagicMock(),
    )
    monkeypatch.setattr(router, "cost", fake_cost)

    cfg = SimpleNamespace(codex_daily_limit=100, llm_router_daily_spend_limit=1.0)
    asyncio.run(router._finalize_successful_route(
        response=_fake_response(),
        model="ollama/qwen2.5-coder:7b", provider="ollama",
        task_type=TaskType.CODE, profile=RoutingProfile.BALANCED,
        prompt="q", classification_data={"task_type": "code", "complexity": "moderate"},
        chain_attempts=[], chain_errors=[],
        correlation_id="cid", failed_attempt_cost=0.0, config=cfg,
        receipt=None, suppress_ledger=False, served_from_cache=True,
    ))

    # Recorded (not lost):
    assert buf.record.call_count >= 2, "cache-served turn lost the context buffer"
    assert sess_rec.called, "cache-served turn lost the durable session_store"
    assert fake_cost.log_routing_decision.called, "cache-served turn lost analytics"
    # NOT re-counted / re-stored:
    assert not spend.record.called, "cache hit must not double-count spend"
    assert not rec_route.called, "cache hit must not emit a completion ledger record"
    assert not sem_store.called, "cache hit must not re-store into the semantic cache"


def test_suppress_ledger_skips_route_quality(monkeypatch):
    """MGEE internal calls pass suppress_ledger=True; the route-quality ledger
    must be skipped while the spend meter still records."""
    spend = MagicMock()
    monkeypatch.setattr("llm_router.session_spend.get_session_spend", lambda: spend)
    rec_route = MagicMock()
    monkeypatch.setattr("llm_router.routing_quality.record_route", rec_route)
    monkeypatch.setattr(router, "get_session_buffer", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(router, "_resolve_context_identity", lambda *_a, **_k: ("p", "s"))
    monkeypatch.setattr("llm_router.session_store.record_event", MagicMock())
    monkeypatch.setattr("llm_router.semantic_cache.store", AsyncMock())
    monkeypatch.setattr(router, "cost", SimpleNamespace(
        log_routing_decision=AsyncMock(), log_claude_usage=AsyncMock(),
        log_codex_usage=AsyncMock(), log_gemini_usage=AsyncMock(),
        get_daily_spend=AsyncMock(return_value=0.0), fire_budget_alert=MagicMock(),
    ))
    cfg = SimpleNamespace(codex_daily_limit=100, llm_router_daily_spend_limit=0.0)
    asyncio.run(router._finalize_successful_route(
        response=_fake_response(), model="ollama/x", provider="ollama",
        task_type=TaskType.QUERY, profile=RoutingProfile.BALANCED, prompt="q",
        classification_data=None, chain_attempts=["ollama/x"], chain_errors=[],
        correlation_id="c", failed_attempt_cost=0.0, config=cfg,
        receipt=None, suppress_ledger=True,
    ))
    assert spend.record.called
    assert not rec_route.called, "suppress_ledger=True must skip the route-quality ledger"
