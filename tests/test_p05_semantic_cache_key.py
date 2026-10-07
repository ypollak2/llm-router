"""P0.5 (R-CTX-7): the semantic cache stores and checks under ONE key, the key
includes the conversation context, and both hit-rate queries return true counts.

Bugs this file keeps closed (docs/BUGS.md, "Semantic cache never hits"):

1. ``route_and_call`` checked the cache with the user's raw prompt but
   ``_finalize_successful_route`` stored it under the prompt AFTER OKF /
   ``<repo_state>`` injection. The two texts differ, so the embeddings and the
   numeric discriminator differ, and a stored answer could never be found again.
2. The key had no context: "yes, do it" answered in conversation A was served
   verbatim in conversation B.
3. Without Ollama the cache did nothing at all (no exact-text fallback).
4. ``cost.get_cache_hit_stats`` and the session-end hook queried columns that do
   not exist (``was_hit``/``accessed_at``, ``cache_hit``/``timestamp``), so both
   always returned zeros or ``{}`` and the hit rate was unmeasurable.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import LLMResponse, RoutingProfile, TaskType

_HOOK = Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks" / "session-end.py"


def _fake_embedding(text: str, base_url: str = "") -> list[float]:
    """Deterministic stand-in for nomic-embed-text: same text -> same vector,
    different text -> an unrelated vector. No Ollama is contacted."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(b - 127.5) / 127.5 for b in digest]


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake-ollama.invalid:11434")
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", f"p05-{tmp_path.name}")
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_CACHE", raising=False)
    import llm_router.config as cfg_mod

    cfg_mod._config = None
    from llm_router.config import get_config

    cfg = get_config()
    if str(cfg.llm_router_db_path) != str(db):
        object.__setattr__(cfg, "llm_router_db_path", db)
    from llm_router.context import _reset_session_buffers_for_test

    _reset_session_buffers_for_test()
    yield db
    _reset_session_buffers_for_test()
    cfg_mod._config = None


def _injected(prompt: str, **_kw) -> str:
    # What OKF/<repo_state> injection does to the prompt before dispatch: the
    # block carries numbers (dirty-file count), as the real one does.
    return f"<repo_state>branch main, 3 dirty files</repo_state>\n{prompt}"


async def _route(prompt: str, *, caller_context: str | None, calls: list, inject=_injected,
                 system_prompt: str | None = None):
    import tests.test_tq007_daily_cap_downgrade as t
    from llm_router import router

    def fake_call_llm(model, messages, **kw):
        calls.append(model)
        return LLMResponse(content=f"answer #{len(calls)}", model=model, input_tokens=7,
                           output_tokens=3, cost_usd=0.002, latency_ms=5.0,
                           provider=model.split("/", 1)[0])

    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()
    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "off"}))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
                return_value=["openai/gpt-4o"]))
        p(patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=fake_call_llm))
        p(patch("llm_router.quality_feedback.should_skip_model", return_value=False))
        p(patch("llm_router.context_injection.inject", side_effect=inject))
        p(patch("llm_router.semantic_cache._get_embedding", side_effect=_fake_embedding))
        resp = await router.route_and_call(
            TaskType.QUERY, prompt, profile=RoutingProfile.BALANCED,
            caller_context=caller_context, system_prompt=system_prompt,
        )
        await router.drain_bg_tasks(3.0)
    return resp


def _rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT hit_count FROM semantic_cache ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_same_request_hits_after_context_injection(cache_env):
    """Store and check use the same key: the raw pre-injection prompt."""
    calls: list = []
    first = await _route("explain the retry policy", caller_context="ctx A", calls=calls)
    second = await _route("explain the retry policy", caller_context="ctx A", calls=calls)

    assert first.content == "answer #1"
    assert len(calls) == 1, f"second identical request reached a provider: {calls}"
    assert second.cache_hit is True and second.content == "answer #1"
    assert _rows(cache_env) == [(1,)], "hit_count must be bumped exactly once"


