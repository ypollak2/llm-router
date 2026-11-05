# ruff: noqa: F401, F811  (pytest fixture env is imported from test_proxy_local_gate and used by name)
"""The opt-in look lane: pure checks, then the proxy app with the local-gate test harness."""
import asyncio
import copy
import json

import pytest

from llm_router.env_registry import ENV_REGISTRY
from llm_router.proxy import backends, local_gate as gate
from llm_router.proxy import server as ps
from llm_router.proxy.server import ProxyConfig
from tests.test_proxy_local_gate import (
    env, Backend, BARE, _elig, _lane, _req_after, _first_call,
    _ollama, _call, _post,
)


@pytest.mark.parametrize("previous", ["exec", "technical_op", "edit", None])
def test_lane_off_and_on(previous):
    row = {"step_class": "continuation", "prev_tool_class": previous}
    old = ("tech_op", "simple") if previous == "technical_op" else gate.R_LANE_UNMAPPED
    assert gate.lane_of(row, None) == old
    assert gate.lane_of(row, None, look=False) == old
    assert gate.lane_of(row, None, look=True) == (
        ("look", "simple") if previous not in (None, "edit") else gate.R_LANE_UNMAPPED
    )


@pytest.mark.parametrize("look", [False, True])
def test_non_continuation(look):
    assert gate.lane_of({"step_class": "turn_first", "prev_tool_class": "exec"},
                        None, look=look) == gate.R_NOT_CONTINUATION


def test_breaker():
    assert gate.breaker_class("look") == ("proxy_local", "look")


def call(name, inp=None):
    return {"type": "tool_use", "id": "tool_1", "name": name, "input": inp or {}}


def test_read_drops_text():
    read = call("Read", {"file_path": "/work/a.py"})
    message = {"role": "assistant", "content": [{"type": "text", "text": "Reading"}, read],
               "stop_reason": "end_turn"}
    before = copy.deepcopy(message)
    result, reason = gate.look_check(message)
    assert reason is None
    assert result == dict(message, content=[read], stop_reason="tool_use")
    assert result is not message
    assert message == before


@pytest.mark.parametrize("content,reason", [
    ([call("Bash", {"command": "git log -5 --oneline"})], None),
    ([call("Bash", {"command": "echo bad > file"})], gate.R_NOT_A_LOOK),
    ([call("Edit", {"file_path": "/work/a.py"})], gate.R_NOT_A_LOOK),
    ([{"type": "text", "text": "done"}], gate.R_NOT_A_LOOK),
    ([call("hand_to_claude")], gate.R_LOCAL_HANDOFF),
    ([call("Read", {"file_path": "/work/a.py"}), call("hand_to_claude")], gate.R_LOCAL_HANDOFF),
    ([], gate.R_NOT_A_LOOK),
    ([None], gate.R_NOT_A_LOOK),
    ([{"type": "tool_use", "name": "Read", "input": "bad"}], gate.R_NOT_A_LOOK),
])
def test_look_check(content, reason):
    result, actual = gate.look_check({"content": content})
    assert actual == reason
    if reason:
        assert result is None
    else:
        assert result == {"content": content, "stop_reason": "tool_use"}


@pytest.mark.parametrize("message", [None, [], "bad", {}, {"content": None}])
def test_malformed(message):
    assert gate.look_check(message) == (None, gate.R_NOT_A_LOOK)


def test_trim_tools_and_prompt():
    offered = [{"name": n, "description": "original " + n, "input_schema": {"type": "object"}}
               for n in ("Edit", "Bash", "LS", "Glob", "Grep", "Read", "Write")]
    body = {"system": "old", "tools": offered, "messages": []}
    before = copy.deepcopy(body)
    result = backends._trim_look(body)
    assert [t["name"] for t in result["tools"]] == list(backends.LOOK_TOOLS) + ["hand_to_claude"]
    assert result["tools"][:-1] == [next(t for t in offered if t["name"] == n)
                                   for n in backends.LOOK_TOOLS]
    assert result["system"] == backends.LOOK_SYSTEM
    assert body == before
    assert backends.DEFAULT_TRIM == "fast"
    assert backends.resolve_trims("look") == [backends._trim_look]
    assert [t["name"] for t in backends._trim_look({"tools": [offered[0]]})["tools"]] == ["hand_to_claude"]


