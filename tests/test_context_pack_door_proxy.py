"""Proxy local tier uses the shared context pack and preserves native tools."""

import json

import pytest

from llm_router import context_pack
from llm_router.local_context_guard import check_overflow
from llm_router.proxy import backends, server
from llm_router.proxy.steps import non_system, prev_tools
from llm_router.proxy.translate import to_ollama
from tests.test_proxy import FakeBackend, Upstream, _app, _call, _ollama, _post, _req, policy  # noqa: F401


@pytest.fixture(autouse=True)
def session_context_all(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")


def _spy_pack(monkeypatch, modes=None):
    calls = []
    real = context_pack.build_pack

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        pack = real(*args, **kwargs)
        if modes is not None:
            modes.append(pack.mode)
        return pack

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
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=32768)
    assert out["system"] == backends.CONDENSED_SYSTEM
    assert non_system(out["messages"])[-2:] == turns[-2:]
    assert out["messages"][-1]["content"][0]["content"] == "RESULT-9 " + "y" * 6000
    assert out["messages"][0] == body["messages"][0]
    recent = out["messages"][1:-2]
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


def _pairs_body(rc, n=30):
    messages = [{"role": "user", "content": [{"type": "text", "text": "fix it"}]}]
    for i in range(n):
        messages.extend([
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{i}", "name": "Read",
                 "input": {"file_path": f"/w/{i}.py"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": "z" * rc}]},
        ])
    return dict(_req(), messages=messages)


def test_pack_mode_pins_the_first_user_request_verbatim():
    body = _pairs_body(3000, n=30)
    body["messages"][0]["content"] = [
        {"type": "text", "text": "FIRST-ASK " + "q" * 500}]
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=32768)
    assert out["messages"] != non_system(body["messages"])
    assert out["messages"][0] == body["messages"][0]
    assert out["messages"].count(body["messages"][0]) == 1
    check_overflow(to_ollama(out, "m", num_ctx=32768), num_ctx=32768)


def test_pin_is_dropped_when_it_would_overflow():
    body = _pairs_body(3000, n=30)
    body["messages"][0]["content"] = [
        {"type": "text", "text": "FIRST-ASK " + "q" * 120_000}]
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=32768)
    assert "FIRST-ASK" not in json.dumps(out["messages"])
    assert out["messages"][-2:] == non_system(body["messages"])[-2:]


def test_short_conversation_is_never_pinned_twice(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_FULL_CAP", "1000")
    body = _req()
    turns = non_system(body["messages"])
    text = context_pack.extract_turn_text(turns[0]["content"], keep_tools=True, cap=False)
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=32768)
    assert out["messages"] != turns
    assert sum(json.dumps(message).count(text) for message in out["messages"]) == 1


def test_full_mode_never_overflows_the_native_payload(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_FULL_CAP", str(10**9))
    modes = []
    calls = _spy_pack(monkeypatch, modes)
    sent_full = False
    rejected_full = False
    for rc in range(2600, 3600, 100):
        body = _pairs_body(rc)
        before = len(calls)
        send = backends.apply_trims(
            body, backends.resolve_trims("fast"), target_window=32768)
        assert len(calls) == before + 1
        payload = to_ollama(send, "m", num_ctx=32768)
        check_overflow(payload, num_ctx=32768)
        native = send["messages"] == non_system(body["messages"])
        sent_full |= modes[-1] == "full" and native
        rejected_full |= modes[-1] == "full" and not native
    assert sent_full
    assert rejected_full
    assert len(calls) == len(range(2600, 3600, 100))


def test_full_mode_cap_env(monkeypatch):
    body = _req()
    turns = non_system(body["messages"])
    monkeypatch.setenv("LLM_ROUTER_PROXY_FULL_CAP", "1000")
    assert backends.full_max_tokens() == 1000
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=32768)
    assert out["messages"] != turns
    assert out["messages"][-2:] == turns[-2:]

    monkeypatch.delenv("LLM_ROUTER_PROXY_FULL_CAP")
    assert backends.full_max_tokens() == backends.DEFAULT_FULL_MAX_TOKENS
    out = backends.apply_trims(body, backends.resolve_trims("fast"), target_window=32768)
    assert out["messages"] == turns

    for raw in ("", "abc", "0", "-5"):
        monkeypatch.setenv("LLM_ROUTER_PROXY_FULL_CAP", raw)
        assert backends.full_max_tokens() == backends.DEFAULT_FULL_MAX_TOKENS