@pytest.mark.asyncio
async def test_context_is_part_of_the_key(cache_env):
    """'yes, do it' under context A must not be served under context B."""
    calls: list = []
    no_inject = lambda prompt, **_kw: prompt  # noqa: E731 - same text at store and check
    await _route("yes, do it", caller_context="ctx A: delete the temp files", calls=calls,
                 inject=no_inject)
    other = await _route("yes, do it", caller_context="ctx B: push to production", calls=calls,
                         inject=no_inject)
    assert len(calls) == 2, "a reply-shaped prompt was answered from another conversation"
    assert other.cache_hit is False

    again = await _route("yes, do it", caller_context="ctx A: delete the temp files", calls=calls,
                         inject=no_inject)
    assert len(calls) == 2 and again.cache_hit is True, "same text + same context must hit"


def test_key_uses_last_two_conversation_messages_when_no_caller_context(cache_env):
    from llm_router import router
    from llm_router.context import _resolve_context_identity, get_session_buffer

    pid, sid = _resolve_context_identity(None, None)
    buf = get_session_buffer(pid, sid)
    buf.record("user", "old turn")
    buf.record("user", "delete the temp files?")
    buf.record("assistant", "Shall I delete them?")
    k1 = router._semantic_cache_key("yes, do it", None, None)

    buf.record("user", "push to production?")
    buf.record("assistant", "Shall I push?")
    k2 = router._semantic_cache_key("yes, do it", None, None)

    assert k1.text == k2.text == "yes, do it"
    assert k1.ctx_hash != k2.ctx_hash
    # Only the last two messages count: an older turn does not change the key.
    buf2 = get_session_buffer(pid + "-other", sid)
    buf2.record("user", "something unrelated")
    buf2.record("user", "push to production?")
    buf2.record("assistant", "Shall I push?")
    from llm_router.semantic_cache import make_key

    assert make_key("yes, do it", context_text=router._recent_context_text(buf2)).ctx_hash == \
        make_key("yes, do it", context_text=router._recent_context_text(buf)).ctx_hash
    # The second-to-last message counts too: two buffers whose LAST message is
    # the same but whose second-to-last differs give different keys (a key
    # built from get_recent(1) would collide here).
    buf3 = get_session_buffer(pid + "-third", sid)
    buf3.record("user", "delete the temp files?")
    buf3.record("assistant", "Shall I go ahead?")
    buf4 = get_session_buffer(pid + "-fourth", sid)
    buf4.record("user", "push to production?")
    buf4.record("assistant", "Shall I go ahead?")
    assert make_key("yes, do it", context_text=router._recent_context_text(buf3)).ctx_hash != \
        make_key("yes, do it", context_text=router._recent_context_text(buf4)).ctx_hash
    # An explicit caller context wins over the buffer.
    assert router._semantic_cache_key("yes, do it", "ctx A", None).ctx_hash == \
        router._semantic_cache_key("yes, do it", "ctx A", None).ctx_hash != k2.ctx_hash


@pytest.mark.asyncio
async def test_caller_system_prompt_is_part_of_the_key(cache_env):
    """Same prompt and context under a different caller system prompt must miss."""
    calls: list = []
    await _route("summarise the diff", caller_context="ctx A", calls=calls,
                 system_prompt="Answer in one line.")
    other = await _route("summarise the diff", caller_context="ctx A", calls=calls,
                         system_prompt="Answer as a numbered list.")
    assert len(calls) == 2 and other.cache_hit is False, \
        "an answer given under one system prompt was served under another"
    again = await _route("summarise the diff", caller_context="ctx A", calls=calls,
                         system_prompt="Answer in one line.")
    assert len(calls) == 2 and again.cache_hit is True, "same system prompt must still hit"


@pytest.mark.asyncio
async def test_old_lookups_are_purged_even_when_no_cache_row_expired(cache_env, monkeypatch):
    """The lookups log follows LLM_ROUTER_PERSIST_TTL_DAYS on its own clock."""
    import time as _time

    from llm_router import semantic_cache

    monkeypatch.setenv("LLM_ROUTER_PERSIST_TTL_DAYS", "30")
    resp = LLMResponse(content="stored", model="openai/gpt-4o", input_tokens=1, output_tokens=1,
                       cost_usd=0.001, latency_ms=1.0, provider="openai")
    k = semantic_cache.make_key("first", context_text="c")
    with patch.object(semantic_cache, "_get_embedding", side_effect=_fake_embedding):
        assert await semantic_cache.check("", TaskType.QUERY, key=k) is None  # creates a lookup
    conn = sqlite3.connect(str(cache_env))
    try:
        conn.execute("UPDATE semantic_cache_lookups SET ts = ?", (_time.time() - 31 * 86_400,))
        conn.commit()
    finally:
        conn.close()
    with patch.object(semantic_cache, "_get_embedding", side_effect=_fake_embedding):
        await semantic_cache.store("", TaskType.QUERY, resp, key=k)  # fresh row: nothing expires
    conn = sqlite3.connect(str(cache_env))
    try:
        (n_lookups,) = conn.execute("SELECT COUNT(*) FROM semantic_cache_lookups").fetchone()
        (n_rows,) = conn.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()
    finally:
        conn.close()
    assert (n_lookups, n_rows) == (0, 1)


