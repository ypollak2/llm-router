"""Gateway door: every completion endpoint builds context via build_pack(door="gateway").

PLAN v16 P1.1 door 7. The endpoints used to flatten the whole message list into
one ``"role: text"`` string. They now send the trailing user turn verbatim as
the request and hand the turns before it to ``route_and_call(messages=...,
context_door="gateway")``, which packs them per provider attempt under the
session privacy rule.
"""

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from llm_router import (
    call_identity,
    context,
    context_pack,
    failopen,
    local_models,
    providers,
    route_server,
    router,
    session_store,
)
from llm_router import gateway as G
from llm_router.types import LLMResponse, TaskType

MARK = "GATEWAY-CONVERSATION-MARKER"
SYS = "be terse"
HISTORY = [
    {"role": "user", "content": MARK + " q"},
    {"role": "assistant", "content": MARK + " a"},
]
REQUEST = {"role": "user", "content": "REQ-TEXT"}


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("LLM_ROUTER_") or key in (
            "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR",
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")
    monkeypatch.setattr(call_identity, "call_session_id", lambda: None)


@pytest.fixture
def client():
    return TestClient(G.app, base_url="http://127.0.0.1")


@pytest.fixture
def payloads(monkeypatch):
    """The payload each endpoint hands route_payload_async; the call is stubbed."""
    seen = []

    async def fake(payload):
        seen.append(dict(payload))
        return {"text": "ok", "provider": "ollama", "model": "x",
                "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}

    monkeypatch.setattr(route_server, "route_payload_async", fake)
    return seen


# ── each endpoint separates the request from the history ─────────────────────

@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"messages": [{"role": "system", "content": SYS},
                                           *HISTORY, REQUEST]}),
    ("/v1/messages", {"system": SYS, "messages": [*HISTORY, REQUEST], "max_tokens": 64}),
    ("/api/chat", {"messages": [{"role": "system", "content": SYS}, *HISTORY, REQUEST]}),
    ("/v1/responses", {"instructions": SYS, "input": [*HISTORY, REQUEST]}),
])
def test_endpoint_sends_request_verbatim_and_history_as_messages(client, payloads, path, body):
    r = client.post(path, json={"model": "auto", **body})
    assert r.status_code == 200, r.text
    (p,) = payloads
    assert p["prompt"] == "REQ-TEXT"
    assert p["messages"] == HISTORY
    assert p["system"] == SYS


def test_content_parts_request_is_its_text(client, payloads):
    r = client.post("/v1/responses", json={"input": [
        *HISTORY,
        {"role": "user", "content": [{"type": "input_text", "text": "REQ-TEXT"}]},
    ]})
    assert r.status_code == 200, r.text
    assert payloads[0]["prompt"] == "REQ-TEXT"
    assert payloads[0]["messages"] == HISTORY


@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"messages": [REQUEST]}),
    ("/v1/responses", {"input": "REQ-TEXT"}),
    ("/api/generate", {"prompt": "REQ-TEXT"}),
])
def test_single_turn_requests_carry_an_empty_history(client, payloads, path, body):
    """An empty list, not None: the gateway door never falls back to the MCP
    path, which would read this process's own session into the call."""
    r = client.post(path, json=body)
    assert r.status_code == 200, r.text
    assert payloads[0]["prompt"] == "REQ-TEXT"
    assert payloads[0]["messages"] == []


def test_assistant_prefill_keeps_the_flattened_prompt(client, payloads):
    """No trailing user turn: nothing the caller sent is dropped."""
    r = client.post("/v1/messages", json={
        "messages": [REQUEST, {"role": "assistant", "content": "PREFILL"}],
        "max_tokens": 64,
    })
    assert r.status_code == 200, r.text
    assert payloads[0]["prompt"] == "user: REQ-TEXT\nassistant: PREFILL"
    assert payloads[0]["messages"] == []


def test_native_route_endpoint_still_sends_no_history(client, payloads):
    r = client.post("/route", json={"prompt": "REQ-TEXT"})
    assert r.status_code == 200, r.text
    assert payloads[0].get("messages") is None


# ── route_payload_async → route_and_call ─────────────────────────────────────

def _fake_route_and_call(monkeypatch):
    got = {}

    async def fake(task_type, prompt, **kwargs):
        got.update(prompt=prompt, **kwargs)
        return LLMResponse(content="ok", model="ollama/x", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="ollama")

    monkeypatch.setattr(router, "route_and_call", fake)
    monkeypatch.setattr(route_server, "_log_route_savings", lambda *a, **k: None)
    return got


