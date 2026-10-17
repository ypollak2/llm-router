"""AGT A.2-t: gateway tools end-to-end, driven by the OFFICIAL openai and anthropic clients.

A real uvicorn on 127.0.0.1:0 runs the real gateway app, router and provider layer. The
only fake is ``litellm.acompletion`` (the network edge), so a request travels the whole
path: wire format -> normalisation -> route_payload_async -> route_and_call -> capability
filter -> call_llm -> fake -> tool_calls -> wire format. One API provider is configured
(openai, a test key), the CLI backends are off and any non-loopback socket is refused.

Not in this file (A.2-s): streaming. ``stream: true`` with tools keeps the existing 400.
"""

from __future__ import annotations

import copy
import json
import socket
import threading
import time

import pytest

openai = pytest.importorskip("openai")
anthropic = pytest.importorskip("anthropic")
litellm = pytest.importorskip("litellm")
uvicorn = pytest.importorskip("uvicorn")

ARGS = {"city": "Lisbon", "unit": "c"}
RESULT = "RESULT-SUNNY-21C"
PROMPT = "what is the weather in Lisbon right now"
FINAL = "It is sunny and 21C in Lisbon."

CHAT_TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Get the weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}
RESPONSES_TOOL = {"type": "function", "name": "get_weather", "description": "Get the weather",
                  "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}
ANTHROPIC_TOOL = {"name": "get_weather", "description": "Get the weather",
                  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}
NAMED = {"type": "function", "function": {"name": "get_weather"}}


class Fake:
    """``litellm.acompletion``: records every call; answers a tool request with one tool call
    until the (rendered) tool result is in the conversation, then with text."""

    def __init__(self):
        self.calls: list[dict] = []
        self.mode = "tool"            # "tool": call the tool; "text": ignore the tools
        self.backend_reason = "stop"  # what the backend reports beside a tool call
        self.arguments = json.dumps(ARGS)

    async def __call__(self, **kw):
        self.calls.append(copy.deepcopy({k: v for k, v in kw.items() if k != "timeout"}))
        has_result = RESULT in json.dumps(kw.get("messages"))
        if kw.get("tools") and self.mode == "tool" and not has_result:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_abc123", "type": "function",
                 "function": {"name": "get_weather", "arguments": self.arguments}}]}
            reason = self.backend_reason
        else:
            message = {"role": "assistant", "content": FINAL}
            reason = "stop"
        return litellm.ModelResponse(
            model=kw["model"],
            choices=[{"index": 0, "message": message, "finish_reason": reason}],
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})

    def with_tools(self):
        return [c for c in self.calls if c.get("tools")]


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(litellm, "acompletion", f)
    return f


@pytest.fixture
def env(monkeypatch, tmp_path):
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY",
                "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY",
                "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY", "COHERE_API_KEY",
                "OLLAMA_BASE_URL", "OLLAMA_BUDGET_MODELS"):
        monkeypatch.delenv(var, raising=False)
    # A private state dir per test: no resource map, quality history or ledger from another
    # test in the same xdist worker (a leaked map once emptied this file's chain in a full run).
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test-not-real")
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")
    monkeypatch.setenv("LLM_ROUTER_GEMINI_SUBSCRIPTION", "false")
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "off")
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "on")
    import llm_router.config as config_module

    config_module._config = None
    # The dynamic routing table is a process global that unpinned chain building reads; a table
    # left by another test (other providers) emptied the chain in a full xdist run (tests/conftest
    # mock_env resets it for the same reason). Pinned-model tests never read it.
    from llm_router.dynamic_routing import reset_dynamic_routing

    reset_dynamic_routing()
    yield
    config_module._config = None
    reset_dynamic_routing()


