"""SDK door: route_and_call(messages=) / route(messages=) build context via build_pack(door="sdk")."""

import os
from types import SimpleNamespace

import pytest

from llm_router import (
    call_identity,
    context,
    context_pack,
    failopen,
    local_models,
    providers,
    router,
    sdk,
    session_store,
)
from llm_router.types import LLMResponse, TaskType

MARK = "SDK-CONVERSATION-MARKER"
REQUEST = {"role": "user", "content": "REQ-TEXT"}
HISTORY = [
    {"role": "user", "content": MARK + " q"},
    {"role": "assistant", "content": MARK + " a"},
]


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
    """route_and_call needs the real config object (profile, budgets)."""
    yield _rig(monkeypatch, False)
    context._reset_session_buffers_for_test()


async def _call(prompt="REQ-TEXT", model="ollama/x", **kwargs):
    return await router._call_text(
        model, prompt, None, None, None, TaskType.QUERY, **kwargs,
    )


def _spy(monkeypatch):
    calls = []
    real = context_pack.build_pack

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(context_pack, "build_pack", spy)
    return calls


@pytest.mark.asyncio
async def test_one_sdk_pack_per_attempt_when_messages_given(monkeypatch, rig):
    calls = _spy(monkeypatch)
    await _call(conversation=HISTORY)
    await _call(model="openai/gpt-4o", conversation=HISTORY)
    assert [(c[0], c[1]["door"], c[1]["messages"], c[1]["target_provider"]) for c in calls] == [
        (("REQ-TEXT",), "sdk", HISTORY, "ollama"),
        (("REQ-TEXT",), "sdk", HISTORY, "openai"),
    ]


@pytest.mark.asyncio
async def test_no_sdk_pack_when_messages_absent(monkeypatch, rig):
    calls = _spy(monkeypatch)
    await _call()
    assert [c[1]["door"] for c in calls] == ["mcp"]
    assert "messages" not in calls[0][1]


@pytest.mark.asyncio
async def test_request_is_untruncated_with_messages(rig):
    request = "x" * 200_000
    await _call(prompt=request, conversation=HISTORY)
    assert rig == [[*HISTORY, {"role": "user", "content": request}]]


