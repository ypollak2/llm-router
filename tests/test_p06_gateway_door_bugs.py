"""P0.6 (R-AGT-1, R-AGT-3): gateway door bugs.

Three defects at da31df7, each reproduced here against the real gateway app
with only ``route_and_call`` faked:

1. "Auto", "AUTO" and "llm-router-auto" were not treated as "let the router
   pick". The gateway matched sentinels case-insensitively, but
   ``route_server`` compared ``model`` to ``("auto", "llm_router-auto")``
   exactly, so these names reached ``route_and_call`` as a literal
   ``model_override`` and came back as HTTP 400.
2. ``stream: true`` was silently dropped (the field was undeclared), so a
   streaming OpenAI/Anthropic client got one JSON body where it expected SSE.
3. ``max_tokens``, ``temperature`` and the system prompt never reached
   ``route_payload_async``: the first two were dropped, the third was folded
   into the user text as a ``system:`` line.
"""

from __future__ import annotations

import json

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


class _Resp:
    content = "ok"
    model = "ollama/qwen2.5-coder:7b"
    provider = "ollama"
    cost_usd = 0.0
    input_tokens = 3
    output_tokens = 1
    complexity = "simple"
    finish_reason = None


@pytest.fixture()
def router_calls(monkeypatch, tmp_path):
    """Fake ``route_and_call`` that keeps the real override validation.

    ``_build_and_filter_chain`` raises ValueError for an override with no
    ``/`` (router.py, "Invalid model_override format"); the fake does the same
    so a sentinel that leaks through as an override fails exactly as live.
    """
    import llm_router.router as R
    import llm_router.route_server as rs

    calls: list[dict] = []

    async def _fake(task_type, prompt, **kw):
        from llm_router import semantic_cache

        override = kw.get("model_override")
        if override and "/" not in override and override not in {"codex", "ollama", "gemini_cli"}:
            raise ValueError(f"Invalid model_override format: {override!r}")
        calls.append({"task_type": task_type, "prompt": prompt,
                      "cache_disabled": semantic_cache._cache_disabled(), **kw})
        return _Resp()

    monkeypatch.setattr(R, "route_and_call", _fake)
    monkeypatch.setattr(rs, "_log_route_savings", lambda *a, **k: None)
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_CACHE", raising=False)
    return calls


@pytest.fixture()
def client():
    from llm_router.gateway import app

    return fastapi_testclient.TestClient(app, base_url="http://127.0.0.1",
                                         raise_server_exceptions=False)


def _body(path: str, model: str | None = "x", **extra) -> dict:
    msgs = [{"role": "user", "content": "hi"}]
    body = {
        "/v1/chat/completions": {"messages": msgs},
        "/v1/messages": {"messages": msgs, "max_tokens": 16},
        "/v1/responses": {"input": "hi"},
        "/api/chat": {"messages": msgs},
        "/api/generate": {"prompt": "hi"},
        "/route": {"prompt": "hi"},
    }[path]
    if model is not None:
        body["model"] = model
    body.update(extra)
    return body


# ── 1. sentinels ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["auto", "Auto", "AUTO", " auto ", "llm-router-auto",
                                  "LLM-Router-Auto", "llm_router-auto", "LLM_ROUTER-AUTO"])
def test_is_auto_model_is_case_insensitive(name):
    from llm_router.route_server import is_auto_model

    assert is_auto_model(name) is True


@pytest.mark.parametrize("name", ["gpt-x", "openai/auto", "autox", "", None, 3])
def test_is_auto_model_rejects_real_names(name):
    from llm_router.route_server import is_auto_model

    assert is_auto_model(name) is False


ALL_DOORS = ["/v1/chat/completions", "/v1/messages", "/v1/responses",
             "/api/chat", "/api/generate", "/route"]


@pytest.mark.parametrize("path", ALL_DOORS)
@pytest.mark.parametrize("name", ["Auto", "AUTO", "llm-router-auto"])
def test_sentinel_is_routed_on_every_door(client, router_calls, path, name):
    r = client.post(path, json=_body(path, model=name))
    assert r.status_code == 200, r.text
    assert len(router_calls) == 1
    assert router_calls[0]["model_override"] is None


