"""AGT A.2-t: gateway tools end-to-end, driven by the OFFICIAL openai and anthropic clients.

A real uvicorn on 127.0.0.1:0 runs the real gateway app, router and provider layer. The
only fake is ``litellm.acompletion`` (the network edge), so a request travels the whole
path: wire format -> normalisation -> route_payload_async -> route_and_call -> capability
filter -> call_llm -> fake -> tool_calls -> wire format. One API provider is configured
(openai, a test key), the CLI backends are off and any non-loopback socket is refused.

A.2-s (streaming) is at the end of this file: the same fake, the same two clients, ``stream=True``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
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
        self.gate: asyncio.Event | None = None   # A.2-s: hold the call until the test sets it
        self.cancelled = threading.Event()       # A.2-s: the held call was cancelled upstream
        self.fail: Exception | None = None       # A.2-s: the backend raises
        self.delay = 0.0                         # F76: seconds the backend takes to answer

    async def __call__(self, **kw):
        self.calls.append(copy.deepcopy({k: v for k, v in kw.items() if k != "timeout"}))
        if self.fail is not None:
            raise self.fail
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
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


# ── A.2-s: streaming (SSE) on all three endpoints, official clients ─────────────────────
# The gateway streams the routed call's complete answer (see gateway._sse_response): the
# fake is the same non-streaming litellm edge, so a streamed call is routed, gated, cached
# and ledgered exactly like a non-streamed one.

import httpx  # noqa: E402  (a dependency of both clients)

ENDPOINTS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")


def _body(path, tools=False, text=PROMPT):
    if path == "/v1/chat/completions":
        b = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": text}]}
        return {**b, "tools": [CHAT_TOOL]} if tools else b
    if path == "/v1/responses":
        b = {"model": "gpt-4o", "stream": True, "input": text}
        return {**b, "tools": [RESPONSES_TOOL]} if tools else b
    b = {"model": "claude-haiku-4-5", "max_tokens": 64, "stream": True,
         "messages": [{"role": "user", "content": text}]}
    return {**b, "tools": [ANTHROPIC_TOOL]} if tools else b


def _frames(base, path, body):
    """The raw SSE frames of one request: [(event name or None, data text)]; comments skipped."""
    out = []
    with httpx.stream("POST", base + path, json=body, timeout=30) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("text/event-stream")
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                out.append((event, line[5:].strip()))
                event = None
    return out


def _usage_rows():
    from llm_router.config import get_config

    con = sqlite3.connect(str(get_config().llm_router_db_path))
    try:
        return con.execute("SELECT model, provider, input_tokens, output_tokens, success, reason "
                           "FROM usage ORDER BY id").fetchall()
    finally:
        con.close()


def test_chat_stream_text_chunks_finish_reason_and_ledger(oai, fake):
    stream = oai.chat.completions.create(
        model="gpt-4o", stream=True, stream_options={"include_usage": True},
        messages=[{"role": "user", "content": PROMPT}])
    chunks = list(stream)
    assert all(c.object == "chat.completion.chunk" for c in chunks)
    assert len({c.id for c in chunks}) == 1
    assert chunks[0].choices[0].delta.role == "assistant"
    deltas = [c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content]
    assert len(deltas) > 1 and "".join(deltas) == FINAL
    finish = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]
    assert finish == ["stop"] and chunks[-2].choices[0].finish_reason == "stop"
    assert not chunks[-1].choices and (chunks[-1].usage.prompt_tokens,
                                       chunks[-1].usage.completion_tokens) == (11, 7)
    rows = _usage_rows()
    assert len(rows) == 1 and rows[0][2:5] == (11, 7, 1)


def test_chat_stream_without_include_usage_has_no_usage_chunk(oai, fake):
    chunks = list(oai.chat.completions.create(
        model="gpt-4o", stream=True, messages=[{"role": "user", "content": PROMPT}]))
    assert all(c.usage is None for c in chunks) and chunks[-1].choices[0].finish_reason == "stop"


def test_chat_stream_tool_call_round_trip(oai, fake):
    msgs = [{"role": "user", "content": PROMPT}]
    chunks = list(oai.chat.completions.create(model="gpt-4o", stream=True, messages=msgs,
                                              tools=[CHAT_TOOL]))
    calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
    assert calls[0].id == "call_abc123" and calls[0].function.name == "get_weather"
    assert calls[0].index == 0 and len(calls) > 2          # header + argument fragments
    assert json.loads("".join(tc.function.arguments or "" for tc in calls)) == ARGS
    assert [c.choices[0].finish_reason for c in chunks if c.choices][-1] == "tool_calls"
    assert not any(c.choices and c.choices[0].delta.content for c in chunks)

    msgs += [{"role": "assistant", "content": None, "tool_calls": [
                 {"id": "call_abc123", "type": "function",
                  "function": {"name": "get_weather", "arguments": json.dumps(ARGS)}}]},
             {"role": "tool", "tool_call_id": "call_abc123", "content": RESULT}]
    chunks = list(oai.chat.completions.create(model="gpt-4o", stream=True, messages=msgs,
                                              tools=[CHAT_TOOL]))
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == FINAL
    assert [c.choices[0].finish_reason for c in chunks if c.choices][-1] == "stop"
    assert len(fake.calls) == 2 and RESULT in json.dumps(fake.calls[1]["messages"])
    assert len(_usage_rows()) == 2


def test_responses_stream_text_event_order_final_response_and_ledger(oai, fake):
    events = list(oai.responses.create(model="gpt-4o", input=PROMPT, stream=True))
    types = [e.type for e in events]
    deltas = [e.delta for e in events if e.type == "response.output_text.delta"]
    assert len(deltas) > 1 and "".join(deltas) == FINAL
    collapsed = [t for i, t in enumerate(types) if i == 0 or t != types[i - 1]]
    assert collapsed == ["response.created", "response.in_progress", "response.output_item.added",
                         "response.content_part.added", "response.output_text.delta",
                         "response.output_text.done", "response.content_part.done",
                         "response.output_item.done", "response.completed"]
    assert [e.sequence_number for e in events] == list(range(len(events)))
    done = events[-1].response
    assert done.status == "completed" and done.output_text == FINAL
    assert (done.usage.input_tokens, done.usage.output_tokens) == (11, 7)
    assert events[0].response.status == "in_progress" and events[0].response.id == done.id
    rows = _usage_rows()
    assert len(rows) == 1 and rows[0][2:5] == (11, 7, 1)


def test_responses_stream_helper_tool_call_round_trip(oai, fake):
    with oai.responses.stream(model="gpt-4o", input=PROMPT, tools=[RESPONSES_TOOL]) as s:
        types = [e.type for e in s]
        final = s.get_final_response()
    calls = [o for o in final.output if o.type == "function_call"]
    assert len(calls) == 1 and calls[0].call_id == "call_abc123" and calls[0].name == "get_weather"
    assert json.loads(calls[0].arguments) == ARGS
    assert types.count("response.function_call_arguments.delta") > 1
    assert "response.function_call_arguments.done" in types and types[-1] == "response.completed"

    turn2 = [{"role": "user", "content": PROMPT},
             {"type": "function_call", "call_id": "call_abc123", "name": "get_weather",
              "arguments": calls[0].arguments},
             {"type": "function_call_output", "call_id": "call_abc123", "output": RESULT}]
    with oai.responses.stream(model="gpt-4o", input=turn2, tools=[RESPONSES_TOOL]) as s:
        final = s.get_final_response()
    assert final.output_text == FINAL and final.status == "completed"
    assert len(_usage_rows()) == 2


def test_anthropic_stream_text_event_order_stop_reason_and_ledger(ant, fake):
    events = list(ant.messages.create(model="claude-haiku-4-5", max_tokens=64, stream=True,
                                      messages=[{"role": "user", "content": PROMPT}]))
    types = [e.type for e in events]
    collapsed = [t for i, t in enumerate(types) if i == 0 or t != types[i - 1]]
    assert collapsed == ["message_start", "content_block_start", "content_block_delta",
                         "content_block_stop", "message_delta", "message_stop"]
    assert "".join(e.delta.text for e in events if e.type == "content_block_delta") == FINAL
    assert types.count("content_block_delta") > 1
    assert events[0].message.usage.input_tokens == 11
    md = [e for e in events if e.type == "message_delta"][0]
    assert md.delta.stop_reason == "end_turn" and md.usage.output_tokens == 7
    with ant.messages.stream(model="claude-haiku-4-5", max_tokens=64,
                             messages=[{"role": "user", "content": PROMPT + " again"}]) as s:
        assert "".join(s.text_stream) == FINAL
        msg = s.get_final_message()
    assert msg.stop_reason == "end_turn" and msg.content[0].text == FINAL
    assert (msg.usage.input_tokens, msg.usage.output_tokens) == (11, 7)
    rows = _usage_rows()
    assert len(rows) == 2 and all(r[2:5] == (11, 7, 1) for r in rows)


def test_anthropic_stream_tool_call_round_trip(ant, fake):
    msgs = [{"role": "user", "content": PROMPT}]
    with ant.messages.stream(model="claude-haiku-4-5", max_tokens=64, tools=[ANTHROPIC_TOOL],
                             messages=msgs) as s:
        types = [e.type for e in s]
        msg = s.get_final_message()
    assert msg.stop_reason == "tool_use"
    block = msg.content[0]
    assert block.type == "tool_use" and block.id == "call_abc123" and block.input == ARGS
    assert types.count("input_json") > 1 and types[-1] == "message_stop"

    msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": "call_abc123",
                                                "name": "get_weather", "input": ARGS}]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_abc123",
                                           "content": RESULT}]}]
    with ant.messages.stream(model="claude-haiku-4-5", max_tokens=64, tools=[ANTHROPIC_TOOL],
                             messages=msgs) as s:
        msg = s.get_final_message()
    assert msg.stop_reason == "end_turn" and msg.content[0].text == FINAL
    assert len(_usage_rows()) == 2


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("tools", [False, True])
def test_stream_is_routed_like_the_non_stream_call_and_ledgered_once(base, fake, path, tools):
    """Same routing decision (the backend sees the same call) and one usage row with the same
    fields as the non-streamed call, for every endpoint, with and without a tool call."""
    plain = {k: v for k, v in _body(path, tools=tools).items() if k != "stream"}
    r = httpx.post(base + path, json=plain, timeout=30)
    assert r.status_code == 200, r.text
    assert len(_frames(base, path, _body(path, tools=tools))) > 2
    assert len(fake.calls) == 2 and fake.calls[0] == fake.calls[1]
    rows = _usage_rows()
    assert len(rows) == 2 and rows[0] == rows[1] and rows[1][4] == 1


@pytest.mark.parametrize("path", ENDPOINTS)
def test_stream_with_a_refused_request_is_still_an_http_400_before_any_event(base, fake, path):
    body = _body(path)
    body["tools"] = [{"type": "web_search_preview"} if path != "/v1/messages"
                     else {"type": "web_search_20250305", "name": "web_search"}]
    r = httpx.post(base + path, json=body, timeout=30)
    assert r.status_code == 400 and not r.headers["content-type"].startswith("text/event-stream")
    assert fake.calls == []


@pytest.mark.timing
def test_client_disconnect_cancels_the_upstream_call_and_leaves_one_marked_row(base, fake):
    fake.gate = asyncio.Event()                  # the backend never answers
    with httpx.stream("POST", base + "/v1/chat/completions", json=_body("/v1/chat/completions"),
                      timeout=30) as r:
        assert r.status_code == 200              # headers are out while the model works
        for _ in range(200):
            if fake.calls:
                break
            time.sleep(0.05)
        assert len(fake.calls) == 1
    for _ in range(200):                         # the connection is closed: the gateway reacts
        if fake.cancelled.is_set() and _usage_rows():
            break
        time.sleep(0.05)
    assert fake.cancelled.is_set(), "the upstream call was not cancelled"
    rows = _usage_rows()
    assert len(rows) == 1 and rows[0][4] == 0 and rows[0][5] == "error_cancelled"
    assert rows[0][2:4] == (0, 0)


@pytest.mark.parametrize("path", ENDPOINTS)
def test_backend_failure_after_headers_is_a_format_correct_error_event(base, fake, path):
    fake.fail = RuntimeError("backend down")
    frames = _frames(base, path, _body(path))
    event, data = frames[-1]
    payload = json.loads(data)
    if path == "/v1/messages":
        assert event == "error" and payload["type"] == "error"
        assert payload["error"]["type"] == "api_error" and "failed" in payload["error"]["message"]
    elif path == "/v1/responses":
        assert event == "error" and payload["type"] == "error" and payload["error"]["message"]
    else:
        assert event is None and payload["error"]["message"] and payload["error"]["type"]
    assert not any(d == "[DONE]" for _, d in frames)   # an errored chat stream does not end "cleanly"
    rows = _usage_rows()
    assert len(rows) == 1 and rows[0][4] == 0


def test_official_clients_raise_on_the_error_event(oai, ant, fake):
    fake.fail = RuntimeError("backend down")
    with pytest.raises(openai.APIError):
        list(oai.chat.completions.create(model="gpt-4o", stream=True,
                                         messages=[{"role": "user", "content": PROMPT}]))
    with pytest.raises(openai.APIError):
        list(oai.responses.create(model="gpt-4o", input=PROMPT, stream=True))
    with pytest.raises(anthropic.APIStatusError):
        with ant.messages.stream(model="claude-haiku-4-5", max_tokens=64,
                                 messages=[{"role": "user", "content": PROMPT}]) as s:
            list(s)


def test_anthropic_unparseable_tool_arguments_become_an_error_event_when_streaming(base, fake):
    fake.arguments = "{not json"
    frames = _frames(base, "/v1/messages", _body("/v1/messages", tools=True))
    event, data = frames[-1]
    assert event == "error" and "not a JSON object" in json.loads(data)["error"]["message"]
    assert not any(e == "content_block_start" for e, _ in frames)


def test_anthropic_system_as_text_blocks_is_accepted_and_forwarded(ant, fake):
    """A.6: Claude Code and the Claude Agent SDK send `system` as a list of text blocks (with
    cache_control), not a string; the gateway used to answer 422."""
    reply = ant.messages.create(
        model="claude-haiku-4-5", max_tokens=64, messages=[{"role": "user", "content": PROMPT}],
        system=[{"type": "text", "text": "billing-header"},
                {"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}])
    assert reply.content[0].text == FINAL
    sent = fake.calls[0]["messages"]
    assert any(m["role"] == "system" and "be brief" in json.dumps(m["content"]) for m in sent), sent


# ── F76: keep-alive, finished-before-disconnect, scrubbed and rate-limit error events ────
def _raw_lines(base, path, body):
    with httpx.stream("POST", base + path, json=body, timeout=30) as r:
        assert r.status_code == 200, r.read()
        return list(r.iter_lines())


@pytest.mark.timing
def test_stream_sends_keep_alive_comments_while_the_model_works(base, fake, monkeypatch):
    from llm_router import gateway

    monkeypatch.setattr(gateway, "_KEEPALIVE_S", 0.05)
    fake.delay = 0.6
    lines = _raw_lines(base, "/v1/chat/completions", _body("/v1/chat/completions"))
    first_data = next(i for i, ln in enumerate(lines) if ln.startswith("data:"))
    assert any(ln == ": keep-alive" for ln in lines[:first_data]), lines[:first_data + 1]
    assert "data: [DONE]" in lines
    deltas = [json.loads(ln[5:])["choices"][0]["delta"].get("content") or ""
              for ln in lines if ln.startswith("data:") and ln != "data: [DONE]"]
    assert "".join(deltas) == FINAL


@pytest.mark.timing
def test_a_call_that_finished_before_the_disconnect_keeps_its_success_row(base, fake):
    with httpx.stream("POST", base + "/v1/chat/completions", json=_body("/v1/chat/completions"),
                      timeout=30) as r:
        assert r.status_code == 200
        for line in r.iter_lines():             # the first data frame exists only once the call is done
            if line.startswith("data:"):
                break
    time.sleep(0.5)                             # the disconnect has been seen by the gateway
    rows = _usage_rows()
    assert len(rows) == 1 and rows[0][4] == 1 and rows[0][5] != "error_cancelled", rows
    assert not fake.cancelled.is_set()


class RateLimitError(Exception):
    """Shaped like litellm / openai / anthropic ``RateLimitError`` (found by class name)."""

    def __init__(self, msg="slow down", retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def _fail_route(monkeypatch, exc):
    import llm_router.route_server as route_server

    async def failing(payload):
        raise exc

    monkeypatch.setattr(route_server, "route_payload_async", failing)


def _wire_error(path, frames):
    """(type, code-or-None, message) of the last frame, per wire format."""
    payload = json.loads(frames[-1][1])
    err = payload["error"]
    code = payload.get("code") if path == "/v1/responses" else err.get("code")
    return err["type"], code, err["message"]


def _check_status(path, plain_status, frames, status, kind):
    assert plain_status == status
    etype, code, _ = _wire_error(path, frames)
    assert etype == kind
    if path != "/v1/messages":      # the Anthropic error event has no code field
        assert code == str(status)
    assert (frames[-1][0] == "error") == (path != "/v1/chat/completions")


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("make,retry", [
    (lambda: litellm.RateLimitError(message="slow down", llm_provider="openai", model="gpt-4o"), None),
    (lambda: RateLimitError(retry_after=7), "7"),
])
def test_a_provider_rate_limit_is_429_on_every_endpoint(base, fake, monkeypatch, path, make, retry):
    _fail_route(monkeypatch, make())
    plain = {k: v for k, v in _body(path).items() if k != "stream"}
    r = httpx.post(base + path, json=plain, timeout=30)
    frames = _frames(base, path, _body(path))
    _check_status(path, r.status_code, frames, 429, "rate_limit_error")
    assert r.headers.get("retry-after") == retry
    assert ("retry after 7 s" in _wire_error(path, frames)[2]) == (retry == "7")


def test_a_rate_limit_behind_a_wrapper_is_429(base, fake, monkeypatch):
    try:
        try:
            raise RateLimitError()
        except RateLimitError as inner:
            raise LookupError("wrapped") from inner
    except LookupError as wrapped:
        _fail_route(monkeypatch, wrapped)
    plain = {k: v for k, v in _body("/v1/messages").items() if k != "stream"}
    assert httpx.post(base + "/v1/messages", json=plain, timeout=30).status_code == 429


def _not_a_rate_limit():
    from llm_router.types import BudgetExceededError

    return {
        "aggregate": RuntimeError(
            "All models failed for query/simple. Last error: auth. Chain failures: "
            "1. openai/gpt-4o: 429 Too Many Requests; 2. gemini/x: 401"),
        "budget": BudgetExceededError("cheapest option costs ~$0.0429, over the cap"),
        "quota": RuntimeError("quota exceeded for this project"),
        "plain": RuntimeError("boom"),
    }


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("name", ["aggregate", "budget", "quota", "plain"])
def test_anything_that_is_not_itself_a_rate_limit_stays_502(base, fake, monkeypatch, path, name):
    _fail_route(monkeypatch, _not_a_rate_limit()[name])
    plain = {k: v for k, v in _body(path).items() if k != "stream"}
    r = httpx.post(base + path, json=plain, timeout=30)
    assert "retry-after" not in r.headers
    frames = _frames(base, path, _body(path))
    assert r.status_code == 502
    etype, code, _ = _wire_error(path, frames)
    assert etype == "api_error"
    if path != "/v1/messages":
        assert code == "502"


@pytest.mark.parametrize("path", ENDPOINTS)
def test_provider_error_text_never_reaches_the_client(base, fake, monkeypatch, path):
    secret = "sk-ant-api03-" + "A" * 40
    seeded = "SEEDED-PROMPT-TEXT-Lisbon-weather"
    _fail_route(monkeypatch, RuntimeError(f"upstream echoed {seeded} {secret} " + "x" * 400))
    plain = {k: v for k, v in _body(path, text=seeded).items() if k != "stream"}
    r = httpx.post(base + path, json=plain, timeout=30)
    wire = _frames(base, path, _body(path, text=seeded))
    streamed = "\n".join(d for _, d in wire)
    for text in (r.text, streamed):
        assert seeded not in text and secret not in text and "xxxx" not in text, text
    assert r.status_code == 502 and "RuntimeError: routing failed" in r.text
    assert _wire_error(path, wire)[2] == "RuntimeError: routing failed"
    # the same seeded text in a rate limit's message stays out of a 429 too
    _fail_route(monkeypatch, RateLimitError(f"limit; prompt was {seeded}"))
    r = httpx.post(base + path, json=plain, timeout=30)
    assert r.status_code == 429 and seeded not in r.text
    assert seeded not in "\n".join(d for _, d in _frames(base, path, _body(path, text=seeded)))


def test_an_unexpected_stream_error_names_only_the_class(base, fake, monkeypatch):
    from llm_router import gateway

    secret = "sk-ant-api03-" + "B" * 40

    def broken(body, include_usage):
        raise ValueError(f"cannot render {secret}")
        yield b""  # pragma: no cover - makes this a generator, as the real one is

    monkeypatch.setattr(gateway, "_chat_frames", broken)
    message = json.loads(_frames(base, "/v1/chat/completions", _body("/v1/chat/completions"))[-1][1]
                         )["error"]["message"]
    assert message == "stream failed: ValueError", message