@pytest.mark.asyncio
async def test_exact_hash_fallback_without_ollama(cache_env, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    import llm_router.config as cfg_mod

    cfg_mod._config = None
    from llm_router.config import get_config

    object.__setattr__(get_config(), "llm_router_db_path", cache_env)
    from llm_router import semantic_cache

    embed = MagicMock(side_effect=_fake_embedding)
    resp = LLMResponse(content="stored", model="openai/gpt-4o", input_tokens=1, output_tokens=1,
                       cost_usd=0.003, latency_ms=1.0, provider="openai")
    key_a = semantic_cache.make_key("list  the files", context_text="ctx A")
    with patch.object(semantic_cache, "_get_embedding", embed):
        await semantic_cache.store("ignored-raw", TaskType.QUERY, resp, key=key_a)
        hit = await semantic_cache.check("ignored-raw", TaskType.QUERY,
                                         key=semantic_cache.make_key("list the files ",
                                                                     context_text="ctx A"))
        miss = await semantic_cache.check("x", TaskType.QUERY,
                                          key=semantic_cache.make_key("list the files",
                                                                      context_text="ctx B"))
    embed.assert_not_called()
    assert hit is not None and hit.content == "stored" and hit.cache_hit
    assert miss is None
    conn = sqlite3.connect(str(cache_env))
    try:
        emb, hits = conn.execute("SELECT embedding, hit_count FROM semantic_cache").fetchone()
    finally:
        conn.close()
    assert emb == "" and hits == 1


async def _three_lookups_one_hit(db: Path) -> None:
    from llm_router import semantic_cache

    resp = LLMResponse(content="stored", model="openai/gpt-4o", input_tokens=1, output_tokens=1,
                       cost_usd=0.004, latency_ms=1.0, provider="openai")
    with patch.object(semantic_cache, "_get_embedding", side_effect=_fake_embedding):
        k = semantic_cache.make_key("what is the retry policy", context_text="c")
        assert await semantic_cache.check("", TaskType.QUERY, key=k) is None       # lookup 1: miss
        await semantic_cache.store("", TaskType.QUERY, resp, key=k)
        assert await semantic_cache.check("", TaskType.QUERY, key=k) is not None   # lookup 2: hit
        other = semantic_cache.make_key("what is the timeout", context_text="c")
        assert await semantic_cache.check("", TaskType.QUERY, key=other) is None   # lookup 3: miss


@pytest.mark.asyncio
async def test_cost_cache_hit_stats_returns_true_counts_with_n(cache_env):
    from llm_router import cost

    empty = await cost.get_cache_hit_stats("all")
    assert empty["n"] == 0 and empty["lookups"] == 0 and empty["hits"] == 0

    await _three_lookups_one_hit(cache_env)
    stats = await cost.get_cache_hit_stats("today")
    assert (stats["hits"], stats["lookups"], stats["n"]) == (1, 3, 3), stats
    assert stats["hit_rate_pct"] == pytest.approx(100 / 3)
    assert stats["estimated_saved_usd"] == pytest.approx(0.004)


@pytest.mark.asyncio
async def test_session_end_cache_hit_stats_returns_true_counts_with_n(cache_env):
    await _three_lookups_one_hit(cache_env)
    spec = importlib.util.spec_from_file_location("session_end_p05", _HOOK)
    se = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(se)
    assert se._db_path() == str(cache_env)
    stats = se._query_cache_hit_stats()
    assert (stats["hits"], stats["lookups"], stats["n"]) == (1, 3, 3), stats
    assert stats["hit_rate_pct"] == pytest.approx(100 / 3)
