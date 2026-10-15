"""MCP text completions reuse injection and gate session content per attempt."""

import json
import os
from types import SimpleNamespace

import pytest

from llm_router import (
    call_identity,
    context,
    context_injection,
    context_pack,
    failopen,
    local_models,
    northstar,
    providers,
    router,
    session_store,
)
from llm_router.types import LLMResponse, TaskType

MARK = "PRIVATE-TRANSCRIPT-MARKER"
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
    monkeypatch.setattr(
        northstar, "claude_projects_dir",
        lambda: tmp_path / ".claude" / "projects",
    )
    monkeypatch.setattr(call_identity, "call_session_id", lambda: None)


@pytest.fixture
def rig(monkeypatch):
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
    yield seen, config
    context._reset_session_buffers_for_test()


async def _call(prompt="REQ-TEXT", model="ollama/x", **kwargs):
    return await router._call_text(
        model, prompt, None, None, None, TaskType.QUERY, **kwargs,
    )


def _transcript(monkeypatch, tmp_path):
    sid = "mcp-pack-session"
    path = tmp_path / ".claude" / "projects" / "proj" / (sid + ".jsonl")
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(
            json.dumps({"type": message["role"], "message": message}) + "\n"
            for message in HISTORY
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(call_identity, "call_session_id", lambda: sid)
    return sid


@pytest.mark.asyncio
async def test_builds_one_mcp_pack_per_call(monkeypatch, rig):
    seen, _ = rig
    calls = []
    windows = []
    real = context_pack.build_pack

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    def window(model):
        windows.append(model)
        return 16384

    monkeypatch.setattr(context_pack, "build_pack", spy)
    monkeypatch.setattr(local_models, "num_ctx", window)
    await _call(caller_context="CTX-TEXT")
    assert calls == [(("REQ-TEXT",), {
        "session_id": None, "project_root": None, "target_window": 16384,
        "door": "mcp", "project": "", "target_provider": "ollama",
    })]
    assert windows == ["x"]
    assert seen == [[{"role": "system", "content": "[Additional context]\nCTX-TEXT"}, REQUEST]]
    await _call(model="openai/gpt-4o")
    assert len(calls) == 2
    assert calls[1] == (("REQ-TEXT",), {                 # D-101: registry 128k, capped at 64k
        "session_id": None, "project_root": None, "target_window": 65_536,
        "door": "mcp", "project": "", "target_provider": "openai",
    })
    assert windows == ["x"]
    assert seen[1] == [REQUEST]
    await _call(model="openai/synthetic-unlisted-model")   # unknown to the registry: the old 8k
    assert calls[2][1]["target_window"] == 8192


@pytest.mark.asyncio
async def test_caller_context_and_request_are_not_duplicated(rig):
    seen, _ = rig
    await _call(caller_context=None)
    assert seen == [[REQUEST]]
    await _call(caller_context="CTX-TEXT")
    assert seen[1] == [
        {"role": "system", "content": "[Additional context]\nCTX-TEXT"}, REQUEST,
    ]


@pytest.mark.asyncio
async def test_request_is_untruncated(rig):
    seen, _ = rig
    request = "x" * 200_000
    await _call(prompt=request)
    assert seen == [[{"role": "user", "content": request}]]


@pytest.mark.asyncio
async def test_transcript_precedes_request_and_keeps_caller_context(
    monkeypatch, rig, tmp_path,
):
    seen, _ = rig
    _transcript(monkeypatch, tmp_path)
    await _call(caller_context="CTX-TEXT")
    assert seen == [[
        {"role": "system", "content": "[Additional context]\nCTX-TEXT"},
        *HISTORY, REQUEST,
    ]]
    await _call(caller_context=HISTORY[0]["content"])
    assert seen[1] == HISTORY + [REQUEST]


@pytest.mark.asyncio
async def test_missing_transcript_uses_caller_context(monkeypatch, rig):
    seen, _ = rig
    monkeypatch.setattr(call_identity, "call_session_id", lambda: "missing")
    await _call(caller_context="CTX-TEXT")
    assert seen == [[{"role": "system", "content": "[Additional context]\nCTX-TEXT"}, REQUEST]]


@pytest.mark.asyncio
async def test_local_mode_gates_each_attempt(monkeypatch, rig, tmp_path):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    await _call(model="ollama/x")
    await _call(model="openai/gpt-4o")
    assert seen == [HISTORY + [REQUEST], [REQUEST]]


@pytest.mark.asyncio
async def test_local_provider_policy_is_read_at_call_time(monkeypatch, rig, tmp_path):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    monkeypatch.setattr(session_store, "LOCAL_PROVIDERS", ("ollama", "codex"))
    await _call(model="codex/fake")
    assert seen == [HISTORY + [REQUEST]]
    monkeypatch.setattr(session_store, "LOCAL_PROVIDERS", ("ollama",))
    await _call(model="codex/fake")
    assert seen[1] == [REQUEST]


@pytest.mark.asyncio
async def test_weakened_provider_gate_demonstrates_transcript_leak(monkeypatch, rig, tmp_path):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    await _call(model="openai/gpt-4o")
    assert seen == [[REQUEST]]
    monkeypatch.setattr(router, "_pack_target_provider", lambda model: "ollama")
    await _call(model="openai/gpt-4o")
    assert seen[1] == HISTORY + [REQUEST]


@pytest.mark.asyncio
async def test_pack_error_records_and_fails_open(monkeypatch, rig):
    seen, _ = rig
    error = RuntimeError("pack down")
    records = []

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(context_pack, "build_pack", boom)
    monkeypatch.setattr(failopen, "record", lambda *args: records.append(args))
    response = await _call(prompt="[OKF]\nREQ", user_prompt="REQ", caller_context="CTX-TEXT")
    assert response.content == "ok"
    assert seen == [[{"role": "user", "content": "[OKF]\nREQ"}]]
    assert records == [("CHZ-FO-ROUTER-MCP-PACK", error)]


@pytest.mark.asyncio
async def test_injected_prompt_is_reused_across_attempts(monkeypatch, rig, tmp_path):
    seen, _ = rig
    root = tmp_path / "proj"
    root.mkdir()
    calls = []

    def inject(*args, **kwargs):
        calls.append((args, kwargs))
        return "[UNEXPECTED]"

    monkeypatch.setattr(context_injection, "inject", inject)
    # Exercise the two LiteLLM attempts directly: route_and_call's broader
    # budget/broker setup is outside this door rig. Its injection is simulated.
    for model in ("ollama/x", "openai/gpt-4o"):
        await _call(model=model, prompt="[OKF]\nREQ", user_prompt="REQ", scope_root=str(root))
    assert calls == []
    expected = [
        {"role": "system", "content": "<project-knowledge>[OKF]</project-knowledge>"},
        {"role": "user", "content": "REQ"},
    ]
    assert seen == [expected, expected]
    assert all(sum(m["content"].count("[OKF]") for m in sent) == 1 for sent in seen)


@pytest.mark.asyncio
async def test_no_project_uses_raw_request(rig):
    seen, _ = rig
    await _call(prompt="unrelated prompt", user_prompt="REQ")
    assert seen == [[{"role": "user", "content": "REQ"}]]


@pytest.mark.asyncio
async def test_context_disabled_skips_pack(monkeypatch, rig):
    seen, config = rig
    config.context_enabled = False

    def unexpected(*args, **kwargs):
        pytest.fail("context disabled must skip the pack")

    monkeypatch.setattr(context_pack, "build_pack", unexpected)
    await _call(prompt="[OKF]\nREQ", user_prompt="REQ", caller_context="CTX-TEXT")
    assert seen == [[{"role": "user", "content": "[OKF]\nREQ"}]]


async def _seed_context(monkeypatch, summary="EARLIER-SUMMARY"):
    async def summaries(limit=3):
        return [{"summary": summary, "task_types": ["query"]}]

    monkeypatch.setattr(context, "get_recent_session_summaries", summaries)
    monkeypatch.setattr(context, "_resolve_context_identity", lambda *args: ("proj", "sid"))
    monkeypatch.setenv("LLM_ROUTER_CONTEXT_OPTIMIZER", "off")
    buf = context.get_session_buffer("proj", "sid")
    buf.record("user", "BUFFER-Q")
    buf.record("assistant", "BUFFER-A")
    session_store.record_event("sid", "user_prompt", "ACCUMULATOR-Q")
    session_store.record_event("sid", "assistant", "ACCUMULATOR-A", role="assistant")
    return await context.get_recent_session_summaries()


@pytest.mark.asyncio
async def test_missing_transcript_keeps_buffer_accumulator_and_summaries(monkeypatch, rig):
    seen, _ = rig
    summaries = await _seed_context(monkeypatch)
    durable = session_store.build_session_context("sid", query="REQ-TEXT", target_provider="ollama")
    assert "ACCUMULATOR-Q" in durable
    assert "ACCUMULATOR-A" in durable
    await _call()
    expected = "\n\n".join([
        context.format_session_summaries(summaries),
        "[Recent conversation context]\nUser: BUFFER-Q\nAssistant: BUFFER-A",
        durable,
    ])
    assert seen == [[{"role": "system", "content": expected}, REQUEST]]


@pytest.mark.asyncio
async def test_transcript_keeps_only_summaries(monkeypatch, rig, tmp_path):
    seen, config = rig
    summaries = await _seed_context(monkeypatch)
    _transcript(monkeypatch, tmp_path)
    real = router.build_context_messages
    calls = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(router, "build_context_messages", spy)
    await _call()
    assert seen == [[
        {"role": "system", "content": context.format_session_summaries(summaries)},
        *HISTORY, REQUEST,
    ]]
    assert calls[0] == {
        "caller_context": None, "query": "REQ-TEXT", "include_current_session": False,
        "max_session_messages": 5, "max_previous_sessions": 3,
        "max_context_tokens": router._MCP_SUMMARY_ONLY_MAX_TOKENS,
        "is_free_model": True, "target_provider": "ollama", "project_root": None,
    }
    assert router._MCP_SUMMARY_ONLY_MAX_TOKENS == 500
    config.context_max_tokens = 100
    await _call()
    assert calls[1]["max_context_tokens"] == 100


@pytest.mark.asyncio
async def test_transcript_summary_budget_is_bounded(monkeypatch, rig, tmp_path):
    seen, _ = rig
    await _seed_context(monkeypatch, summary="summary word " * 2000)
    _transcript(monkeypatch, tmp_path)

    async def no_compaction(text, **kwargs):
        return text, None

    monkeypatch.setattr(context, "compact_structural", no_compaction)
    await _call()
    summary = seen[0][0]
    assert summary["role"] == "system"
    expected = context._fit_layers_by_priority(
        {"1": context.format_session_summaries([
            {"summary": "summary word " * 2000, "task_types": ["query"]},
        ])},
        router._MCP_SUMMARY_ONLY_MAX_TOKENS * 4, "\n\n",
    )
    assert summary["content"] == expected
    assert len(summary["content"]) <= router._MCP_SUMMARY_ONLY_MAX_TOKENS * 4
    assert seen[0][1:] == HISTORY + [REQUEST]


@pytest.mark.asyncio
async def test_privacy_blocks_accumulator_and_summaries_but_keeps_caller(monkeypatch, rig):
    seen, _ = rig
    await _seed_context(monkeypatch)
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")

    async def unexpected(**kwargs):
        pytest.fail("blocked session context must not be built")

    monkeypatch.setattr(router, "build_context_messages", unexpected)
    await _call(model="openai/gpt-4o", caller_context="CTX-TEXT")
    assert seen == [[
        {"role": "system", "content": "[Additional context]\nCTX-TEXT"}, REQUEST,
    ]]


@pytest.mark.asyncio
async def test_leading_assistants_are_dropped_after_system_block(monkeypatch, rig):
    from dataclasses import replace

    seen, _ = rig
    real = context_pack.build_pack

    def pack(*args, **kwargs):
        return replace(real(*args, **kwargs), recent=[
            {"role": "assistant", "content": "ORPHAN-1"},
            {"role": "assistant", "content": "ORPHAN-2"},
            *HISTORY,
        ])

    monkeypatch.setattr(context_pack, "build_pack", pack)
    await router._call_text(
        "ollama/x", "[OKF]\nREQ-TEXT", "SYS", None, None, TaskType.QUERY,
        user_prompt="REQ-TEXT", caller_context="CTX-TEXT",
    )
    assert seen == [[
        {"role": "system", "content": "SYS"},
        {"role": "system", "content": "<project-knowledge>[OKF]</project-knowledge>"},
        {"role": "system", "content": "[Additional context]\nCTX-TEXT"},
        *HISTORY, REQUEST,
    ]]


@pytest.mark.asyncio
async def test_query_is_separate_from_uncut_caller_context(monkeypatch, rig):
    seen, config = rig
    monkeypatch.setattr(context, "_resolve_context_identity", lambda *args: ("proj", "sid"))
    calls = []

    def durable(*args, **kwargs):
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(session_store, "build_session_context", durable)
    config.context_max_tokens = 1
    caller = "CTX-TEXT" * 1000
    await _call(caller_context=caller)
    assert seen == [[{"role": "system", "content": "[Additional context]\n" + caller}, REQUEST]]
    assert calls[0][1]["query"] == "REQ-TEXT"
    calls.clear()
    await context.build_context_messages(caller_context="legacy", target_provider="ollama")
    assert calls[0][1]["query"] == "legacy"


@pytest.mark.asyncio
async def test_two_attempt_fallback_injects_once_per_turn(
    temp_db, mock_env, monkeypatch, tmp_path,
):
    """D-43(e) through the real dispatch loop: attempt 1 fails, attempt 2 succeeds."""
    from unittest.mock import AsyncMock, patch

    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    injected = []

    def inject(prompt, *args, **kwargs):
        injected.append(prompt)
        return "[OKF-BLOCK]\n" + prompt

    monkeypatch.setattr(context_injection, "inject", inject)
    sent = []

    async def fake(model, messages, **kwargs):
        sent.append((model, messages))
        if len(sent) == 1:
            raise RuntimeError("first provider down")
        return LLMResponse(
            content="ok", model=model, input_tokens=1, output_tokens=1,
            cost_usd=0.0, latency_ms=1.0, provider=model.split("/", 1)[0],
        )

    with patch("llm_router.router._build_and_filter_chain", new=AsyncMock(
            return_value=["openai/gpt-4o-mini", "gemini/gemini-2.5-flash"])), \
         patch("llm_router.providers.call_llm", new=fake):
        await router.route_and_call(
            TaskType.QUERY, "REQ-TEXT", project_root=str(root),
        )
    assert len(sent) == 2
    assert injected == ["REQ-TEXT"]
    for _, messages in sent:
        assert sum(m["content"].count("[OKF-BLOCK]") for m in messages) == 1
        assert messages[-1] == {"role": "user", "content": "REQ-TEXT"}