@pytest.mark.asyncio
async def test_route_payload_threads_messages_with_the_gateway_door(monkeypatch):
    got = _fake_route_and_call(monkeypatch)
    await route_server.route_payload_async({"prompt": "REQ-TEXT", "messages": HISTORY})
    assert (got["prompt"], got["messages"], got["context_door"]) == (
        "REQ-TEXT", HISTORY, "gateway",
    )


@pytest.mark.asyncio
async def test_route_payload_without_messages_passes_none(monkeypatch):
    got = _fake_route_and_call(monkeypatch)
    await route_server.route_payload_async({"prompt": "REQ-TEXT"})
    assert got["messages"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not a list", [["user", "hi"]], [None]])
async def test_route_payload_rejects_malformed_messages(monkeypatch, bad):
    _fake_route_and_call(monkeypatch)
    with pytest.raises(ValueError):
        await route_server.route_payload_async({"prompt": "REQ-TEXT", "messages": bad})


# ── _call_text packs with door="gateway" ─────────────────────────────────────

def _rig(monkeypatch, stub_config):
    if stub_config:
        config = SimpleNamespace(
            context_enabled=True, caveman_mode="off", prompt_cache_enabled=False,
        )
        monkeypatch.setattr(router, "get_config", lambda: config)
    monkeypatch.setattr(local_models, "num_ctx", lambda model: 8192)
    context._reset_session_buffers_for_test()

    async def no_summaries(limit=3):
        return []

    monkeypatch.setattr(context, "get_recent_session_summaries", no_summaries)
    monkeypatch.setattr(session_store, "resolve_session_id", lambda explicit=None: explicit)
    seen = []

    async def fake(model, messages, **kwargs):
        seen.append(messages)
        return LLMResponse(
            content="ok", model=model, input_tokens=1, output_tokens=1,
            cost_usd=0.0, latency_ms=1.0, provider=model.split("/", 1)[0],
        )

    monkeypatch.setattr(providers, "call_llm", fake)
    return seen


@pytest.fixture
def rig(monkeypatch):
    yield _rig(monkeypatch, True)
    context._reset_session_buffers_for_test()


@pytest.fixture
def rig_real(monkeypatch):
    yield _rig(monkeypatch, False)
    context._reset_session_buffers_for_test()


async def _call(model="ollama/x", **kwargs):
    return await router._call_text(
        model, "REQ-TEXT", None, None, None, TaskType.QUERY,
        context_door="gateway", **kwargs,
    )


def _spy(monkeypatch):
    calls = []
    real = context_pack.build_pack

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(context_pack, "build_pack", spy)
    return calls


@pytest.mark.asyncio
async def test_one_gateway_pack_per_attempt(monkeypatch, rig):
    calls = _spy(monkeypatch)
    await _call(conversation=HISTORY)
    await _call(model="openai/gpt-4o", conversation=HISTORY)
    assert [(c["door"], c["messages"], c["target_provider"]) for c in calls] == [
        ("gateway", HISTORY, "ollama"),
        ("gateway", HISTORY, "openai"),
    ]
    assert rig[0] == [*HISTORY, REQUEST]


@pytest.mark.asyncio
async def test_local_mode_gates_gateway_history_per_attempt(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    await _call(model="ollama/x", conversation=HISTORY[:1])
    await _call(model="openai/gpt-4o", conversation=HISTORY[:1])
    assert rig == [[HISTORY[0], REQUEST], [REQUEST]]


@pytest.mark.asyncio
async def test_off_mode_never_sends_gateway_history(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    await _call(model="ollama/x", conversation=HISTORY[:1])
    assert rig == [[REQUEST]]


@pytest.mark.asyncio
async def test_gateway_pack_error_records_its_own_code(monkeypatch, rig):
    error = RuntimeError("pack down")
    records = []

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(context_pack, "build_pack", boom)
    monkeypatch.setattr(failopen, "record", lambda *args: records.append(args))
    response = await _call(conversation=HISTORY)
    assert response.content == "ok"
    assert rig == [[REQUEST]]
    assert records == [("CHZ-FO-ROUTER-GATEWAY-PACK", error)]


# ── end to end: HTTP request → provider messages ─────────────────────────────

def test_chat_completion_reaches_the_provider_as_packed_turns(monkeypatch, client, rig_real):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")
    monkeypatch.setattr(route_server, "_log_route_savings", lambda *a, **k: None)
    calls = _spy(monkeypatch)
    r = client.post("/api/chat", json={
        "model": "ollama/x",
        "messages": [*HISTORY, REQUEST],
    })
    assert r.status_code == 200, r.text
    assert [c["door"] for c in calls] == ["gateway"]
    sent = rig_real[-1]
    assert sent[-1]["role"] == "user" and sent[-1]["content"].endswith("REQ-TEXT")
    assert HISTORY[0] in sent and HISTORY[1] in sent
    assert all("user: " not in m["content"] for m in sent)


@pytest.mark.asyncio
async def test_gateway_cloud_window_keeps_a_mid_size_conversation_whole(rig):
    """Owner decision: the gateway's cloud window is 32k, so a ~12k-token
    conversation is sent whole; the SDK door (8k) keeps only the last 7."""
    convo = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"T{i} " + "w" * 2400}
             for i in range(20)]
    await _call(model="openai/gpt-4o", conversation=convo)
    await router._call_text("openai/gpt-4o", "REQ-TEXT", None, None, None, TaskType.QUERY,
                            conversation=convo, context_door="sdk")
    gateway, sdk_door = rig
    assert [m["content"][:4] for m in gateway[:-1]] == [m["content"][:4] for m in convo]
    # Last 7 are T13..T19; T13 is an assistant turn, dropped as a leading one.
    assert len(sdk_door) == 7 and sdk_door[0]["content"].startswith("T14 ")
    assert router._GATEWAY_NON_LOCAL_WINDOW == 32_768


# ── review fixes (CLI providers, redaction, budgets, context off, roles, cache key) ──

@pytest.mark.asyncio
async def test_cli_providers_get_the_callers_history(monkeypatch):
    config = SimpleNamespace(context_enabled=False)
    out = await router._cli_prompt_with_context(
        "REQ-TEXT", "codex", None, config, None, conversation=HISTORY, context_door="gateway")
    assert MARK + " q" in out and MARK + " a" in out and out.endswith("REQ-TEXT")
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")  # codex is not privacy-local
    assert await router._cli_prompt_with_context(
        "REQ-TEXT", "codex", None, config, None, conversation=HISTORY,
        context_door="gateway") == "REQ-TEXT"


@pytest.mark.asyncio
async def test_history_is_redacted_like_the_prompt(monkeypatch, rig_real):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")

    def fake_redact(text):
        return text.replace("SECRET-1", "[REDACTED]"), ({"key": 1} if "SECRET-1" in text else {})

    monkeypatch.setattr(router, "_maybe_redact", fake_redact)
    history = [{"role": "user", "content": "my key is SECRET-1"},
               {"role": "assistant", "content": [{"type": "text", "text": "noted SECRET-1"}]}]
    await router.route_and_call(TaskType.QUERY, "REQ-TEXT", model_override="ollama/x",
                                messages=history, context_door="gateway")
    sent = " ".join(m["content"] for m in rig_real[-1])
    assert "SECRET-1" not in sent and sent.count("[REDACTED]") == 2


def test_budget_estimates_count_the_history():
    assert router._messages_chars(None) == 0
    assert router._messages_chars([
        {"role": "user", "content": "abcd"},
        {"role": "assistant", "content": [{"type": "text", "text": "ef"},
                                          {"type": "tool_result", "content": "ghi"}]},
    ]) == 9


@pytest.mark.asyncio
async def test_history_is_sent_with_context_injection_off(monkeypatch, rig):
    monkeypatch.setattr(router, "get_config", lambda: SimpleNamespace(
        context_enabled=False, caveman_mode="off", prompt_cache_enabled=False))
    await _call(conversation=HISTORY)
    assert rig == [[*HISTORY, REQUEST]]


def test_unknown_roles_and_bare_strings_become_user_turns(client, payloads):
    r = client.post("/v1/chat/completions", json={"messages": [
        {"role": "function", "name": "f", "content": "FN-OUT"}, REQUEST]})
    assert r.status_code == 200, r.text
    assert payloads[0]["messages"] == [{"role": "user", "content": "[function] FN-OUT"}]
    r = client.post("/v1/responses", json={"input": ["Background: py3.8", REQUEST]})
    assert r.status_code == 200, r.text
    assert payloads[1]["messages"] == [{"role": "user", "content": "Background: py3.8"}]


def test_cache_key_ignores_the_session_buffer_when_history_is_given(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the process buffer must not be read for a caller history")

    monkeypatch.setattr(router, "get_session_buffer", boom)
    router._semantic_cache_key("p", None, None, None, messages=HISTORY)