@pytest.fixture
def no_outside_network(monkeypatch):
    real = socket.socket.connect

    def guarded(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test reached the network: {host}")
        return real(self, address, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def base(env, fake, no_outside_network):
    from llm_router.gateway import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "gateway did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def oai(base):
    return openai.OpenAI(base_url=f"{base}/v1", api_key="x", max_retries=0, timeout=30)


@pytest.fixture
def ant(base):
    return anthropic.Anthropic(base_url=base, api_key="x", max_retries=0, timeout=30)


def _openai_form(tools) -> bool:
    return bool(tools) and all(
        t["type"] == "function" and t["function"]["name"] == "get_weather"
        and t["function"]["parameters"]["type"] == "object" for t in tools)


# ── tool round trips, one per endpoint, official clients ────────────────────────────────

def test_chat_completions_tool_round_trip(oai, fake):
    msgs = [{"role": "user", "content": PROMPT}]
    r1 = oai.chat.completions.create(model="gpt-4o", messages=msgs, tools=[CHAT_TOOL])
    choice = r1.choices[0]
    assert choice.finish_reason == "tool_calls"
    call = choice.message.tool_calls[0]
    assert call.function.name == "get_weather"
    assert json.loads(call.function.arguments) == ARGS
    assert choice.message.content in (None, "")
    assert len(fake.calls) == 1 and _openai_form(fake.calls[0]["tools"])

    msgs += [{"role": "assistant", "content": None, "tool_calls": [
                 {"id": call.id, "type": "function",
                  "function": {"name": call.function.name, "arguments": call.function.arguments}}]},
             {"role": "tool", "tool_call_id": call.id, "content": RESULT}]
    r2 = oai.chat.completions.create(model="gpt-4o", messages=msgs, tools=[CHAT_TOOL])
    assert r2.choices[0].message.content == FINAL
    assert r2.choices[0].finish_reason == "stop"
    assert not r2.choices[0].message.tool_calls
    assert len(fake.calls) == 2 and RESULT in json.dumps(fake.calls[1]["messages"])


def test_responses_tool_round_trip(oai, fake):
    r1 = oai.responses.create(model="gpt-4o", input=PROMPT, tools=[RESPONSES_TOOL])
    calls = [o for o in r1.output if o.type == "function_call"]
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert json.loads(calls[0].arguments) == ARGS
    assert calls[0].call_id == "call_abc123"
    assert r1.status == "completed"
    assert len(fake.calls) == 1 and _openai_form(fake.calls[0]["tools"])

    turn2 = [{"role": "user", "content": PROMPT},
             {"type": "function_call", "call_id": calls[0].call_id, "name": calls[0].name,
              "arguments": calls[0].arguments},
             {"type": "function_call_output", "call_id": calls[0].call_id, "output": RESULT}]
    r2 = oai.responses.create(model="gpt-4o", input=turn2, tools=[RESPONSES_TOOL])
    assert r2.output_text == FINAL
    assert not [o for o in r2.output if o.type == "function_call"]
    assert RESULT in json.dumps(fake.calls[1]["messages"])


def test_anthropic_messages_tool_round_trip(ant, fake):
    msgs = [{"role": "user", "content": PROMPT}]
    r1 = ant.messages.create(model="claude-haiku-4-5", max_tokens=256, messages=msgs,
                             tools=[ANTHROPIC_TOOL])
    assert r1.stop_reason == "tool_use"
    use = [b for b in r1.content if b.type == "tool_use"]
    assert len(use) == 1
    assert use[0].name == "get_weather"
    assert isinstance(use[0].input, dict) and use[0].input == ARGS   # parsed object, not a string
    assert use[0].id == "call_abc123"
    assert len(fake.calls) == 1 and _openai_form(fake.calls[0]["tools"])

    msgs += [{"role": "assistant", "content": [
                 {"type": "tool_use", "id": use[0].id, "name": use[0].name, "input": use[0].input}]},
             {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": use[0].id, "content": RESULT}]}]
    r2 = ant.messages.create(model="claude-haiku-4-5", max_tokens=256, messages=msgs,
                             tools=[ANTHROPIC_TOOL])
    assert r2.stop_reason == "end_turn"
    assert [b.text for b in r2.content if b.type == "text"] == [FINAL]
    assert RESULT in json.dumps(fake.calls[1]["messages"])


# ── the backend's "stop" beside a tool call must not reach the client ─────────────────

def test_finish_reason_is_tool_calls_even_when_the_backend_says_stop(oai, ant, fake):
    fake.backend_reason = "stop"
    r = oai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": PROMPT}],
                                    tools=[CHAT_TOOL])
    assert r.choices[0].finish_reason == "tool_calls"
    a = ant.messages.create(model="claude-haiku-4-5", max_tokens=64,
                            messages=[{"role": "user", "content": PROMPT + " (anthropic)"}],
                            tools=[ANTHROPIC_TOOL])
    assert a.stop_reason == "tool_use"


# ── tool_choice reaches the model in OpenAI form ───────────────────────────────────────

@pytest.mark.parametrize("sent,expected", [
    ("auto", "auto"), ("required", "required"), (NAMED, NAMED)])
def test_chat_tool_choice_is_forwarded(oai, fake, sent, expected):
    oai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": PROMPT}],
                                tools=[CHAT_TOOL], tool_choice=sent)
    assert fake.with_tools()[-1]["tool_choice"] == expected


@pytest.mark.parametrize("sent,expected", [
    ("auto", "auto"), ("required", "required"),
    ({"type": "function", "name": "get_weather"}, NAMED)])
def test_responses_tool_choice_is_mapped(oai, fake, sent, expected):
    oai.responses.create(model="gpt-4o", input=PROMPT, tools=[RESPONSES_TOOL], tool_choice=sent)
    assert fake.with_tools()[-1]["tool_choice"] == expected


@pytest.mark.parametrize("sent,expected", [
    ({"type": "auto"}, "auto"), ({"type": "any"}, "required"),
    ({"type": "tool", "name": "get_weather"}, NAMED)])
def test_anthropic_tool_choice_is_mapped(ant, fake, sent, expected):
    ant.messages.create(model="claude-haiku-4-5", max_tokens=64, tools=[ANTHROPIC_TOOL],
                        messages=[{"role": "user", "content": PROMPT}], tool_choice=sent)
    assert fake.with_tools()[-1]["tool_choice"] == expected


def test_anthropic_tool_schema_is_mapped_to_a_function_tool(ant, fake):
    ant.messages.create(model="claude-haiku-4-5", max_tokens=64, tools=[ANTHROPIC_TOOL],
                        messages=[{"role": "user", "content": PROMPT}])
    sent = fake.with_tools()[-1]["tools"][0]
    assert sent["function"]["parameters"] == ANTHROPIC_TOOL["input_schema"]
    assert sent["function"]["description"] == "Get the weather"


# ── the semantic cache is never consulted or written for a tools request ──────────────────
# End to end against the REAL semantic cache, UNPINNED model (a pinned model skips the cache
# anyway, so a pinned test cannot see REQUEST_HAS_TOOLS). A.2-t review: three mutants survived
# the earlier mock-only test (REQUEST_HAS_TOOLS dropped from _cache_disabled, its reset removed,
# gates / quality escalation applied to tool-call replies).

LONG_PROMPT = ("what is the weather in Lisbon right now, and should I bring an umbrella and a "
               "jacket for the evening walk along the river, please check carefully")


@pytest.mark.parametrize("m", ["auto", "llm-router", "unknown-model-xyz"])
def test_unpinned_tools_request_twice_reaches_the_backend_twice(oai, fake, m):
    msgs = [{"role": "user", "content": PROMPT}]
    for _ in range(2):
        r = oai.chat.completions.create(model=m, messages=msgs, tools=[CHAT_TOOL])
        assert r.choices[0].finish_reason == "tool_calls"
    assert len(fake.with_tools()) == 2, "a tool-call turn was replayed from the cache"


def test_unpinned_plain_then_tools_then_plain(oai, fake):
    """plain primes the cache; tools must NOT be answered from it; plain is cached again."""
    msgs = [{"role": "user", "content": PROMPT}]
    r = oai.chat.completions.create(model="auto", messages=msgs)
    assert r.choices[0].message.content == FINAL
    assert len(fake.calls) == 1
    r = oai.chat.completions.create(model="auto", messages=msgs, tools=[CHAT_TOOL])
    assert r.choices[0].finish_reason == "tool_calls", "tools request got the cached text"
    assert len(fake.with_tools()) == 1
    r = oai.chat.completions.create(model="auto", messages=msgs)
    assert r.choices[0].message.content == FINAL
    assert not r.choices[0].message.tool_calls


def test_plain_request_after_tools_request_in_the_same_context_uses_the_cache(monkeypatch):
    """M1: the REQUEST_HAS_TOOLS reset. Both requests run in ONE task (asyncio.run copies the
    context, so separate runs hide a leak). A tools request must not leave the bypass on."""
    import asyncio

    import llm_router.route_server as rs
    import llm_router.router as R
    from llm_router import semantic_cache
    from llm_router.types import LLMResponse

    seen = []

    async def _fake(task_type, prompt, **kw):
        seen.append(semantic_cache._cache_disabled())
        return LLMResponse(content="ok", model="openai/gpt-4o", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="openai")

    async def both():
        await rs.route_payload_async({"prompt": "p", "tools": [CHAT_TOOL]})
        mid = semantic_cache.REQUEST_HAS_TOOLS.get()
        await rs.route_payload_async({"prompt": "p"})
        return mid

    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "on")
    monkeypatch.setattr(R, "route_and_call", _fake)
    monkeypatch.setattr(rs, "_log_route_savings", lambda *a, **k: None)
    mid = asyncio.run(both())
    assert seen == [True, False]
    assert mid is False, "REQUEST_HAS_TOOLS leaked past the tools request"


@pytest.fixture
def spies(monkeypatch):
    """Record every contract-gate run and every quality score, and still call the real ones.
    With one model in the chain a rejected reply is still returned (exhaustion floor), so a
    retry count cannot see a gate that wrongly ran; the call itself can."""
    import llm_router.quality_feedback as qf
    import llm_router.router as R

    seen = {"gates": [], "scores": []}
    real_gates, real_score = R.run_gates, qf.score_response

    def gates(contract, content):
        seen["gates"].append(content)
        return real_gates(contract, content)

    def score(content, *a, **k):
        seen["scores"].append(content)
        return real_score(content, *a, **k)

    monkeypatch.setattr(R, "run_gates", gates)
    monkeypatch.setattr(qf, "score_response", score)
    return seen


def test_tool_only_reply_skips_the_contract_gates_and_quality_scoring(oai, fake, spies):
    """M2 + M6. Empty content + tool_calls on a gated task type (the gateway classifies this
    prompt QUERY/SIMPLE: length gate, min 1 char, and an empty string fails it). Neither the
    gates nor quality escalation may judge a tool-call reply by its (empty) text."""
    msgs = [{"role": "user", "content": LONG_PROMPT}]
    r = oai.chat.completions.create(model="auto", tools=[CHAT_TOOL], messages=msgs)
    assert r.choices[0].finish_reason == "tool_calls"
    assert r.choices[0].message.tool_calls[0].function.name == "get_weather"
    assert len(fake.calls) == 1
    assert spies == {"gates": [], "scores": []}, "a tool-call reply was gated or scored"


def test_plain_reply_is_still_gated_and_scored(oai, fake, spies):
    """Anti-vacuity for the test above: the spies do fire for a text reply."""
    oai.chat.completions.create(model="auto", messages=[{"role": "user", "content": LONG_PROMPT}])
    assert spies["gates"] == [FINAL] and spies["scores"] == [FINAL]


def test_tool_call_reply_is_not_escalated_to_another_model(oai, fake, monkeypatch):
    """M6 behaviour: even with the threshold at 1.0 and a long prompt, a tool call is one call."""
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_THRESHOLD", "1.0")
    r = oai.chat.completions.create(model="auto", tools=[CHAT_TOOL],
                                    messages=[{"role": "user", "content": LONG_PROMPT}])
    assert r.choices[0].finish_reason == "tool_calls"
    assert len(fake.calls) == 1


# ── refusals that stay ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model", ["perplexity/sonar"])
def test_no_tool_capable_model_is_a_400(oai, ant, fake, model):
    """perplexity/sonar is known to lack tools (registry): the capability filter, fed the
    real tools array, empties the chain. Nothing is dispatched."""
    with pytest.raises(openai.BadRequestError) as exc:
        oai.chat.completions.create(model=model, messages=[{"role": "user", "content": PROMPT}],
                                    tools=[CHAT_TOOL])
    assert exc.value.status_code == 400 and "tools" in str(exc.value).lower()
    with pytest.raises(openai.BadRequestError):
        oai.responses.create(model=model, input=PROMPT, tools=[RESPONSES_TOOL])
    with pytest.raises(anthropic.BadRequestError):
        ant.messages.create(model=model, max_tokens=64, tools=[ANTHROPIC_TOOL],
                            messages=[{"role": "user", "content": PROMPT}])
    assert fake.calls == []


def test_the_same_model_answers_without_tools(oai, fake):
    """Anti-vacuity for the test above: the 400 is about the tools, not the model."""
    r = oai.chat.completions.create(model="perplexity/sonar",
                                    messages=[{"role": "user", "content": PROMPT}])
    assert r.choices[0].message.content == FINAL


def test_a_cli_backend_is_never_tried_with_tools(oai, fake, monkeypatch):
    """codex / gemini_cli return text only: pinned to one, the chain is empty -> 400."""
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "")
    with pytest.raises(openai.BadRequestError):
        oai.chat.completions.create(model="codex/gpt-5.5", tools=[CHAT_TOOL],
                                    messages=[{"role": "user", "content": PROMPT}])
    assert fake.calls == []