@pytest.mark.asyncio
async def test_message_order_and_leading_assistant_dropped(rig):
    convo = [
        {"role": "assistant", "content": "A0"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]
    await _call(model="openai/gpt-4o", conversation=convo)
    assert rig == [[
        {"role": "user", "content": "U1"}, {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"}, REQUEST,
    ]]


@pytest.mark.asyncio
async def test_trailing_copy_of_request_is_not_sent_twice(rig):
    await _call(conversation=[*HISTORY[:1], REQUEST])
    assert rig == [[HISTORY[0], REQUEST]]


@pytest.mark.asyncio
async def test_caller_context_still_sent_with_messages(rig):
    await _call(conversation=HISTORY[:1], caller_context="CTX")
    assert rig == [[{"role": "system", "content": "[Additional context]\nCTX"},
                    HISTORY[0], REQUEST]]


@pytest.mark.asyncio
async def test_local_mode_gates_conversation_per_attempt(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    await _call(model="ollama/x", conversation=HISTORY[:1])
    await _call(model="openai/gpt-4o", conversation=HISTORY[:1])
    assert rig == [[HISTORY[0], REQUEST], [REQUEST]]


@pytest.mark.asyncio
async def test_off_mode_never_sends_conversation(monkeypatch, rig):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    await _call(model="ollama/x", conversation=HISTORY[:1])
    assert rig == [[REQUEST]]


@pytest.mark.asyncio
async def test_weakened_provider_gate_demonstrates_leak(monkeypatch, rig):
    """Mutation: if the attempt's provider is mis-resolved as local, the
    conversation reaches an API provider. The gate tests above fail without
    the real per-attempt provider."""
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    await _call(model="openai/gpt-4o", conversation=HISTORY[:1])
    assert rig == [[REQUEST]]
    monkeypatch.setattr(router, "_pack_target_provider", lambda model: "ollama")
    await _call(model="openai/gpt-4o", conversation=HISTORY[:1])
    assert rig[1] == [HISTORY[0], REQUEST]


@pytest.mark.asyncio
async def test_pack_error_records_and_fails_open(monkeypatch, rig):
    error = RuntimeError("pack down")
    records = []

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(context_pack, "build_pack", boom)
    monkeypatch.setattr(failopen, "record", lambda *args: records.append(args))
    response = await _call(conversation=HISTORY)
    assert response.content == "ok"
    assert rig == [[REQUEST]]
    assert records == [("CHZ-FO-ROUTER-SDK-PACK", error)]


@pytest.mark.asyncio
async def test_no_messages_output_is_identical_to_empty_context(rig):
    await _call()
    await _call(caller_context="CTX")
    assert rig == [[REQUEST], [{"role": "system", "content": "[Additional context]\nCTX"}, REQUEST]]


@pytest.mark.asyncio
async def test_route_and_call_threads_messages_to_the_provider(monkeypatch, rig_real):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")
    resp = await router.route_and_call(
        TaskType.QUERY, "REQ-TEXT", model_override="ollama/x", messages=HISTORY[:1],
    )
    assert resp.content == "ok"
    assert HISTORY[0] in rig_real[-1] and rig_real[-1][-1]["content"].endswith("REQ-TEXT")


@pytest.mark.asyncio
async def test_route_and_call_without_messages_sends_no_history(monkeypatch, rig_real):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")
    await router.route_and_call(TaskType.QUERY, "REQ-TEXT", model_override="ollama/x")
    assert all(MARK not in m["content"] for m in rig_real[-1])


def test_semantic_cache_key_separates_conversations():
    a = router._semantic_cache_key("p", "", None, None, messages=HISTORY)
    b = router._semantic_cache_key("p", "", None, None, messages=HISTORY[:1])
    none = router._semantic_cache_key("p", "", None, None)
    assert len({repr(a), repr(b), repr(none)}) == 3


def test_sdk_route_with_messages_goes_through_route_and_call(monkeypatch):
    got = {}

    async def fake(task_type, prompt, **kwargs):
        got.update(prompt=prompt, **kwargs)
        return LLMResponse(content="hi", model="openai/gpt-4o", input_tokens=2,
                           output_tokens=3, cost_usd=0.0, latency_ms=4.0, provider="openai")

    monkeypatch.setattr(router, "route_and_call", fake)
    r = sdk.route("REQ", task_type="query", complexity="simple", messages=HISTORY)
    assert (r.text, r.model, r.provider, r.total_tokens) == ("hi", "openai/gpt-4o", "openai", 5)
    assert got["messages"] == HISTORY and got["prompt"] == "REQ"


@pytest.mark.asyncio
async def test_sdk_route_with_messages_works_inside_a_running_loop(monkeypatch):
    async def fake(task_type, prompt, **kwargs):
        return LLMResponse(content="hi", model="m", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="p")

    monkeypatch.setattr(router, "route_and_call", fake)
    assert sdk.route("REQ", task_type="query", complexity="simple", messages=[]).text == "hi"


def test_sdk_route_rejects_malformed_messages():
    with pytest.raises(ValueError):
        sdk.route("REQ", messages=[{"role": "user"}])
    with pytest.raises(ValueError):
        sdk.route("REQ", messages="not a list")


def test_sdk_route_without_messages_does_not_use_route_and_call(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("route_and_call must not be reached without messages")

    monkeypatch.setattr(router, "route_and_call", boom)
    monkeypatch.setattr(sdk, "_route", lambda prompt, **kw: "legacy")
    assert sdk.route("REQ") == "legacy"


# ── review fixes: timeout, error mapping, background-task drain ──

def _fake_rac(monkeypatch, exc=None):
    got = {}

    async def fake(task_type, prompt, **kwargs):
        got.update(kwargs)
        if exc is not None:
            raise exc
        return LLMResponse(content="hi", model="m", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="p")

    monkeypatch.setattr(router, "route_and_call", fake)
    return got


def test_sdk_messages_path_honours_timeout(monkeypatch):
    got = _fake_rac(monkeypatch)
    sdk.route("REQ", task_type="query", complexity="simple", messages=[], timeout=7)
    assert got["max_wall_clock_seconds"] == 7


def test_sdk_messages_path_error_mapping(monkeypatch):
    from llm_router.types import BudgetExceededError

    _fake_rac(monkeypatch, BudgetExceededError("cap"))
    with pytest.raises(BudgetExceededError):
        sdk.route("REQ", task_type="query", complexity="simple", messages=[])
    for exc in (ValueError("No models available"), TimeoutError("slow"), RuntimeError("x")):
        _fake_rac(monkeypatch, exc)
        with pytest.raises(sdk.RoutingError):
            sdk.route("REQ", task_type="query", complexity="simple", messages=[])


def test_sdk_messages_path_drains_background_tasks(monkeypatch):
    _fake_rac(monkeypatch)
    drained = []

    async def drain(timeout_s=5.0):
        drained.append(True)

    monkeypatch.setattr(router, "drain_bg_tasks", drain)
    sdk.route("REQ", task_type="query", complexity="simple", messages=[])
    assert drained == [True]
