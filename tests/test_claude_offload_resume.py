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
    base = dict(llm_router_claude_offload_resume=True, llm_router_claude_offload_resume_ttl_s=3300,
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
    """plan() then release: these tests are sequential, one call at a time (see the busy tests)."""
    key, entry = claude_resume.plan(cfg or _cfg(), model, scope, cwd, conversation=conv, now=now)
    claude_resume.release(key)
    return key, entry


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
    # A CLI that rewrites ids on resume would double every call: THIS key stays un-resumed for a
    # while; another key (model) is unaffected.
    assert _plan(now=2.0) == (None, None)
    k2, _ = _plan(model="sonnet", now=2.0)
    assert k2 is not None


def test_cap_by_age():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    assert _plan(now=3299.0)[1] is not None
    assert _plan(now=3301.0)[1] is None                       # > 3300 s: the 1 h cache is gone


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


def test_host_session_id_uses_only_the_mcp_call_identity(monkeypatch):
    monkeypatch.undo()           # use the real function, not the fixture's stub
    called = MagicMock(return_value="POINTER")
    monkeypatch.setattr("llm_router.session_store.resolve_session_id", called)
    monkeypatch.setattr("llm_router.call_identity.call_session_id", lambda: None)
    # process-wide env ids are inherited by every child of a Claude Code shell: never a key
    monkeypatch.setenv("CLAUDE_SESSION_ID", "from-env")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-env-2")
    assert claude_resume.host_session_id() is None
    called.assert_not_called()
    assert claude_resume.make_key("opus", "/repo", "/repo") is None
    cfg = _cfg()
    assert claude_resume.plan(cfg, "opus", "/repo", "/repo", conversation=None) == (None, None)
    monkeypatch.setattr("llm_router.call_identity.call_session_id", lambda: "mcp-sess")
    assert claude_resume.host_session_id() == "mcp-sess"


def test_concurrent_calls_on_one_key_second_runs_fresh():
    k1, e1 = claude_resume.plan(_cfg(), "opus", "/repo", "/repo", conversation=None, now=0.0)
    k2, e2 = claude_resume.plan(_cfg(), "opus", "/repo", "/repo", conversation=None, now=0.0)
    assert k1 is not None and k2 is None and e2 is None       # second does not wait, does not resume
    # an unkeyed fresh call never touches the stored entry
    claude_resume.settle(k2, e2, _res(sid="other", total=0.9), now=0.0)
    claude_resume.settle(k1, e1, _res(), now=0.0)
    claude_resume.release(k1)
    k3, e3 = claude_resume.plan(_cfg(), "opus", "/repo", "/repo", conversation=None, now=1.0)
    assert e3 is not None and e3.session_id == SID
    claude_resume.release(k3)


def test_missing_session_id_on_resume_retries_fresh_without_pausing():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    _, e = _plan(now=1.0)
    s = claude_resume.settle(key, e, _res(sid=None, total=None), now=1.0)
    assert s.retry is True and s.discarded_cost_usd is None    # unknown, not 0.0
    k2, e2 = _plan(now=2.0)
    assert k2 == key and e2 is None                            # entry dropped, key NOT paused


def test_failed_resume_cost_is_unknown_not_zero():
    key, _ = _plan(now=0.0)
    claude_resume.settle(key, None, _res(), now=0.0)
    _, e = _plan(now=1.0)
    s = claude_resume.settle(key, e, _res(rc=1, sid=None, usage=None, total=None), now=1.0)
    assert s.discarded_cost_usd is None


def test_resume_prompt_prepends_caller_context_only_when_set():
    assert claude_resume.resume_prompt("new", None) == "new"
    assert claude_resume.resume_prompt("new", "  ") == "new"
    out = claude_resume.resume_prompt("new", "use the v2 API")
    assert out.endswith("new") and "use the v2 API" in out and out.index("v2") < out.index("new")


# -- router dispatch: the real branch in router.py, with run_claude replaced ----------------------

@pytest.fixture
def routed(temp_db, mock_env, monkeypatch):
    """Subscription mode, only the Claude CLI reachable; ``routed.sent`` records each run_claude call."""
    from llm_router import router as router_module

    monkeypatch.delenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BLOCK_PROVIDERS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_OFFLOAD_RESUME", "true")     # off by default: enable explicitly
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
    # the failed resume's cost is unknown, so the total is unknown: never an understated 0.43
    assert r2.api_equivalent_cost_usd is None


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


@pytest.mark.asyncio
async def test_router_resume_keeps_caller_context(routed):
    routed.replies += [_res(sid=SID, total=0.40), _res(sid=SID, total=0.46)]
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile, TaskType
    await route_and_call(TaskType.CODE, "first", profile=RoutingProfile.BALANCED)
    await route_and_call(TaskType.CODE, "second", profile=RoutingProfile.BALANCED,
                         caller_context="the user pinned API v2")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    assert routed.sent[1].resume_session_id == SID
    assert "the user pinned API v2" in routed.sent[1].prompt and routed.sent[1].prompt.endswith("second")


@pytest.mark.asyncio
async def test_router_concurrent_offloads_do_not_share_a_resume(routed):
    # every reply carries SID, so a resumed call is never read as a mismatch whichever pops it
    routed.replies += [_res(sid=SID, total=0.40), _res(sid=SID, total=0.46), _res(sid=SID, total=0.10)]
    await _call("first")
    gate = asyncio.Event()
    inside = []

    import llm_router.router as rm
    inner = rm.run_claude

    async def slow(prompt, **kw):
        inside.append(prompt)            # both calls are past plan() (it runs before run_claude)
        await gate.wait()
        return await inner(prompt, **kw)

    rm.run_claude = slow
    try:
        t1 = asyncio.create_task(_call("second"))
        t2 = asyncio.create_task(_call("third"))
        for _ in range(300):              # wait until BOTH are blocked inside run_claude
            if len(inside) == 2:
                break
            await asyncio.sleep(0.05)
        assert len(inside) == 2
        gate.set()
        await asyncio.gather(t1, t2)
    finally:
        rm.run_claude = inner
    assert len(routed.sent) == 3
    resumed = [c for c in routed.sent[1:] if c.resume_session_id]
    assert len(resumed) == 1                                   # exactly one of the two resumes SID


def test_log_usage_prices_cli_cache_writes_at_the_1h_rate():
    from llm_router import cost, pricing
    five = cost._claude_cost("claude-sonnet-5-5", 0, 0, cache_write_t=1_000_000)
    hour = cost._claude_cost("claude-sonnet-5-5", 0, 0, cache_write_t=1_000_000, cache_write_1h=True)
    inp = pricing.rates_per_m("claude-sonnet-5-5")["input"]
    assert hour == pytest.approx(inp * 2.0) and five == pytest.approx(inp * 1.25)
    # other components untouched, and the default (5-minute) path is unchanged
    assert cost._claude_cost("claude-sonnet-5-5", 1_000_000, 0, cache_read_t=1_000_000, cache_write_1h=True) == \
        cost._claude_cost("claude-sonnet-5-5", 1_000_000, 0, cache_read_t=1_000_000)


@pytest.mark.asyncio
async def test_usage_row_for_a_cli_offload_is_priced_with_1h_writes(temp_db):
    """The usage row's potential_cost_usd uses the real tokens, with the 1h write rate only when flagged."""
    import sqlite3
    from llm_router import cost
    from llm_router.config import get_config
    from llm_router.types import LLMResponse, RoutingProfile, TaskType

    def _resp(one_hour):
        return LLMResponse(content="x", model="anthropic/sonnet", input_tokens=7, output_tokens=11,
                           cost_usd=0.0, latency_ms=1.0, provider="anthropic",
                           cache_creation_input_tokens=200_000, cache_read_input_tokens=300_000,
                           cache_write_1h=one_hour)

    await cost.log_usage(_resp(True), TaskType.CODE, RoutingProfile.BALANCED, correlation_id="c-1h")
    await cost.log_usage(_resp(False), TaskType.CODE, RoutingProfile.BALANCED, correlation_id="c-5m")
    con = sqlite3.connect(str(get_config().llm_router_db_path))
    try:
        rows = dict(con.execute("SELECT correlation_id, potential_cost_usd FROM usage").fetchall())
    finally:
        con.close()
    assert set(rows) == {"c-1h", "c-5m"}
    from llm_router import pricing
    inp = pricing.rates_per_m(pricing.savings_baseline_model())["input"]
    # only the cache-write component differs: 200k tokens at (2.0 - 1.25)x the baseline input rate
    assert rows["c-1h"] - rows["c-5m"] == pytest.approx(200_000 * inp * 0.75 / 1_000_000)
    assert rows["c-1h"] > rows["c-5m"] > 0


def test_resume_is_off_by_default():
    from llm_router.config import RouterConfig
    assert RouterConfig.model_fields["llm_router_claude_offload_resume"].default is False
    assert claude_resume.enabled(SimpleNamespace()) is False


@pytest.mark.parametrize("exc", [RuntimeError("boom"), asyncio.CancelledError()], ids=["raises", "cancelled"])
@pytest.mark.asyncio
async def test_router_releases_the_key_when_run_claude_raises_or_is_cancelled(routed, monkeypatch, exc):
    import llm_router.router as rm
    routed.replies += [_res(sid=SID, total=0.40), _res(sid=SID, total=0.46)]
    await _call("first")
    if not routed.sent:
        pytest.skip("anthropic CLI branch was not selected by this harness")
    inner = rm.run_claude

    async def boom(prompt, **kw):
        raise exc

    monkeypatch.setattr(rm, "run_claude", boom)
    with pytest.raises((Exception, asyncio.CancelledError)):
        await _call("second")
    assert claude_resume._BUSY == set()                        # not stuck in flight
    # the session entry survived and its key is free, so the next plan() on it resumes. (Not a third
    # routed call: a raising provider trips the health tracker and would hide the point.)
    (key, entry), = claude_resume._STORE.items()
    k2, e2 = claude_resume.plan(_cfg(), key[3], key[1], key[2], conversation=None)
    assert k2 == key and e2 is entry and e2.session_id == SID
    claude_resume.release(k2)
    del inner
