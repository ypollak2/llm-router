"""Proxy local tier uses the shared context pack and preserves native tools."""

import json

import pytest

from llm_router import context_pack
from llm_router.proxy import backends, server
from llm_router.proxy.steps import non_system, prev_tools
from tests.test_proxy import FakeBackend, Upstream, _app, _call, _ollama, _post, _req, policy  # noqa: F401


@pytest.fixture(autouse=True)
def session_context_all(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")


def _spy_pack(monkeypatch):
    calls = []
    real = context_pack.build_pack

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(context_pack, "build_pack", spy)
    return calls


def _tool_heavy_body():
    messages = [{"role": "user", "content": "ORIGINAL-ASK"}]
    for i in range(10):
        messages.extend([
            {"role": "assistant", "content": [
                {"type": "text", "text": f"step {i}"},
                {"type": "tool_use", "id": f"t{i}", "name": "Bash",
                 "input": {"command": f"cmd {i}"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": f"RESULT-{i} " + "y" * 6000}]},
        ])
    messages.insert(-1, {"role": "system", "content": "reminder"})
    return {
        "system": "BIG SYSTEM",
        "tools": [
            {"name": name, "description": "d",
             "input_schema": {"type": "object", "properties": {}}}
            for name in ("Read", "Edit", "Write", "Bash")
        ],
        "messages": messages,
    }


async def test_local_tier_builds_the_pack_once_per_request_with_door_proxy(tmp_path, policy, monkeypatch):  # noqa: F811
    calls = _spy_pack(monkeypatch)
    body = _req()
    marker = "REQ-MARK-" + "x" * 49_991
    last_user = [m for m in body["messages"] if m["role"] == "user"][-1]
    result = next(b for b in last_user["content"] if b["type"] == "tool_result")
    result["content"] = marker
    backend = FakeBackend(_ollama(calls=[_call("Read", {"file_path": "/work/repo/a.py"})]))
    app = _app(tmp_path, Upstream(), backend)

    await _post(app, body)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["door"] == "proxy"
    assert isinstance(kwargs["target_window"], int) and kwargs["target_window"] > 0
    assert len(marker) == 50_000 and marker in args[0]
    assert len(backend.calls) == 1
    assert marker in json.dumps(backend.calls[0])

    await _post(app, body)
    assert len(calls) == 2


async def test_policy_kept_request_never_builds_a_pack(tmp_path, monkeypatch):
    async def choose(text, pinned):
        return {"task_type": "code", "complexity": "moderate",
                "model": None, "chain_head": []}

    monkeypatch.setattr(server, "choose_model", choose)
    calls = _spy_pack(monkeypatch)
    backend = FakeBackend(_ollama("Done."))
    await _post(_app(tmp_path, Upstream(), backend), _req())
    assert calls == []
    assert backend.calls == []


def test_tool_heavy_conversation_pack_mode(monkeypatch):
    body = _tool_heavy_body()
    before = json.dumps(body)
    turns = non_system(body["messages"])
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=2048)
    assert out["system"] == backends.CONDENSED_SYSTEM
    assert non_system(out["messages"])[-2:] == turns[-2:]
    assert out["messages"][-1]["content"][0]["content"] == "RESULT-9 " + "y" * 6000
    recent = out["messages"][:-2]
    assert len(recent) == 6
    assert all(isinstance(m["content"], str) for m in recent)
    assert [m["role"] for m in recent] == ["assistant", "user"] * 3  # a6 u6 a7 u7 a8 u8
    assert recent[0]["content"].startswith("step 6")
    assert all("ORIGINAL-ASK" not in m["content"] for m in recent)
    assert any("[tool_use: Bash]" in m["content"] for m in recent)
    assert any("[tool_result]" in m["content"] for m in recent)
    results = [m for m in recent if m["role"] == "user"]
    assert len(results) == 3
    assert all("[…truncated" in m["content"] and len(m["content"]) < 4200 for m in results)
    assert all(m["role"] != "system" for m in out["messages"])
    assert prev_tools(out) == ["Bash"]
    assert json.dumps(body) == before


def test_short_conversation_full_mode_is_native_and_unchanged():
    out = backends.apply_trims(_req(), backends.resolve_trims("fast"), target_window=32768)
    assert out["messages"] == non_system(_req()["messages"])


def test_session_context_off_withholds_history_but_keeps_request(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    body = _tool_heavy_body()
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=32768)
    assert non_system(out["messages"]) == non_system(body["messages"])[-2:]
