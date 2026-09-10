"""A wire endpoint must qualify the bare model names its clients actually send.

F-03 made the gateway honour the caller's ``model`` instead of discarding it.
That was right, but it forwarded the value to ``model_override`` verbatim — and
every real client sends a bare, provider-less name, because on its own API the
provider is implied by the endpoint:

    OpenAI     model="gpt-4o"
    Anthropic  model="claude-haiku-4-5"
    Ollama     model="llama3.2"

``model_override`` requires ``provider/model`` and raises ValueError otherwise,
which the gateway maps to HTTP 400. So the fix that started honouring `model`
also started rejecting every request from a normally-configured SDK client.
Found by pointing the real anthropic SDK at a live gateway; no mocked test could
have, because they all passed the already-qualified form.

Each endpoint knows its own provider. That is the whole point of having separate
endpoints, and it is what makes the bare name unambiguous.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm_router.gateway import app


@pytest.fixture
def routed(monkeypatch):
    seen: list[dict] = []

    async def fake(payload: dict) -> dict:
        seen.append(payload)
        return {"text": "ok", "model": "ollama/m", "provider": "ollama",
                "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0,
                "latency_ms": 1.0}

    monkeypatch.setattr("llm_router.route_server.route_payload_async", fake)
    return seen


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("path,body,bare,qualified", [
    ("/v1/chat/completions",
     {"messages": [{"role": "user", "content": "hi"}]}, "gpt-4o", "openai/gpt-4o"),
    ("/v1/responses", {"input": "hi"}, "gpt-4o", "openai/gpt-4o"),
    ("/v1/messages",
     {"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
     "claude-haiku-4-5", "anthropic/claude-haiku-4-5"),
    ("/api/chat",
     {"messages": [{"role": "user", "content": "hi"}]}, "llama3.2", "ollama/llama3.2"),
    ("/api/generate", {"prompt": "hi"}, "llama3.2", "ollama/llama3.2"),
])
def test_bare_model_is_qualified_with_the_endpoint_provider(
    client, routed, path, body, bare, qualified
):
    r = client.post(path, json={**body, "model": bare})
    assert r.status_code == 200, f"{path} rejected a normal client's model name: {r.text}"
    assert routed[0]["model"] == qualified


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/messages", {"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}),
])
def test_already_qualified_names_pass_through_untouched(client, routed, path, body):
    r = client.post(path, json={**body, "model": "ollama/qwen3-coder:30b"})
    assert r.status_code == 200
    assert routed[0]["model"] == "ollama/qwen3-coder:30b"


@pytest.mark.parametrize("auto", ["auto", "llm_router-auto"])
def test_auto_still_means_let_the_router_pick(client, routed, auto):
    r = client.post("/v1/chat/completions", json={
        "model": auto, "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200
    # route_payload_async maps these to "no override"; qualifying them would
    # turn "pick for me" into a demand for a model called openai/auto.
    assert routed[0]["model"] in (auto, None)