def test_trim_packs_history_and_window(monkeypatch):
    turns = [{"role": "user", "content": "Find the implementation"}]
    packed = [{"role": "user", "content": "packed"}]
    def pack(actual, window, shell):
        assert actual == turns
        assert window == 4096
        assert shell["system"] == backends.LOOK_SYSTEM
        return "layers", packed
    monkeypatch.setattr(backends, "_pack_messages", pack)
    result = backends.apply_trims({"messages": turns}, [backends._trim_look], target_window=4096)
    assert result["messages"] == packed
    assert result["system"] == "layers\n\n" + backends.LOOK_SYSTEM


@pytest.mark.parametrize("value,expected", [("on", True), ("off", False)])
def test_environment(monkeypatch, value, expected):
    monkeypatch.setenv("LLM_ROUTER_PROXY_LOCAL_LOOK", value)
    assert ProxyConfig.from_env().local_look is expected
    assert ENV_REGISTRY["LLM_ROUTER_PROXY_LOCAL_LOOK"] == ENV_REGISTRY["LLM_ROUTER_PROXY_LOCAL_GATE"]


@pytest.mark.parametrize("mode", ["compaction", "serve"])
def test_look_refuses_local_agent(env, mode):
    config = ({"local_agent": ps.LocalAgentConfig()} if mode == "compaction"
              else {"serve": ps.local_mode.SERVE_LOCAL_AGENT, "model": "ollama/test"})
    with pytest.raises(ValueError, match="--local-look.*local-agent"):
        env.app(Backend(), local_look=True, **config)


@pytest.mark.parametrize("flags", [["--local-agent"], ["--serve", "local-agent"]])
def test_cli_look_refuses_local_agent(monkeypatch, capsys, flags):
    monkeypatch.setattr(ProxyConfig, "from_env", classmethod(lambda cls: ProxyConfig()))
    monkeypatch.setattr(ps, "_local_agent_preflight", lambda cfg: pytest.fail("must refuse before preflight"))
    assert ps.cmd_proxy(["--local-look", "on", "--local-gate", "on",
                         "--steps", "continuation", *flags]) == 2
    error = capsys.readouterr().err
    assert "--local-look on cannot combine" in error
    assert "Traceback" not in error





def look_env(env):
    env.write(_elig((BARE, "look", "simple", _lane())))


def exec_request():
    return _req_after("Bash", {"command": "pytest -q"})


async def test_look_flag_off_preserves_exec_forwarding(env):
    backend = Backend()
    await _post(env.app(backend), exec_request())
    row = env.rows()[0]
    assert row["decision"] == "forwarded"
    assert row["reason"] == "lane_unmapped"
    assert "served_via" not in row and "okf" not in row
    assert backend.calls == []


async def test_look_read_served_with_restricted_prompt(env, monkeypatch):
    look_env(env)
    attaches = []
    monkeypatch.setattr(ps.okf_context, "attach", lambda *a, **kw: attaches.append(a))
    def forbidden(*a, **kw):
        pytest.fail("look must bypass edit/capability handling")
    monkeypatch.setattr(ps.la_capability, "check_reply", forbidden)
    backend = Backend(_ollama(calls=[_call("Read", {"file_path": "/work/a.py"})]))
    body = exec_request()
    before = copy.deepcopy(body)
    response = await _post(env.app(backend, local_look=True), body)
    assert response.status_code == 200 and not env.up.requests
    row = env.rows()[0]
    assert row["decision"] == "served" and row["served_via"] == "look"
    assert row["okf"] == {"status": "skipped_look"} and attaches == []
    sent = backend.calls[0]
    names = {t["name"] for t in sent["tools"]}
    assert names <= set(backends.LOOK_TOOLS) | {"hand_to_claude"}
    assert {"Read", "hand_to_claude"} <= names
    assert backends.LOOK_SYSTEM in sent["system"]
    assert body == before


