"""Claude CLI offload resumes ONE CLI session per llm-router session (prompt-cached history).

The CLI output replayed here has the real ``claude -p --output-format json`` shape: ``result``,
``session_id``, ``is_error``, ``total_cost_usd`` (CUMULATIVE under ``--resume``), ``usage``
(PER CALL), ``modelUsage``. The subprocess is always mocked; no real ``claude`` ever runs.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_router import claude_agent, claude_resume
from llm_router.claude_agent import ClaudeResult, run_claude

SID = "11111111-2222-3333-4444-555555555555"


def _doc(result="pong", sid=SID, total=0.40, usage=None, is_error=False):
    return {
        "type": "result", "subtype": "success", "is_error": is_error, "result": result,
        "session_id": sid, "total_cost_usd": total,
        "usage": usage if usage is not None else {
            "input_tokens": 12, "cache_creation_input_tokens": 9000,
            "cache_read_input_tokens": 0, "output_tokens": 300},
        "modelUsage": {"claude-opus-4-8": {"costUSD": total}},
    }


def _usage(inp=5, write=100, read=9000, out=200):
    return {"input_tokens": inp, "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read, "output_tokens": out}


class _Proc:
    def __init__(self, stdout: bytes, rc=0):
        self.returncode = rc

        async def _out():
            for line in stdout.splitlines(keepends=True):
                yield line

        async def _err():
            return
            yield  # pragma: no cover

        self.stdout, self.stderr = _out(), _err()

    async def wait(self):
        return self.returncode


@pytest.fixture
def cli(monkeypatch):
    """Mock the subprocess; ``cli.queue`` holds the stdout of each next call, ``cli.calls`` the argv/cwd."""
    state = SimpleNamespace(queue=[], calls=[])

    async def _exec(*args, **kwargs):
        state.calls.append(SimpleNamespace(argv=list(args), cwd=kwargs.get("cwd")))
        return _Proc(state.queue.pop(0))

    monkeypatch.setattr(claude_agent, "find_claude_binary", lambda: "/fake/claude")
    monkeypatch.setattr(claude_agent.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
    injected = []

    def _inject(prompt, **kw):
        injected.append(prompt)
        return "<OKF>\n" + prompt

    monkeypatch.setattr("llm_router.context_injection.inject", _inject)
    state.injected = injected
    return state


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch):
    claude_resume._reset_for_tests()
    monkeypatch.setattr(claude_resume, "host_session_id", lambda: "sess-A")
    yield
    claude_resume._reset_for_tests()


def _cfg(**over):
    base = dict(llm_router_claude_offload_resume=True, llm_router_claude_offload_resume_ttl_s=270,
                llm_router_claude_offload_resume_max_ctx_tokens=150_000)
    base.update(over)
    return SimpleNamespace(**base)


def _res(sid=SID, total=0.40, usage=None, rc=0, content="ok"):
    return ClaudeResult(content=content, model="opus", exit_code=rc, duration_sec=0.1, session_id=sid,
                        usage=usage if usage is not None else {"input": 5, "cache_read": 0,
                                                              "cache_write": 9000, "output": 300},
                        total_cost_usd=total)


# -- run_claude: structured / resume argv and parsing ---------------------------------------------

def test_first_call_records_session_id_and_real_usage(cli):
    cli.queue.append(json.dumps(_doc(result="line one\nline two")).encode())
    events = []

    async def _ev(kind, text):
        events.append(text)

    r = asyncio.run(run_claude("hello", model="opus", structured=True, on_event=_ev))
    argv = cli.calls[0].argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--resume" not in argv
    assert r.success and r.content == "line one\nline two"
    assert r.session_id == SID
    assert r.usage == {"input": 12, "cache_write": 9000, "cache_read": 0, "output": 300}
    assert r.total_cost_usd == 0.40
    assert events == ["line one", "line two"]            # streaming notifications still fire
    assert cli.injected == ["hello"]                    # first turn injects as before


def test_resumed_call_sends_resume_flag_and_skips_inject(cli):
    cli.queue.append(json.dumps(_doc(total=0.46, usage=_usage())).encode())
    r = asyncio.run(run_claude("only the new request", model="opus", structured=True,
                               resume_session_id=SID, inject_context=False))
    argv = cli.calls[0].argv
    assert argv[argv.index("--resume") + 1] == SID
    assert "--no-session-persistence" not in argv
    assert argv[argv.index("-p") + 1] == "only the new request"   # not re-folded, not re-injected
    assert cli.injected == []
    assert r.usage == {"input": 5, "cache_write": 100, "cache_read": 9000, "output": 200}


def test_text_mode_is_unchanged_without_structured(cli):
    cli.queue.append(b"pong\n")
    r = asyncio.run(run_claude("hi", model="opus"))
    argv = cli.calls[0].argv
    assert argv[argv.index("--output-format") + 1] == "text"
    assert r.content == "pong" and r.session_id is None and r.usage is None and r.total_cost_usd is None


def test_is_error_document_fails_the_call_and_drops_ids(cli):
    cli.queue.append(json.dumps(_doc(result="API Error: refused", is_error=True)).encode())
    r = asyncio.run(run_claude("x", model="opus", structured=True))
    assert not r.success and r.session_id is None and r.usage is None and r.total_cost_usd is None


def test_unparseable_output_has_no_usage_not_zero_usage(cli):
    cli.queue.append(b"not json at all\n")
    r = asyncio.run(run_claude("x", model="opus", structured=True))
    assert r.success and r.content == "not json at all"
    assert r.usage is None and r.session_id is None


def test_partial_usage_block_is_unknown_not_zero(cli):
    d = _doc()
    del d["usage"]["cache_read_input_tokens"]
    cli.queue.append(json.dumps(d).encode())
    r = asyncio.run(run_claude("x", model="opus", structured=True))
    assert r.usage is None            # a missing count must not read as a measured 0


# -- claude_resume: plan / settle ------------------------------------------------------------------

def _plan(cfg=None, model="opus", scope="/repo", cwd="/repo", conv=None, now=None):
    return claude_resume.plan(cfg or _cfg(), model, scope, cwd, conversation=conv, now=now)


def test_second_call_resumes_and_cost_is_the_delta_of_the_cumulative_total():
    key, entry = _plan(now=100.0)
    assert entry is None                                      # first turn: no id yet
    s1 = claude_resume.settle(key, entry, _res(total=0.40), now=100.0)
    assert s1.api_cost_usd == pytest.approx(0.40) and not s1.resumed

    key2, entry2 = _plan(now=130.0)
    assert key2 == key and entry2 is not None and entry2.session_id == SID
    # resumed: usage is per call, but total_cost_usd is cumulative for the session
    s2 = claude_resume.settle(key2, entry2, _res(total=0.46, usage={"input": 5, "cache_read": 9000,
                                                                    "cache_write": 100, "output": 200}), now=130.0)
    assert s2.resumed and not s2.retry
    assert s2.api_cost_usd == pytest.approx(0.06)             # 0.46 - 0.40, not 0.46

    _, entry3 = _plan(now=160.0)
    s3 = claude_resume.settle(key, entry3, _res(total=0.50), now=160.0)
    assert s3.api_cost_usd == pytest.approx(0.04)


def test_cost_unknown_when_total_missing_or_went_backwards():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(total=0.40), now=0.0)
    _, e = _plan(now=1.0)
    assert claude_resume.settle(key, e, _res(total=None), now=1.0).api_cost_usd is None
    key, _ = _plan(now=2.0)       # entry kept (last total now unknown)
    _, e = _plan(now=2.0)
    assert e is not None
    assert claude_resume.settle(key, e, _res(total=0.30), now=2.0).api_cost_usd is None   # 0.30 < unknown prior


def test_fallback_when_resumed_call_errors():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    _, e = _plan(now=1.0)
    s = claude_resume.settle(key, e, _res(rc=1, sid=None, usage=None, total=None, content="boom"), now=1.0)
    assert s.retry is True
    assert _plan(now=2.0)[1] is None                          # id forgotten


def test_timeout_on_resume_forgets_the_session_but_does_not_retry():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    _, e = _plan(now=1.0)
    s = claude_resume.settle(key, e, _res(rc=124, sid=None, usage=None, total=None), now=1.0)
    assert s.retry is False
    assert _plan(now=2.0)[1] is None


def test_fallback_on_session_id_mismatch_discards_the_answer_and_pauses_resume():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(total=0.40), now=0.0)
    _, e = _plan(now=1.0)
    s = claude_resume.settle(key, e, _res(sid="other-session", total=0.48), now=1.0)
    assert s.retry is True
    assert s.discarded_cost_usd == pytest.approx(0.08)        # the thrown-away call still cost something
    # A CLI that rewrites ids on resume would double every call: resume stays off for a while.
    assert _plan(now=2.0) == (None, None)


def test_cap_by_age():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    assert _plan(now=269.0)[1] is not None
    assert _plan(now=271.0)[1] is None                        # > 270 s: the 5-min cache is gone


def test_cap_by_context_tokens():
    key, _ = _plan(now=0.0)
    big = {"input": 10, "cache_read": 140_000, "cache_write": 9_000, "output": 2_000}   # 151,010
    claude_resume.settle(key, None, _res(usage=big), now=0.0)
    assert _plan(now=1.0)[1] is None
    key, _ = _plan(cfg=_cfg(llm_router_claude_offload_resume_max_ctx_tokens=200_000), now=1.0)
    claude_resume.settle(key, None, _res(usage=big), now=1.0)
    assert _plan(cfg=_cfg(llm_router_claude_offload_resume_max_ctx_tokens=200_000), now=2.0)[1] is not None


def test_session_without_measured_usage_is_never_resumed():
    key, _ = _plan(now=0.0)
    r = _res()
    r.usage = None
    claude_resume.settle(key, None, r, now=0.0)
    assert _plan(now=1.0)[1] is None


def test_keys_separate_by_cwd_model_scope_and_llm_router_session(monkeypatch):
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    assert _plan(now=1.0)[1] is not None
    assert _plan(cwd="/other", now=1.0)[1] is None            # CLI finds a session by cwd
    assert _plan(model="sonnet", now=1.0)[1] is None
    assert _plan(scope="/elsewhere", now=1.0)[1] is None
    monkeypatch.setattr(claude_resume, "host_session_id", lambda: "sess-B")
    assert _plan(now=1.0)[1] is None                          # another llm-router session


def test_flag_off_conversation_and_no_session_never_resume(monkeypatch):
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    assert _plan(cfg=_cfg(llm_router_claude_offload_resume=False), now=1.0) == (None, None)
    assert _plan(conv=[{"role": "user", "content": "hi"}], now=1.0) == (None, None)
    assert _plan(cfg=SimpleNamespace(), now=1.0) == (None, None)        # a config that lacks the flag
    monkeypatch.setattr(claude_resume, "host_session_id", lambda: None)
    assert _plan(now=1.0) == (None, None)


def test_fresh_call_without_a_key_still_reports_its_own_cost():
    s = claude_resume.settle(None, None, _res(total=0.12))
    assert s.api_cost_usd == pytest.approx(0.12) and not s.resumed
    assert claude_resume.settle(None, None, _res(rc=1, total=None)).api_cost_usd is None


def test_host_session_id_ignores_the_machine_wide_pointer(monkeypatch):
    monkeypatch.undo()           # use the real function, not the fixture's stub
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    called = MagicMock(return_value="POINTER")
    monkeypatch.setattr("llm_router.session_store.resolve_session_id", called)
    monkeypatch.setattr("llm_router.call_identity.call_session_id", lambda: None)
    assert claude_resume.host_session_id() is None
    called.assert_not_called()
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-env")
    assert claude_resume.host_session_id() == "from-env"


# -- router dispatch: the real branch in router.py, with run_claude replaced ----------------------

@pytest.fixture
def routed(temp_db, mock_env, monkeypatch):
    """Subscription mode, only the Claude CLI reachable; ``routed.sent`` records each run_claude call."""
    from llm_router import router as router_module

    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.1)
    monkeypatch.setattr(router_module, "claude_offload_available", lambda config: True)
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setattr("llm_router.discover.get_cached_ollama_models", lambda: [])
    import llm_router.config as config_module
    config_module._config = None

    state = SimpleNamespace(sent=[], replies=[])

    async def _fake(prompt, model=None, **kw):
        state.sent.append(SimpleNamespace(prompt=prompt, model=model, **kw))
        return state.replies.pop(0)

    monkeypatch.setattr(router_module, "run_claude", _fake)

    async def _fold(prompt, provider, caller_context, config, project_root=None, **kw):
        return f"<FOLDED HISTORY>\n{prompt}"

    monkeypatch.setattr(router_module, "_cli_prompt_with_context", _fold)
    return state


async def _call(prompt):
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType
    return await route_and_call(TaskType.CODE, prompt, profile=RoutingProfile.BALANCED)


@pytest.mark.asyncio
async def test_router_resumes_second_offload_and_records_real_usage(routed):
    routed.replies += [
        _res(sid=SID, total=0.40, content="first"),
        _res(sid=SID, total=0.46, content="second",
             usage={"input": 5, "cache_read": 9000, "cache_write": 100, "output": 200}),
    ]
    r1 = await _call("first request")
    r2 = await _call("second request")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    c1, c2 = routed.sent
    assert c1.resume_session_id is None and c1.prompt.startswith("<FOLDED HISTORY>") and c1.inject_context is True
    assert c1.structured is True
    assert c2.resume_session_id == SID
    # only the new turn, not the re-folded buffer. route_and_call already prefixes the CURRENT
    # repo-state block to `prompt` (observed fresh every turn), which is genuinely new context.
    assert c2.prompt.endswith("second request") and "<FOLDED HISTORY>" not in c2.prompt
    assert c2.inject_context is False                          # inject not duplicated
    # real per-call usage replaces len//4; cache fields carried; cost_usd stays 0 (flat rate)
    assert (r1.input_tokens, r1.output_tokens, r1.cache_creation_input_tokens, r1.cache_read_input_tokens) == (5, 300, 9000, 0)
    assert (r2.input_tokens, r2.output_tokens, r2.cache_creation_input_tokens, r2.cache_read_input_tokens) == (5, 200, 100, 9000)
    assert r1.cost_usd == 0.0 and r2.cost_usd == 0.0
    assert r1.api_equivalent_cost_usd == pytest.approx(0.40) and r1.cli_session_resumed is False
    assert r2.api_equivalent_cost_usd == pytest.approx(0.06) and r2.cli_session_resumed is True


@pytest.mark.asyncio
async def test_router_redoes_a_failed_resume_fresh(routed):
    routed.replies += [
        _res(sid=SID, total=0.40, content="first"),
        _res(rc=1, sid=None, usage=None, total=None, content="API Error: refused"),
        _res(sid="new-sid", total=0.43, content="recovered"),
    ]
    await _call("first request")
    r2 = await _call("second request")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    assert [c.resume_session_id for c in routed.sent] == [None, SID, None]
    assert routed.sent[2].prompt.startswith("<FOLDED HISTORY>") and routed.sent[2].inject_context is True
    assert r2.content == "recovered" and r2.cli_session_resumed is False
    assert r2.api_equivalent_cost_usd == pytest.approx(0.43)


@pytest.mark.asyncio
async def test_router_flag_off_is_the_old_path(routed, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_OFFLOAD_RESUME", "false")
    import llm_router.config as config_module
    config_module._config = None
    routed.replies += [ClaudeResult(content="a", model="x", exit_code=0, duration_sec=0.1),
                       ClaudeResult(content="b", model="x", exit_code=0, duration_sec=0.1)]
    r = await _call("first request")
    await _call("second request")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    assert all(c.structured is False and c.resume_session_id is None for c in routed.sent)
    assert all(c.prompt.startswith("<FOLDED HISTORY>") for c in routed.sent)
    assert r.api_equivalent_cost_usd is None                   # unknown, not 0.0
    assert r.input_tokens >= 1                                  # old len//4 estimate