@pytest.mark.parametrize("path,expected", [
    ("/v1/chat/completions", "openai/gpt-x"),
    ("/v1/responses", "openai/gpt-x"),
    ("/v1/messages", "anthropic/gpt-x"),
    ("/api/chat", "ollama/gpt-x"),
    ("/route", None),  # native /route takes provider/model; a bare name is 400
])
def test_real_model_name_stays_pinned(client, router_calls, path, expected):
    r = client.post(path, json=_body(path, model="gpt-x"))
    if expected is None:
        assert r.status_code == 400
        assert router_calls == []
    else:
        assert r.status_code == 200, r.text
        assert router_calls[0]["model_override"] == expected


def test_gateway_and_route_server_share_one_sentinel_helper():
    from llm_router import gateway, route_server

    assert gateway.is_auto_model is route_server.is_auto_model
    assert not hasattr(gateway, "_AUTO_SENTINELS")


# ── 2. stream:true is refused, never dropped ─────────────────────────────────

#: Endpoints whose clients expect SSE when they send ``stream: true``.
SSE_ENDPOINTS = ["/v1/chat/completions", "/v1/responses", "/v1/messages"]

#: Ollama streams NDJSON: one JSON object per line, the last with
#: ``done: true``. The gateway's single ``done: true`` object IS a valid
#: one-chunk NDJSON stream, so ``stream: true`` is served, not dropped (and
#: Ollama clients default to streaming, so a 400 would break working callers).
NDJSON_ENDPOINTS = ["/api/chat", "/api/generate"]

#: POST routes that do not take a chat/completion request.
NOT_COMPLETIONS = ["/route", "/ground"]


def test_every_post_route_is_classified_for_stream():
    """A completion endpoint added later must be put in one of the lists."""
    from llm_router.gateway import app

    posts = sorted({r.path for r in app.routes if "POST" in (getattr(r, "methods", None) or set())})
    assert posts, "no POST routes discovered"
    unclassified = set(posts) - set(SSE_ENDPOINTS) - set(NDJSON_ENDPOINTS) - set(NOT_COMPLETIONS)
    assert not unclassified, f"classify these for stream handling: {sorted(unclassified)}"


@pytest.mark.parametrize("path", SSE_ENDPOINTS)
def test_stream_true_is_an_explicit_400(client, router_calls, path):
    r = client.post(path, json=_body(path, stream=True))
    assert r.status_code == 400
    assert r.json()["detail"] == "streaming not supported yet (v16 A.2)"
    assert router_calls == [], "a refused request must not be routed"


@pytest.mark.parametrize("path", SSE_ENDPOINTS)
def test_stream_false_still_routes(client, router_calls, path):
    r = client.post(path, json=_body(path, stream=False))
    assert r.status_code == 200, r.text
    assert len(router_calls) == 1


@pytest.mark.parametrize("path", NDJSON_ENDPOINTS)
def test_ollama_stream_true_is_a_valid_one_chunk_ndjson_stream(client, router_calls, path):
    r = client.post(path, json=_body(path, stream=True))
    assert r.status_code == 200, r.text
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 1
    chunk = json.loads(lines[0])
    assert chunk["done"] is True


# ── 3. max_tokens, temperature, system forwarded ─────────────────────────────

@pytest.fixture()
def payloads(monkeypatch):
    import llm_router.route_server as rs

    seen: list[dict] = []

    async def _fake_payload(payload):
        seen.append(payload)
        return {"text": "ok", "provider": "ollama", "model": "ollama/x",
                "cost_usd": 0.0, "input_tokens": 1, "output_tokens": 1,
                "complexity": "simple"}

    monkeypatch.setattr(rs, "route_payload_async", _fake_payload)
    return seen


