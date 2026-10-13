"""MCP text completions use the shared pack and gate the whole chain."""

import json
import os
from types import SimpleNamespace

import pytest

from llm_router import (
    call_identity,
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
    seen = []

    async def fake(model, messages, **kwargs):
        seen.append(messages)
        return LLMResponse(
            content="ok", model=model, input_tokens=1, output_tokens=1,
            cost_usd=0.0, latency_ms=1.0, provider=model.split("/", 1)[0],
        )

    monkeypatch.setattr(providers, "call_llm", fake)
    return seen, config


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
    await _call(caller_context="CTX-TEXT", chain=["ollama/x", "openai/gpt-4o"])
    assert calls == [(("REQ-TEXT",), {
        "session_id": None, "project_root": None, "target_window": 16384,
        "door": "mcp", "context": "CTX-TEXT", "target_provider": "openai",
    })]
    assert windows == ["x"]
    assert seen == [[{"role": "user", "content": "CTX-TEXT"}, REQUEST]]
    await _call(model="openai/gpt-4o")
    assert len(calls) == 2
    assert calls[1] == (("REQ-TEXT",), {
        "session_id": None, "project_root": None, "target_window": 8192,
        "door": "mcp", "context": None, "target_provider": "openai",
    })
    assert windows == ["x"]
    assert seen[1] == [REQUEST]


@pytest.mark.asyncio
async def test_caller_context_and_request_are_not_duplicated(rig):
    seen, _ = rig
    await _call(caller_context=None)
    assert seen == [[REQUEST]]
    await _call(caller_context="CTX-TEXT")
    assert seen[1] == [
        {"role": "user", "content": "CTX-TEXT"}, REQUEST,
    ]


@pytest.mark.asyncio
async def test_request_is_untruncated(rig):
    seen, _ = rig
    request = "x" * 200_000
    await _call(prompt=request)
    assert seen == [[{"role": "user", "content": request}]]


@pytest.mark.asyncio
async def test_transcript_precedes_request_and_supersedes_context(
    monkeypatch, rig, tmp_path,
):
    seen, _ = rig
    _transcript(monkeypatch, tmp_path)
    await _call(caller_context="CTX-TEXT")
    assert seen == [HISTORY + [REQUEST]]


@pytest.mark.asyncio
async def test_missing_transcript_uses_caller_context(monkeypatch, rig):
    seen, _ = rig
    monkeypatch.setattr(call_identity, "call_session_id", lambda: "missing")
    await _call(caller_context="CTX-TEXT")
    assert seen == [[{"role": "user", "content": "CTX-TEXT"}, REQUEST]]


@pytest.mark.asyncio
async def test_local_mode_mixed_chain_withholds_transcript(
    monkeypatch, rig, tmp_path,
):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    await _call(chain=["ollama/x", "openai/gpt-4o"])
    assert seen == [[REQUEST]]


@pytest.mark.asyncio
async def test_local_mode_local_chain_keeps_transcript(monkeypatch, rig, tmp_path):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    await _call(chain=["ollama/x"])
    assert seen == [HISTORY + [REQUEST]]


@pytest.mark.asyncio
async def test_local_provider_policy_is_read_at_call_time(
    monkeypatch, rig, tmp_path,
):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    chain = ["codex/fake", "ollama/x"]
    monkeypatch.setattr(session_store, "LOCAL_PROVIDERS", ("ollama", "codex"))
    await _call(chain=chain)
    assert seen == [HISTORY + [REQUEST]]
    monkeypatch.setattr(
        session_store, "LOCAL_PROVIDERS",
        tuple(p for p in session_store.LOCAL_PROVIDERS if p != "codex"),
    )
    await _call(chain=chain)
    assert seen[1] == [REQUEST]


@pytest.mark.asyncio
async def test_current_model_is_considered_when_absent_from_chain(
    monkeypatch, rig, tmp_path,
):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    await _call(model="openai/gpt-4o", chain=["ollama/x"])
    assert seen == [[REQUEST]]


@pytest.mark.asyncio
async def test_weakened_chain_gate_demonstrates_transcript_leak(
    monkeypatch, rig, tmp_path,
):
    seen, _ = rig
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    _transcript(monkeypatch, tmp_path)
    chain = ["ollama/x", "openai/gpt-4o"]
    await _call(chain=chain)
    assert seen == [[REQUEST]]
    monkeypatch.setattr(
        router, "_pack_target_provider",
        lambda model, chain: chain[0].split("/", 1)[0],
    )
    await _call(chain=chain)
    assert seen[1] == HISTORY + [REQUEST]
    assert any(MARK in message["content"] for message in seen[1])


@pytest.mark.asyncio
async def test_pack_error_records_and_fails_open(monkeypatch, rig):
    seen, _ = rig
    error = RuntimeError("pack down")
    records = []

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(context_pack, "build_pack", boom)
    monkeypatch.setattr(failopen, "record", lambda *args: records.append(args))
    response = await _call(caller_context="CTX-TEXT")
    assert response.content == "ok"
    assert seen == [[REQUEST]]
    assert records == [("CHZ-FO-ROUTER-MCP-PACK", error)]


@pytest.mark.asyncio
async def test_injected_prompt_is_not_doubled(monkeypatch, rig, tmp_path):
    seen, _ = rig
    root = tmp_path / "proj"
    root.mkdir()
    calls = []

    def project(request, project_root, session_id):
        calls.append((request, project_root, session_id))
        return "[OKF]"

    monkeypatch.setattr(context_pack, "_project", project)
    await _call(prompt="[OKF]\nREQ", user_prompt="REQ", scope_root=str(root))
    assert calls == [("REQ", str(root), None)]
    assert seen == [[
        {"role": "system", "content": "<project-knowledge>[OKF]</project-knowledge>"},
        {"role": "user", "content": "REQ"},
    ]]
    assert sum(m["content"].count("[OKF]") for m in seen[0]) == 1


@pytest.mark.asyncio
async def test_no_project_keeps_original_injected_prompt(rig):
    seen, _ = rig
    await _call(prompt="[OKF]\nREQ", user_prompt="REQ")
    assert seen == [[{"role": "user", "content": "[OKF]\nREQ"}]]


@pytest.mark.asyncio
async def test_context_disabled_skips_pack(monkeypatch, rig):
    seen, config = rig
    config.context_enabled = False

    def unexpected(*args, **kwargs):
        pytest.fail("context disabled must skip the pack")

    monkeypatch.setattr(context_pack, "build_pack", unexpected)
    await _call(prompt="[OKF]\nREQ", user_prompt="REQ", caller_context="CTX-TEXT")
    assert seen == [[{"role": "user", "content": "[OKF]\nREQ"}]]