async def test_look_handoff_original_body_and_healthy_backend(env, monkeypatch):
    look_env(env)
    outcomes = []
    original = ps.BackendHealth.record
    def record(self, model, err, reason, elapsed):
        result = original(self, model, err, reason, elapsed)
        outcomes.append((err, reason, self._get(model).streak, self.is_open(model)))
        return result
    monkeypatch.setattr(ps.BackendHealth, "record", record)
    backend = Backend(_ollama(calls=[_call("hand_to_claude", {})]))
    body = exec_request()
    await _post(env.app(backend, local_look=True), body)
    row = env.rows()[0]
    assert row["decision"] == "forwarded" and row["reason"] == "local_handoff"
    assert row["local_gate"]["reason"] == "local_handoff"
    assert env.up.requests[0].content == json.dumps(body).encode()
    assert outcomes == [(None, None, 0, False)]


@pytest.mark.parametrize("name,inp", [
    ("Edit", {"file_path": "/work/a.py", "old_string": "a", "new_string": "b"}),
    ("Bash", {"command": "echo bad > file"}),
])
async def test_look_non_read_reply_forwards(env, name, inp):
    look_env(env)
    # Inject a valid backend message directly: this tests the post-backend
    # guard even for tools absent from the offered schemas.
    class ReplyBackend(Backend):
        async def complete(self, body, timeout_s):
            self.calls.append(body)
            return {"id": "test", "content": [call(name, inp)]}, None, {}
    backend = ReplyBackend()
    body = exec_request()
    await _post(env.app(backend, local_look=True), body)
    row = env.rows()[0]
    assert row["decision"] == "forwarded" and row["reason"] == "not_a_look"
    assert row["local_gate"]["reason"] == "not_a_look"
    assert env.up.requests[0].content == json.dumps(body).encode()


async def test_look_busy_and_error_releases_counter(env):
    look_env(env)
    entered, release = asyncio.Event(), asyncio.Event()
    class BlockingBackend(Backend):
        async def complete(self, body, timeout_s):
            if not self.calls:
                self.calls.append(body)
                entered.set()
                await release.wait()
                raise RuntimeError("test backend error")
            return await super().complete(body, timeout_s)
    backend = BlockingBackend(_ollama(calls=[_call("Read", {"file_path": "/work/a.py"})]))
    app = env.app(backend, local_look=True)
    first = asyncio.create_task(_post(app, exec_request()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await _post(app, exec_request())
        row = env.rows()[0]
        assert row["decision"] == "forwarded" and row["reason"] == "local_busy"
        assert row["local_gate"]["reason"] == "local_busy"
        assert len(backend.calls) == 1
    finally:
        release.set()
        await first
    assert env.rows()[1]["local_gate"]["reason"] == "local_failed"
    # A new admitted call demonstrates the counter returned to zero.
    await _post(app, exec_request())
    assert env.rows()[-1]["decision"] == "served"
    assert len(backend.calls) == 2


async def test_look_first_turn_still_forwarded(env):
    look_env(env)
    backend = Backend()
    await _post(env.app(backend, local_look=True), _first_call())
    row = env.rows()[0]
    assert row["decision"] == "forwarded" and row["reason"] == "not_eligible"
    assert backend.calls == []


def test_a_look_reply_gets_more_output_room_than_an_easy_step():
    body = backends._trim_look({"system": "x", "messages": [], "tools": []})
    assert backends.num_predict_for(body) == backends.NUM_PREDICT_LOOK
    assert backends.num_predict_for({"system": "other", "messages": []}) == backends.NUM_PREDICT_EASY


def test_look_lane_caps_a_run_of_local_steps_and_off_leaves_the_setting_alone():
    assert ps._guard_limit(ProxyConfig(local_look=True, local_gate=True, loop_max_consecutive=8)) == 2
    assert ps._guard_limit(ProxyConfig(local_look=True, local_gate=True, loop_max_consecutive=1)) == 1
    assert ps._guard_limit(ProxyConfig(local_look=False, loop_max_consecutive=8)) == 8