SYS = "be terse"


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"messages": [{"role": "system", "content": SYS},
                                           {"role": "user", "content": "hi"}],
                              "max_tokens": 64, "temperature": 0.3}),
    ("/v1/chat/completions", {"messages": [{"role": "developer", "content": SYS},
                                           {"role": "user", "content": "hi"}],
                              "max_completion_tokens": 64, "temperature": 0.3}),
    ("/v1/responses", {"instructions": SYS, "input": "hi",
                       "max_output_tokens": 64, "temperature": 0.3}),
    ("/v1/responses", {"input": [{"role": "system", "content": SYS},
                                 {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                       "max_output_tokens": 64, "temperature": 0.3}),
    ("/v1/messages", {"system": SYS, "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 64, "temperature": 0.3}),
    ("/api/chat", {"messages": [{"role": "system", "content": SYS},
                                {"role": "user", "content": "hi"}],
                   "options": {"num_predict": 64, "temperature": 0.3}}),
    ("/api/generate", {"system": SYS, "prompt": "hi",
                       "options": {"num_predict": 64, "temperature": 0.3}}),
])
def test_three_parameters_reach_route_payload(client, payloads, path, body):
    r = client.post(path, json={"model": "auto", **body})
    assert r.status_code == 200, r.text
    assert len(payloads) == 1
    p = payloads[0]
    assert p["max_tokens"] == 64
    assert p["temperature"] == 0.3
    assert p["system"] == SYS
    # Sent once, as the system prompt -- not also as a line of user text.
    assert SYS not in p["prompt"]
    assert "hi" in p["prompt"]


def test_absent_parameters_are_none_not_invented(client, payloads):
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    p = payloads[0]
    assert (p["system"], p["max_tokens"], p["temperature"]) == (None, None, None)


def test_system_only_request_is_still_routed(client, payloads):
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "system", "content": SYS}]})
    assert r.status_code == 200
    assert SYS in payloads[0]["prompt"] and payloads[0]["system"] is None


def test_route_payload_hands_the_three_to_route_and_call(client, router_calls):
    r = client.post("/v1/chat/completions", json={
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": "hi"}],
        "max_tokens": 64, "temperature": 0.3})
    assert r.status_code == 200, r.text
    kw = router_calls[0]
    assert (kw["system_prompt"], kw["max_tokens"], kw["temperature"]) == (SYS, 64, 0.3)


# ── guard: a forwarded system prompt must not share semantic-cache entries ───

def test_semantic_cache_is_bypassed_while_a_caller_system_prompt_is_set(client, router_calls):
    """The cache keys on prompt + task type only. With the system prompt moved
    out of the prompt, two callers with different system prompts asking "hi"
    would share one cached answer. Until the key carries it (P0.5), such a
    call neither reads nor writes the cache; calls without one are untouched,
    and the bypass does not leak past the call."""
    from llm_router import semantic_cache

    client.post("/v1/chat/completions", json={
        "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": "hi"}]})
    client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}]})
    assert [c["cache_disabled"] for c in router_calls] == [True, False]
    assert semantic_cache._cache_disabled() is False


@pytest.mark.asyncio
async def test_cache_check_and_store_do_not_embed_under_the_bypass(monkeypatch):
    from llm_router import semantic_cache
    from llm_router.types import TaskType

    embedded: list[str] = []
    monkeypatch.delenv("LLM_ROUTER_SEMANTIC_CACHE", raising=False)
    monkeypatch.setattr(semantic_cache, "_get_embedding",
                        lambda text, url: embedded.append(text) or None)

    class _Cfg:
        ollama_base_url = "http://127.0.0.1:1"

    monkeypatch.setattr("llm_router.config.get_config", lambda: _Cfg())
    token = semantic_cache.CALLER_SYSTEM_PROMPT.set(True)
    try:
        assert await semantic_cache.check("hi", TaskType.QUERY) is None
        await semantic_cache.store("hi", TaskType.QUERY, _Resp())
    finally:
        semantic_cache.CALLER_SYSTEM_PROMPT.reset(token)
    assert embedded == []
    # Mutation guard: without the bypass the same call does embed.
    assert await semantic_cache.check("hi", TaskType.QUERY) is None
    assert embedded == ["hi"]
