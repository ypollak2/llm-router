"""Optional isolated Claude CLI offload: argv flags and resume-key separation."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from llm_router import claude_agent, claude_resume
from llm_router.claude_agent import ClaudeResult, run_claude
from llm_router.config import RouterConfig

SID = "11111111-2222-3333-4444-555555555555"
FLAGS = ["--setting-sources", "project,local", "--strict-mcp-config"]


class _Proc:
    def __init__(self, stdout: bytes):
        self.returncode = 0
        self._out = stdout

        async def _gen(data):
            for line in data.splitlines(keepends=True):
                yield line

        self.stdout = _gen(self._out)
        self.stderr = _gen(b"")

    async def wait(self):
        return 0


@pytest.fixture
def cli(monkeypatch):
    calls = []

    async def _exec(*args, **kwargs):
        calls.append(list(args))
        return _Proc(json.dumps({"result": "ok", "session_id": SID, "usage": {}}).encode())

    monkeypatch.setattr(claude_agent, "find_claude_binary", lambda: "/fake/claude")
    monkeypatch.setattr(claude_agent.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
    return calls


def _run(**kw):
    return asyncio.run(run_claude("hi", model="sonnet", structured=True, inject_context=False, **kw))


def test_flag_off_argv_has_no_isolation_flags(cli):
    _run()
    _run(resume_session_id=SID)
    for argv in cli:
        assert "--setting-sources" not in argv and "--strict-mcp-config" not in argv
    assert cli[0] == ["/fake/claude", "-p", "hi", "--output-format", "json", "--model",
                      claude_agent._model_alias("sonnet")]


def test_flag_on_fresh_and_resumed_calls_carry_isolation_flags(cli):
    _run(isolate=True)
    _run(isolate=True, resume_session_id=SID)
    for argv in cli:
        i = argv.index("--setting-sources")
        assert argv[i:i + 3] == FLAGS
    assert "--resume" not in cli[0] and cli[1][cli[1].index("--resume") + 1] == SID


def test_config_default_is_off():
    assert RouterConfig.model_fields["llm_router_claude_offload_isolate"].default is False
    assert claude_resume.isolate_enabled(SimpleNamespace()) is False


def test_resume_key_separates_by_isolation_mode(monkeypatch):
    claude_resume._reset_for_tests()
    monkeypatch.setattr(claude_resume, "host_session_id", lambda: "sess-A")
    base = dict(llm_router_claude_offload_resume=True)
    off = SimpleNamespace(**base, llm_router_claude_offload_isolate=False)
    on = SimpleNamespace(**base, llm_router_claude_offload_isolate=True)
    res = ClaudeResult(content="ok", model="m", exit_code=0, duration_sec=0.1, session_id=SID,
                       usage={"input": 1, "cache_read": 0, "cache_write": 10, "output": 1},
                       total_cost_usd=0.1)
    key_off, entry = claude_resume.plan(off, "sonnet", "/r", "/r", conversation=None)
    assert entry is None
    claude_resume.settle(key_off, None, res)
    assert claude_resume.plan(off, "sonnet", "/r", "/r", conversation=None)[1] is not None
    key_on, entry_on = claude_resume.plan(on, "sonnet", "/r", "/r", conversation=None)
    assert entry_on is None and key_on != key_off      # never resumes a session of the other mode
    claude_resume._reset_for_tests()