def test_streaming_with_tools_keeps_the_refusal_until_a2s(oai, ant, fake):
    with pytest.raises(openai.BadRequestError) as exc:
        oai.chat.completions.create(model="gpt-4o", stream=True, tools=[CHAT_TOOL],
                                    messages=[{"role": "user", "content": PROMPT}])
    assert "streaming not supported" in str(exc.value)
    with pytest.raises(anthropic.BadRequestError):
        ant.messages.create(model="claude-haiku-4-5", max_tokens=64, stream=True,
                            tools=[ANTHROPIC_TOOL], messages=[{"role": "user", "content": PROMPT}])
    assert fake.calls == []


def test_hosted_tools_are_refused_not_dropped(oai, ant, fake):
    with pytest.raises(openai.BadRequestError):
        oai.responses.create(model="gpt-4o", input=PROMPT, tools=[{"type": "web_search_preview"}])
    with pytest.raises(anthropic.BadRequestError):
        ant.messages.create(model="claude-haiku-4-5", max_tokens=64,
                            tools=[{"type": "web_search_20250305", "name": "web_search"}],
                            messages=[{"role": "user", "content": PROMPT}])
    assert fake.calls == []


def test_tool_choice_without_tools_is_still_refused(oai, fake):
    with pytest.raises(openai.BadRequestError):
        oai.chat.completions.create(model="gpt-4o", tool_choice="required",
                                    messages=[{"role": "user", "content": PROMPT}])
    assert fake.calls == []


def test_unparseable_tool_arguments_are_a_502_on_anthropic_not_a_string_input(ant, fake):
    fake.arguments = "{not json"
    with pytest.raises(anthropic.APIStatusError) as exc:
        ant.messages.create(model="claude-haiku-4-5", max_tokens=64, tools=[ANTHROPIC_TOOL],
                            messages=[{"role": "user", "content": PROMPT + " bad args"}])
    assert exc.value.status_code == 502


def test_a_request_without_tools_is_unchanged(oai, ant, fake):
    r = oai.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": PROMPT + " plain"}])
    assert r.choices[0].message.content == FINAL and r.choices[0].finish_reason == "stop"
    a = ant.messages.create(model="claude-haiku-4-5", max_tokens=64,
                            messages=[{"role": "user", "content": PROMPT + " plain anthropic"}])
    assert a.content[0].text == FINAL and a.stop_reason == "end_turn"
    assert all("tools" not in c and "tool_choice" not in c for c in fake.calls)
