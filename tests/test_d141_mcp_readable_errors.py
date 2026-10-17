"""D-141: an MCP tool error reaches the client as readable text, and routed calls have a deadline.

Bug A (coordinator repro 2026-10-16): ``llm(task="research")`` raised ValueError("No providers
available for research/premium ... Fix: ...") and Claude Code received only the bare text
``Error executing tool llm`` -- ``mcp`` 2.x keeps an unexpected exception's message on the
server. Bug B: a query chain of four local models hung >150 s with no exception while the MCP
client stops waiting at ~120 s.

Every test here drives a tool over the MCP protocol (``mcp.client.Client`` in memory) or
through ``IdentityMCPServer.call_tool``, the server method the protocol handler calls, so
the assertions are on what a client actually receives. No real provider is called:
``route_and_call`` or ``litellm.acompletion`` is replaced by a fake in every routed test.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router import call_identity, mcp_door

LIVE_MESSAGE = (
    "No providers available for research/premium. Configured providers: {'ollama', 'openrouter'}. "
    "Fix: run `llm-router doctor` to see what is missing. Set GEMINI_API_KEY for research."
)
PROMPT = "D141-PROMPT-CANARY please answer"


def _text(result) -> str:
    return result.content[0].text


def _probe(fn, name: str | None = None):
    server = call_identity.IdentityMCPServer("probe")
    server.tool(name=name)(fn)
    return server


async def _call(server, name: str, args: dict, guard_s: float = 10.0):
    """One ``tools/call`` over the in-memory MCP transport; never hangs the suite."""
    from mcp.client import Client

    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args)

    return await asyncio.wait_for(go(), guard_s)


@pytest.fixture
def _no_spend(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")
    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0):
        yield


# ── A. premise: what mcp itself does with an unexpected exception ─────────────

@pytest.mark.asyncio
async def test_premise_a_plain_mcp_server_sends_only_the_bare_text():
    """The bug's cause, pinned: without the door, mcp >= 2.2 (live: 2.2.0) sends the client
    the bare string only. 2.0.0 (uv.lock) still appended the message; the door handles both."""
    from mcp.server.mcpserver import MCPServer, exceptions

    async def boom() -> str:
        raise ValueError(LIVE_MESSAGE)

    server = MCPServer("plain")
    server.tool()(boom)
    result = await _call(server, "boom", {})
    assert result.is_error
    if hasattr(exceptions, "UnexpectedToolError"):
        assert _text(result) == "Error executing tool boom"
    else:
        assert _text(result) == f"Error executing tool boom: {LIVE_MESSAGE}"


# ── A. the door, on every tool family of the real server ──────────────────────

@pytest.mark.asyncio
async def test_identity_server_returns_the_exception_type_and_message():
    async def boom() -> str:
        raise ValueError(LIVE_MESSAGE)

    result = await _call(_probe(boom), "boom", {})
    text = _text(result)
    assert result.is_error
    assert text.splitlines()[0] == f"llm-router error: ValueError: {LIVE_MESSAGE}"
    assert "Error executing tool" not in text


def _real_server():
    from llm_router import server

    return server.mcp


def _first_line(result) -> str:
    return _text(result).splitlines()[0]


@pytest.mark.asyncio
async def test_real_llm_door_no_provider_message_reaches_the_client(temp_db):
    """The live repro: llm(task="research") -> route_and_call raises the fix-hint ValueError."""
    with patch("llm_router.tools.text.route_and_call",
               new_callable=lambda: AsyncMock(side_effect=ValueError(LIVE_MESSAGE))):
        result = await _call(_real_server(), "llm", {"prompt": PROMPT, "task": "research"})
    assert result.is_error
    assert _first_line(result) == f"llm-router error: ValueError: {LIVE_MESSAGE}"


@pytest.mark.asyncio
async def test_real_llm_door_end_to_end_with_no_providers(no_providers_env, monkeypatch):
    """No fake router: the real route_and_call finds no provider and its own text arrives.
    (task="query": since F25 / D-170 an empty research chain is a refusal, not an error;
    tests/test_f25_research_empty_chain.py covers that door.)"""
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    result = await _call(_real_server(), "llm", {"prompt": PROMPT, "task": "query"}, guard_s=60)
    first = _first_line(result)
    assert result.is_error
    assert first.startswith("llm-router error: "), first
    assert any(s in _text(result) for s in ("No providers available", "No available models",
                                             "All models failed")), _text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,target", [
    # the task aliases (llm_query/llm_code/...) are registered only with LLM_ROUTER_SLIM=off;
    # on the default surface they are reached through llm(task=...), which calls them.
    ("llm", {"prompt": PROMPT, "task": "query"}, "llm_router.tools.text.route_and_call"),
    ("llm", {"prompt": PROMPT, "task": "code"}, "llm_router.tools.text.route_and_call"),
    ("llm", {"prompt": PROMPT, "task": "analyze"}, "llm_router.tools.text.route_and_call"),
    ("llm", {"prompt": PROMPT, "task": "generate"}, "llm_router.tools.text.route_and_call"),
    ("llm_route", {"prompt": PROMPT, "complexity_override": "simple", "task_type": "query"},
     "llm_router.tools.routing.route_and_call"),
    ("llm_edit", {"task": "rename x", "files": []}, "llm_router.tools.text.route_and_call"),
])
async def test_real_routed_doors_return_readable_errors(temp_db, tool, args, target):
    err = RuntimeError("All models failed for query/balanced. Fix: start Ollama or set a key.")
    with patch(target, new_callable=lambda: AsyncMock(side_effect=err)):
        result = await _call(_real_server(), tool, args)
    assert result.is_error, _text(result)
    assert _first_line(result) == f"llm-router error: RuntimeError: {err}"


@pytest.mark.asyncio
async def test_real_non_routing_door_returns_a_readable_error(temp_db):
    with patch("llm_router.tools.consolidated._cache_view", side_effect=KeyError("cache-table")):
        result = await _call(_real_server(), "llm_router_status", {"view": "cache"})
    assert result.is_error
    assert _first_line(result) == "llm-router error: KeyError: 'cache-table'"


@pytest.mark.asyncio
async def test_the_models_tried_are_named_and_no_prompt_is_logged(temp_db):
    from llm_router import failopen

    async def route(q: str) -> str:
        mcp_door.attach_trace(["ollama/qwen3-coder:30b", "ollama/qwen3.5"])
        # the message carries the prompt (as some provider errors do), so the log check bites
        raise RuntimeError(f"All models failed for: {q}")

    result = await _call(_probe(route, name="llm_query"), "llm_query", {"q": PROMPT})
    assert _text(result).splitlines() == [
        f"llm-router error: RuntimeError: All models failed for: {PROMPT}",
        "tried: ollama/qwen3-coder:30b, ollama/qwen3.5",
    ]
    log = failopen.store_path().read_text()
    assert "CHZ-FO-MCP-TOOL-ERROR" in log
    assert "D141-PROMPT-CANARY" not in log


@pytest.mark.asyncio
async def test_secrets_in_the_error_message_are_scrubbed_before_the_client():
    async def leak() -> str:
        raise RuntimeError("provider rejected key AKIAIOSFODNN7EXAMPLE for this call")

    text = _text(await _call(_probe(leak), "leak", {}))
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert text == "llm-router error: RuntimeError: provider rejected key [REDACTED-AWS_KEY_ID] for this call"


@pytest.mark.asyncio
async def test_the_crash_traceback_is_logged_at_debug_without_secrets(monkeypatch):
    import llm_router.logging as lr_logging

    seen: list = []

    class _Rec:
        def debug(self, event, **kw):
            seen.append(("debug", event, kw))

        def warning(self, event, **kw):
            seen.append(("warning", event, kw))

    monkeypatch.setattr(lr_logging, "get_logger", lambda name=None: _Rec())

    async def deep_failure() -> str:
        raise ValueError("boom AKIAIOSFODNN7EXAMPLE")

    await _call(_probe(deep_failure), "deep_failure", {})
    tb = [kw for level, event, kw in seen if level == "debug" and event == "mcp_tool_traceback"]
    assert len(tb) == 1 and tb[0]["tool"] == "deep_failure"
    assert "Traceback (most recent call last)" in tb[0]["traceback"]
    assert "deep_failure" in tb[0]["traceback"] and "ValueError" in tb[0]["traceback"]
    assert "AKIAIOSFODNN7EXAMPLE" not in tb[0]["traceback"]


@pytest.mark.asyncio
async def test_a_deliberate_tool_error_keeps_mcps_own_readable_text():
    from mcp.server.mcpserver.exceptions import ToolError

    async def refuse() -> str:
        raise ToolError("bad argument")

    result = await _call(_probe(refuse), "refuse", {})
    assert _text(result) == "Error executing tool refuse: bad argument"


# ── cancellation is never turned into a result ───────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["plain_tool", "llm"])
@pytest.mark.parametrize("exc", [asyncio.CancelledError, KeyboardInterrupt])
async def test_cancel_and_interrupt_propagate_through_the_door(name, exc):
    async def stop() -> str:
        raise exc()

    server = _probe(stop, name=name)
    with pytest.raises(exc):
        await server.call_tool(name, {})


@pytest.mark.asyncio
async def test_an_external_cancel_of_a_running_routed_call_propagates(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "30")
    started = asyncio.Event()

    async def hang() -> str:
        started.set()
        await asyncio.sleep(30)
        return "never"

    server = _probe(hang, name="llm")
    task = asyncio.create_task(server.call_tool("llm", {}))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── B. the deadline ───────────────────────────────────────────────────────────

def test_deadline_env_parsing(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_MCP_DEADLINE_S", raising=False)
    assert mcp_door.deadline_s() == 90.0
    for raw, want in (("12.5", 12.5), ("0", 0.0), ("-3", 0.0), ("abc", 90.0), ("inf", 90.0), ("nan", 90.0)):
        monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", raw)
        assert mcp_door.deadline_s() == want, raw


def test_no_attempt_cap_outside_an_mcp_call():
    assert mcp_door.attempt_cap_s("ollama/qwen3.5") is None


@pytest.mark.asyncio
async def test_the_attempt_cap_is_for_local_models_only(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "90")
    caps: dict = {}

    async def probe() -> str:
        caps["local"] = mcp_door.attempt_cap_s("ollama/qwen3-coder:30b")
        caps["cloud"] = mcp_door.attempt_cap_s("anthropic/claude-opus-4")
        return "ok"

    await _call(_probe(probe, name="llm"), "llm", {})
    assert caps == {"local": 45.0, "cloud": None}


def test_the_cap_error_is_not_a_timeout_to_the_router():
    """LOCAL-TIMEOUT-1 keys its cooldown on _is_timeout_error; a door cap must not trip it."""
    from llm_router.router import _is_timeout_error

    assert not _is_timeout_error(mcp_door.AttemptCapExceeded("ollama/x", 45.0))


@pytest.mark.asyncio
async def test_deadline_zero_leaves_a_routed_door_unbounded(monkeypatch):
    """M3: ``0`` means off, not "use the default" -- with the default shrunk to 0.2 s, a
    routed door that takes 0.6 s still answers."""
    monkeypatch.setattr(mcp_door, "MCP_DEADLINE_DEFAULT_S", 0.2)
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "0")

    async def slow() -> str:
        await asyncio.sleep(0.6)
        return "answered"

    assert _text(await _call(_probe(slow, name="llm"), "llm", {})) == "answered"
    monkeypatch.delenv("LLM_ROUTER_MCP_DEADLINE_S")  # premise: the shrunk default does bound it
    assert _text(await _call(_probe(slow, name="llm"), "llm", {})).startswith(
        "llm-router error: deadline 0.2s exceeded")


@pytest.mark.asyncio
async def test_real_llm_edit_door_is_deadline_bound(temp_db, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "0.5")

    async def hang(*args, **kwargs):
        await asyncio.sleep(30)

    with patch("llm_router.tools.text.route_and_call", new_callable=lambda: AsyncMock(side_effect=hang)):
        result = await _call(_real_server(), "llm_edit", {"task": "rename x", "files": []})
    assert _text(result).startswith("llm-router error: deadline 0.5s exceeded; tried: no model attempt recorded")


def test_every_route_and_call_door_is_deadline_bound():
    assert {"llm_edit", "llm_text_job", "llm_router_agent_route"} <= mcp_door.DEADLINE_TOOLS


@pytest.mark.timing  # asserts elapsed vs the deadline: serial `timing` CI lane
@pytest.mark.asyncio
async def test_real_llm_door_returns_readable_timeout_within_the_deadline(temp_db, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "0.5")

    async def hang(*args, **kwargs):
        mcp_door.attach_trace(["ollama/qwen3-coder:30b", "ollama/qwen3.8-uncensored"])
        await asyncio.sleep(30)

    t0 = time.monotonic()
    with patch("llm_router.tools.text.route_and_call", new_callable=lambda: AsyncMock(side_effect=hang)):
        result = await _call(_real_server(), "llm", {"prompt": PROMPT, "task": "query"})
    elapsed = time.monotonic() - t0
    assert result.is_error
    assert _text(result) == (
        "llm-router error: deadline 0.5s exceeded; tried: ollama/qwen3-coder:30b, "
        "ollama/qwen3.8-uncensored; local models may be busy (Ollama queue) — retry or use tier=fast"
    )
    assert elapsed < 0.5 + 2.0, elapsed  # deadline + generous epsilon for client/session setup


@pytest.mark.asyncio
async def test_the_deadline_does_not_apply_to_non_routed_tools(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "0.2")

    async def slowish() -> str:
        await asyncio.sleep(0.5)
        return "done"

    result = await _call(_probe(slowish), "slowish", {})
    assert _text(result) == "done"


def _litellm_response(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    resp.citations = []
    return resp


async def _route_door(chain: list[str], calls: list, tracker=None, cooldown: dict | None = None):
    """A ``llm_query``-named tool that routes exactly like the door (real route_and_call);
    only the provider SDK call (litellm.acompletion) and the chain are fakes."""
    tracker = tracker or _healthy_tracker()
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    async def fake_acompletion(**kwargs):
        model = kwargs["model"]
        calls.append(model)
        if "slowish" in model:  # a cloud call that is slow but legitimate (extended thinking)
            await asyncio.sleep(5)
            return _litellm_response("CLOUD-CANARY")
        if "slow" in model:
            await asyncio.sleep(30)
        return _litellm_response("FAST-CANARY")

    async def llm_query(q: str) -> str:
        return (await route_and_call(TaskType.QUERY, q)).content

    with patch("litellm.acompletion", side_effect=fake_acompletion), \
         patch("llm_router.local_context_guard._ps_context_length", return_value=None), \
         patch("llm_router.router._build_and_filter_chain", new_callable=lambda: AsyncMock(return_value=chain)), \
         patch("llm_router.router.get_tracker", return_value=tracker), \
         patch.dict("llm_router.router._local_timeout_at", clear=True), \
         patch("llm_router.router.is_codex_available", return_value=False):
        result = await _call(_probe(llm_query), "llm_query", {"q": PROMPT}, guard_s=20)
        if cooldown is not None:
            from llm_router import router

            cooldown.update(router._local_timeout_at)
        return result


def _healthy_tracker():
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    return tracker


@pytest.mark.timing  # asserts elapsed vs the deadline: serial `timing` CI lane
@pytest.mark.asyncio
async def test_a_hung_attempt_falls_through_to_a_fast_candidate_inside_the_deadline(
        temp_db, mock_env, _no_spend, monkeypatch):
    # 8 s, not less: route_and_call's own setup before the first attempt took 2.7 s on a loaded
    # machine (2026-10-16, load ~80), and the per-attempt cap (4 s) must outlast that setup.
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "8")
    calls: list = []
    cooldown: dict = {}
    tracker = _healthy_tracker()
    t0 = time.monotonic()
    # two local candidates: a mixed chain is re-ordered by the Q&A ordering (D-116), which
    # would put the cloud model first and never reach the hung local one
    result = await _route_door(["ollama/slow-local", "ollama/fast-local"], calls, tracker, cooldown)
    elapsed = time.monotonic() - t0
    assert _text(result) == "FAST-CANARY", _text(result)
    assert len(calls) == 2 and "slow-local" in calls[0] and "fast-local" in calls[1], calls
    assert elapsed < 8.0 + 3.0, elapsed
    # D-154: the cap is not a provider fault -- no LOCAL-TIMEOUT-1 cooldown, no health failure
    assert cooldown == {}, cooldown
    assert all(c.args[:1] != ("ollama",) for c in tracker.record_failure.call_args_list), \
        tracker.record_failure.call_args_list


@pytest.mark.timing  # asserts elapsed vs the deadline: serial `timing` CI lane
@pytest.mark.asyncio
async def test_a_slow_cloud_attempt_is_not_cut_by_the_local_cap(
        temp_db, mock_env, _no_spend, monkeypatch):
    """D-154, scaled: the cloud call takes 5 s of an 8 s deadline (60 s of 90 s live), more
    than the 4 s local cap, and still answers."""
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "8")
    calls: list = []
    result = await _route_door(["openai/slowish-cloud", "gemini/fast-model"], calls)
    assert _text(result) == "CLOUD-CANARY", (_text(result), calls)
    assert calls == ["openai/slowish-cloud"], calls


@pytest.mark.timing  # asserts elapsed vs the deadline: serial `timing` CI lane
@pytest.mark.asyncio
async def test_an_all_slow_chain_returns_the_timeout_text_naming_the_attempts(
        temp_db, mock_env, _no_spend, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MCP_DEADLINE_S", "8")
    calls: list = []
    t0 = time.monotonic()
    result = await _route_door(["ollama/slow-a", "ollama/slow-b", "ollama/slow-c"], calls)
    elapsed = time.monotonic() - t0
    text = _text(result)
    assert result.is_error
    # slow-a is cut at its 4 s cap, slow-b is still running when the 8 s deadline expires
    assert len(calls) == 2 and "slow-a" in calls[0] and "slow-b" in calls[1], calls
    assert text == ("llm-router error: deadline 8s exceeded; tried: ollama/slow-a, ollama/slow-b; "
                    "local models may be busy (Ollama queue) — retry or use tier=fast"), text
    assert elapsed < 8.0 + 3.0, elapsed
