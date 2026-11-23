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
    claude_resume.release(key_off)                      # plan() marks a key in flight
    key_off2, entry_off2 = claude_resume.plan(off, "sonnet", "/r", "/r", conversation=None)
    assert entry_off2 is not None
    claude_resume.release(key_off2)
    key_on, entry_on = claude_resume.plan(on, "sonnet", "/r", "/r", conversation=None)
    assert entry_on is None and key_on != key_off      # never resumes a session of the other mode
    claude_resume.release(key_on)
    claude_resume._reset_for_tests()


# -- neutral cwd -----------------------------------------------------------------------------------

import os  # noqa: E402
import stat  # noqa: E402

from tests.test_claude_offload_resume import (  # noqa: E402,F401
    SID as _SID, _call, _clean_store, _res, routed,
)


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def test_isolated_cwd_creates_private_dir(tmp_root):
    path, reason = claude_agent.isolated_cwd()
    assert reason == "" and path is not None and path.startswith(str(tmp_root))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    assert claude_agent.isolated_cwd() == (path, "")            # idempotent


def test_isolated_cwd_refuses_unsafe_dirs(tmp_root, caplog):
    path, _ = claude_agent.isolated_cwd()
    os.chmod(path, 0o777)
    got, reason = claude_agent.isolated_cwd()
    assert got is None and "writable" in reason
    assert "unusable" in caplog.text
    os.rmdir(path)
    open(path, "w").close()                                    # a file in its place
    got, reason = claude_agent.isolated_cwd()
    assert got is None and "not a directory" in reason


def test_subprocess_cwd_overrides_only_the_cwd(monkeypatch):
    seen = {}

    async def _exec(*args, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        return _Proc(json.dumps({"result": "ok", "session_id": SID}).encode())

    monkeypatch.setattr(claude_agent, "find_claude_binary", lambda: "/fake/claude")
    monkeypatch.setattr(claude_agent.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
    asyncio.run(run_claude("hi", structured=True, inject_context=False, subprocess_cwd="/x/neutral"))
    assert seen["cwd"] == "/x/neutral"
    asyncio.run(run_claude("hi", structured=True, inject_context=False))
    assert seen["cwd"] == os.getcwd()


def _set_isolate(monkeypatch, on):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_OFFLOAD_ISOLATE", "true" if on else "false")
    import llm_router.config as config_module
    config_module._config = None


@pytest.mark.asyncio
async def test_router_isolated_uses_neutral_cwd_for_fresh_resumed_and_retry(routed, monkeypatch, tmp_root):  # noqa: F811
    _set_isolate(monkeypatch, True)
    routed.replies += [_res(sid=_SID, total=0.40), _res(sid=_SID, total=0.46),
                       _res(rc=1, sid=None, usage=None, total=None, content="API Error"),
                       _res(sid="new-sid", total=0.43)]
    await _call("one")
    await _call("two")
    await _call("three")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    neutral = claude_agent.isolated_cwd()[0]
    assert [c.resume_session_id for c in routed.sent] == [None, _SID, _SID, None]   # fresh, resumed, failed resume, retry-fresh
    assert all(c.isolate is True and c.subprocess_cwd == neutral for c in routed.sent)
    assert all(getattr(c, "working_dir", None) is None for c in routed.sent)        # context scope untouched
    key = next(iter(claude_resume._STORE))
    assert key[2] == os.path.realpath(neutral) and key[4] is True


@pytest.mark.asyncio
async def test_router_isolated_unsafe_dir_falls_back_to_callers_cwd(routed, monkeypatch, tmp_root):  # noqa: F811
    _set_isolate(monkeypatch, True)
    path, _ = claude_agent.isolated_cwd()
    os.chmod(path, 0o777)
    routed.replies += [_res(sid=_SID, total=0.40)]
    await _call("one")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    assert routed.sent[0].isolate is True and not hasattr(routed.sent[0], "subprocess_cwd")
    assert next(iter(claude_resume._STORE))[2] == os.path.realpath(os.getcwd())


@pytest.mark.asyncio
async def test_router_flag_off_keeps_cwd(routed, monkeypatch, tmp_root):  # noqa: F811
    _set_isolate(monkeypatch, False)
    routed.replies += [_res(sid=_SID, total=0.40)]
    await _call("one")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    c = routed.sent[0]
    assert c.isolate is False and not hasattr(c, "subprocess_cwd") and getattr(c, "working_dir", None) is None
    assert not [n for n in os.listdir(tmp_root) if n.startswith("llm-router-claude-offload")]
    assert next(iter(claude_resume._STORE))[2] == os.path.realpath(os.getcwd())


@pytest.mark.asyncio
async def test_router_isolation_works_with_resume_off(routed, monkeypatch, tmp_root):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_OFFLOAD_RESUME", "false")
    _set_isolate(monkeypatch, True)
    routed.replies += [ClaudeResult(content="a", model="x", exit_code=0, duration_sec=0.1)]
    await _call("one")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    c = routed.sent[0]
    assert c.structured is False and c.resume_session_id is None
    assert c.isolate is True and c.subprocess_cwd == claude_agent.isolated_cwd()[0]


def test_isolated_cwd_refuses_a_dir_under_home(tmp_path, monkeypatch, caplog):
    import tempfile
    home = tmp_path / "fakehome"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(home / "tmp"))
    claude_agent._ISOLATED_CWD_WARNED.clear()
    got, reason = claude_agent.isolated_cwd()
    assert got is None and "$HOME" in reason
    assert not [p for p in (home / "tmp").iterdir()]            # nothing created under $HOME
    assert "unusable" in caplog.text


def test_isolated_cwd_refuses_a_symlink_in_place_of_the_dir(tmp_root):
    claude_agent._ISOLATED_CWD_WARNED.clear()
    target = tmp_root / "elsewhere"
    target.mkdir(mode=0o700)
    os.symlink(target, tmp_root / f"llm-router-claude-offload-{os.getuid()}")
    got, reason = claude_agent.isolated_cwd()
    assert got is None and "not a directory" in reason


def test_isolated_cwd_warns_once_per_process(tmp_root, caplog):
    claude_agent._ISOLATED_CWD_WARNED.clear()
    path, _ = claude_agent.isolated_cwd()
    os.chmod(path, 0o777)
    for _ in range(3):
        assert claude_agent.isolated_cwd()[0] is None
    assert caplog.text.count("unusable") == 1
