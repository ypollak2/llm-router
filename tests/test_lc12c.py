"""LC-12b / D-277: flag unset = the passed local model ALONE, driven through llm_text_job -> route_and_call.

Only the provider call layer (``router._call_text``), the chain builder and, where a test needs a step to remove
the local model, the capability filter's verdict are replaced; the TJCHAIN tests at the end put the real chain
builder back. Tmp homes, fake digests, no network, no model.
"""
# ruff: noqa: F811  (the `world` fixture is imported from test_lc12 and used as an argument)
from __future__ import annotations

import pytest

from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.text_jobs import MAX_JOB_ATTEMPTS
from llm_router.tools import text as T
from llm_router.types import Complexity, LLMResponse, TaskType
from tests.test_lc12 import FLAG, call_job, passed, world  # noqa: F401
from tests.test_lc8_routing import BARE, DIGEST, JOB

LOCAL = f"ollama/{BARE}"
CLOUD = ["gemini/gemini-2.5-flash", "anthropic/claude-sonnet-5"]
GOOD = "feat: touch src/a.py"
BAD_TEXT = "fix bug in one spot"  # names no changed file, so the acceptance check rejects it


@pytest.fixture
def e2e(world, tmp_path, mock_env, temp_db, monkeypatch):
    """The real route_and_call under llm_text_job; ``_call_text`` is the only model call, recorded in ``tried``."""
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "provider_reset.json"))
    monkeypatch.setattr(router, "is_codex_available", lambda: False)
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setattr(T, "route_and_call", router.route_and_call)
    state = {"chain": [LOCAL, *CLOUD], "script": {}, "tried": []}

    async def build(*a, **k):
        return list(state["chain"])

    async def call_text(model, *a, **k):
        state["tried"].append(model)
        out = state["script"].get(model, GOOD)
        if isinstance(out, Exception):
            raise out
        return LLMResponse(content=out, model=model, input_tokens=5, output_tokens=5, cost_usd=0.0,
                           latency_ms=1.0, provider=model.split("/", 1)[0])

    monkeypatch.setattr(router, "_build_and_filter_chain", build)
    monkeypatch.setattr(router, "_call_text", call_text)
    # LC-12f: the pass-only cut site re-reads the live /api/tags row; by default it matches the pass.
    monkeypatch.setattr(er, "_live_tags", lambda: {"models": [{"name": BARE, "model": BARE, "digest": DIGEST}]})
    from unittest.mock import AsyncMock
    monkeypatch.setattr("llm_router.cost.get_daily_spend", AsyncMock(return_value=0.0))
    monkeypatch.setattr("llm_router.cost.get_monthly_spend", AsyncMock(return_value=0.0))
    world.monkeypatch = monkeypatch
    world.state = state
    return world


def _drain():
    import asyncio
    asyncio.run(router.drain_bg_tasks(timeout_s=5.0))


def test_accepted_on_the_passed_model_alone(e2e):
    passed(e2e)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL]
    assert "status=accepted, attempts=1" in out


def test_unset_local_error_hands_back_and_calls_no_cloud_model(e2e):
    passed(e2e)
    e2e.state["script"][LOCAL] = RuntimeError("ollama down")
    before = router._pending_spend
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL]  # the one local attempt; no cloud entry was called
    assert "status=fallback, attempts=1" in out and "Do this job yourself." in out
    assert "no cloud model was called" in out
    assert abs(router._pending_spend - before) < 1e-9


def test_unset_rejected_answers_retry_on_the_local_model_only_then_hand_back(e2e):
    passed(e2e)
    e2e.state["script"][LOCAL] = BAD_TEXT
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL] * MAX_JOB_ATTEMPTS
    assert f"status=rejected, attempts={MAX_JOB_ATTEMPTS}" in out and "do this job yourself" in out.lower()


def test_unset_local_error_after_a_rejection_hands_back_with_the_real_attempt_count(e2e, monkeypatch):
    passed(e2e)
    seq = iter([BAD_TEXT, RuntimeError("ollama went away")])

    async def flaky(model, *a, **k):
        e2e.state["tried"].append(model)
        x = next(seq)
        if isinstance(x, Exception):
            raise x
        return LLMResponse(content=x, model=model, input_tokens=1, output_tokens=1, cost_usd=0.0,
                           latency_ms=1.0, provider="ollama")

    monkeypatch.setattr(router, "_call_text", flaky)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL, LOCAL]
    assert "status=fallback, attempts=2" in out


def test_flag_on_local_error_falls_through_to_the_next_entry(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)
    e2e.state["script"][LOCAL] = RuntimeError("ollama down")
    out = call_job()
    _drain()
    tried = e2e.state["tried"]
    assert tried[0] == LOCAL and len(tried) >= 2 and tried[1] != LOCAL  # fell through to the next entry
    assert "status=accepted" in out


def test_flag_on_still_raises_when_every_entry_fails(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)
    for m in (LOCAL, *CLOUD, "openai/gpt-4o-mini"):
        e2e.state["script"][m] = RuntimeError("down")
    with pytest.raises(Exception):  # noqa: B017,PT011 - the router's own exhaustion error, as before
        call_job()
    _drain()


def test_unset_passed_model_missing_from_the_chain_refuses_with_no_model_call_and_no_leak(e2e):
    passed(e2e)
    e2e.state["chain"] = list(CLOUD)
    before = router._pending_spend
    out = call_job()
    _drain()
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out and "not in the router's chain" in out
    assert router._pending_spend == pytest.approx(before, abs=1e-12)  # PR #705 review: the reservation was leaking here


def test_route_and_call_refusal_releases_the_reservation(e2e, monkeypatch):
    """Straight at route_and_call: the PassedModelNotInChain raise leaves _pending_spend as it was."""
    import asyncio
    passed(e2e)
    e2e.state["chain"] = list(CLOUD)
    seen = {}
    real = router._drop_failed_eligibility

    def spy(*a, **k):
        seen["pending_during"] = router._pending_spend
        return real(*a, **k)

    monkeypatch.setattr(router, "_drop_failed_eligibility", spy)
    t1 = er.set_text_job_context(JOB, 10)
    t2 = er.set_require_passed_first()
    before = router._pending_spend
    try:
        with pytest.raises(er.PassedModelNotInChain):
            asyncio.run(router.route_and_call(TaskType.GENERATE, "p", eligibility_exempt_reason=er.TEXT_JOB_REASON))
        asyncio.run(router.drain_bg_tasks(timeout_s=5.0))
    finally:
        er.reset_require_passed_first(t2)
        er.reset_text_job_context(t1)
    assert seen["pending_during"] > before  # the reservation WAS held at the refusal point
    assert router._pending_spend == pytest.approx(before, abs=1e-12)


def test_unset_capability_filter_removing_the_local_model_refuses_and_releases(e2e, monkeypatch):
    passed(e2e)
    from llm_router import capability_filter as cf
    monkeypatch.setattr(cf, "enabled", lambda: True)
    monkeypatch.setattr(cf, "filter_chain_report",
                        lambda models, needs: ([m for m in models if m != LOCAL], [(LOCAL, "window")]))
    before = router._pending_spend
    out = call_job()
    _drain()
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out
    assert router._pending_spend == pytest.approx(before, abs=1e-12)


def test_flag_on_capability_filter_keeps_the_rest_of_the_chain(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    from llm_router import capability_filter as cf
    monkeypatch.setattr(cf, "enabled", lambda: True)
    monkeypatch.setattr(cf, "filter_chain_report",
                        lambda models, needs: ([m for m in models if m != LOCAL], [(LOCAL, "window")]))
    call_job()
    _drain()
    assert e2e.state["tried"] and LOCAL not in e2e.state["tried"]


def test_unset_emergency_chain_is_never_walked(e2e, monkeypatch):
    """A distinct BUDGET emergency chain must not be walked when the local model fails (LC-12d: reachable)."""
    from llm_router.types import RoutingProfile
    passed(e2e)
    emergency = [LOCAL, "openai/gpt-4o-mini", "groq/llama-3.3-70b"]  # distinct from the primary chain
    primary = e2e.state["chain"]

    async def build(task_type, profile, *a, **k):
        return list(emergency if profile == RoutingProfile.BUDGET else primary)

    monkeypatch.setattr(router, "_build_and_filter_chain", build)
    real = router._resolve_profile

    def non_budget(*a, **k):  # the emergency path is only reachable from a non-BUDGET profile
        _p, c, think = real(*a, **k)
        return RoutingProfile.BALANCED, c, think

    monkeypatch.setattr(router, "_resolve_profile", non_budget)
    e2e.state["script"][LOCAL] = RuntimeError("down")
    out = call_job()
    _drain()
    assert e2e.state["tried"] == [LOCAL]  # one local attempt; the emergency chain (which also has LOCAL) not walked
    assert "status=fallback" in out and "no cloud model was called" in out


def test_unset_eligibility_step_raising_fails_closed_and_releases_once(e2e, monkeypatch):
    """LC-12d / S1: any exception inside the eligibility step refuses; it never falls open to the raw chain."""
    passed(e2e)
    e2e.state["chain"] = [CLOUD[0], LOCAL, CLOUD[1]]  # a paid model heads the raw chain (the reviewer's case)

    def boom(*a, **k):
        raise RuntimeError("note_bypass broke")

    monkeypatch.setattr(er, "note_bypass", boom)
    monkeypatch.setattr(router, "_pending_spend", 5.0)  # non-zero seed: a double release would show
    out = call_job()
    _drain()
    assert e2e.state["tried"] == []
    assert not [m for m in e2e.state["tried"] if not m.startswith("ollama/")]
    assert "status=fallback, attempts=0" in out and "Do this job yourself." in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)


def test_flag_on_eligibility_step_raising_still_fails_open(e2e, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    passed(e2e)
    monkeypatch.setattr(er, "note_bypass", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    call_job()
    _drain()
    assert e2e.state["tried"]  # the full chain is still served, as before


def test_unset_survivor_that_is_not_the_passed_model_refuses_and_releases_once(e2e, monkeypatch):
    """LC-12d defense in depth: a non-passed head surviving the eligibility step is refused at the cut."""
    passed(e2e)
    monkeypatch.setattr(router, "_drop_failed_eligibility", lambda models, *a, **k: list(CLOUD))
    monkeypatch.setattr(router, "_pending_spend", 5.0)
    out = call_job()
    _drain()
    assert e2e.state["tried"] == []
    assert "status=fallback, attempts=0" in out
    assert router._pending_spend == pytest.approx(5.0, abs=1e-12)


def test_stream_door_call_never_raises_the_requirement():
    """_drop_failed_eligibility without an exempt reason (the stream door) ignores the pass-first requirement."""
    t2 = er.set_require_passed_first()
    try:
        assert router._drop_failed_eligibility(list(CLOUD), TaskType.GENERATE, Complexity.MODERATE) == CLOUD
    finally:
        er.reset_require_passed_first(t2)


def test_enable_with_an_empty_ollama_inventory_says_ollama_is_down(tmp_path, monkeypatch):
    from llm_router.commands import local_check as LCC
    from tests.test_lc10i_wiring import deps, run
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("llm_router.local_check.display.inventory", lambda: ([], {}))
    d, sink, _ = deps(tmp_path)
    d.inventory = lambda: []
    assert run(["enable", "commit_message", "--model", "some-ollama:1b"], d) == 0
    assert LCC.OLLAMA_DOWN_NOTE in sink.text and LCC.RECONNECT_NOTE not in sink.text


# ---- TJCHAIN-1 / TJCHAIN-2: the chain of a text job does not follow its input; its answer goes to its own check ----
# docs/BUGS.md TJCHAIN-1: route_and_call derived the tier from the prompt (a prompt over 2000 characters read as
# complex, the PREMIUM chain, which injects no local model), so the pass-only lane was refused for every such job.
# ``e2e`` stubs the chain builder with one fixed list, which hides that. ``real_chain`` puts the real builder back, with
# the passed model as the one configured local model, so the tier the router derives decides the chain.
# docs/BUGS.md TJCHAIN-2: the generic quality score (0.30 for a one-line commit message) threw away a local answer
# the job's own check accepts, and called a pricier model.

_REAL_BUILD_CHAIN = router._build_and_filter_chain  # taken at import: ``e2e`` replaces the attribute for each test
SIZES = [300, 2000, 40000]  # 40000 is llm_text_job's input cap (text_jobs.MAX_INPUT_CHARS)


@pytest.fixture
def real_chain(e2e, monkeypatch):
    from llm_router.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "ollama_budget_models", BARE)
    monkeypatch.setattr(cfg, "ollama_base_url", "http://127.0.0.1:11434")  # explicit URL: nothing probes the daemon
    monkeypatch.setattr("llm_router.discover.ollama_can_generate", lambda m: True)
    monkeypatch.setattr(router, "_build_and_filter_chain", _REAL_BUILD_CHAIN)
    return e2e


@pytest.fixture
def low_score(monkeypatch):
    """Every answer scores 0.30 (under the 0.4 threshold) and check_output is wrapped to count its calls."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from llm_router import quality_feedback, text_jobs
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "1")
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_THRESHOLD", "0.4")
    score = Mock(return_value=SimpleNamespace(score=0.3, reasons=["short"]))
    check = Mock(wraps=text_jobs.check_output)
    monkeypatch.setattr(quality_feedback, "score_response", score)
    monkeypatch.setattr(text_jobs, "check_output", check)
    return SimpleNamespace(score=score, check=check)


def _wide_pass(world, limit=94464):
    """A passed, enabled lane whose recorded scope admits ``limit`` characters (94464 is the live lane's scope)."""
    from tests.test_lc8_routing import PROV, lane_file
    passed(world)
    from llm_router.local_check import runtimes as rt
    world.monkeypatch.setattr(er, "_route_time_ctx", lambda m: 32768)
    settings = rt.settings_hash(temperature=rt.TEMPERATURE, ctx=32768, max_tokens=rt.MAX_TOKENS, think=False)
    lane_file(world.home, prov={**PROV, "settings_hash": settings, "scope": {"max_prompt_chars": limit}})
    er.reset_cache()


def _job_of(size):
    """Run llm_text_job on a diff of exactly ``size`` characters; return the structured sink it fills."""
    import asyncio

    from tests.test_lc12 import DIFF
    sink: dict = {}
    asyncio.run(T._llm_text_job(JOB, DIFF + "+" + "x" * (size - len(DIFF) - 1), None, None, sink))
    _drain()
    return sink


@pytest.mark.parametrize("flag", [None, "1"], ids=["flag-unset", "flag-1"])
@pytest.mark.parametrize("size", SIZES)
def test_text_job_chain_leads_with_the_passed_model_at_any_input_size(real_chain, monkeypatch, size, flag):
    """TJCHAIN-1. Flag unset: served by the passed model alone, not refused. Flag 1: the passed model heads the chain."""
    _wide_pass(real_chain)
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "0")  # TJCHAIN-2 has its own tests; this asks about the chain
    if flag:
        monkeypatch.setenv(FLAG, flag)
    out = _job_of(size)
    assert out["status"] == "accepted", out
    assert out["model"] == LOCAL
    assert real_chain.state["tried"] == [LOCAL]


@pytest.mark.parametrize("flag", [None, "1"], ids=["flag-unset", "flag-1"])
def test_text_job_chain_ignores_the_engines_verdict_on_the_wording(real_chain, monkeypatch, flag):
    """TJCHAIN-1. Under LLM_ROUTER_ENGINE_MODE=decide the engine classifies the prompt's words. It is stubbed to call
    every prompt deep reasoning (the REASONING chain holds no local model); a text job's chain must not follow."""
    from types import SimpleNamespace

    from llm_router import engine_door
    _wide_pass(real_chain)
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "0")
    monkeypatch.setattr(engine_door, "decision", lambda *a, **k: SimpleNamespace(complexity="deep_reasoning"))
    if flag:
        monkeypatch.setenv(FLAG, flag)
    out = _job_of(300)
    assert out["status"] == "accepted", out
    assert real_chain.state["tried"] == [LOCAL]


@pytest.mark.parametrize(("size", "status"), [(3000, "accepted"), (3001, "fallback")])
def test_input_over_the_lane_scope_is_handed_back_and_calls_no_model(real_chain, size, status):
    """The lane's scope.max_prompt_chars still bounds a job: at the limit it runs, one character over it does not."""
    _wide_pass(real_chain, limit=3000)
    out = _job_of(size)
    assert out["status"] == status
    assert real_chain.state["tried"] == ([LOCAL] if status == "accepted" else [])


def test_router_refuses_an_input_over_the_lane_scope_even_when_the_entry_check_passes(real_chain, monkeypatch):
    """Defense in depth: with llm_text_job's own gate forced open, the router's chain step still refuses the job before
    any model call. The chain now holds cloud entries at every size, and none of them may serve a pass-only job."""
    _wide_pass(real_chain, limit=1000)
    monkeypatch.setattr(er, "passed_lane_available", lambda *a, **k: True)
    out = _job_of(2000)
    assert (out["status"], out["fallback"], out["attempts"]) == ("fallback", "lane_refused", 0)
    assert real_chain.state["tried"] == []


def test_text_job_with_no_passed_lane_is_handed_back_and_calls_no_model(real_chain):
    out = _job_of(2000)
    assert (out["status"], out["fallback"], out["attempts"]) == ("fallback", "no_lane", 0)
    assert real_chain.state["tried"] == []


def test_router_refuses_a_text_job_with_no_passed_lane_even_when_the_entry_check_passes(real_chain, monkeypatch):
    monkeypatch.setattr(er, "passed_lane_available", lambda *a, **k: True)
    out = _job_of(2000)
    assert (out["status"], out["fallback"], out["attempts"]) == ("fallback", "lane_refused", 0)
    assert real_chain.state["tried"] == []


def test_router_refuses_when_the_passed_model_is_not_a_configured_local_model(real_chain, monkeypatch):
    """The real chain is then cloud entries only; a pass-only job is not served from them."""
    from llm_router.config import get_config
    _wide_pass(real_chain)
    monkeypatch.setattr(get_config(), "ollama_budget_models", "")
    out = _job_of(2000)
    assert (out["status"], out["fallback"], out["attempts"]) == ("fallback", "lane_refused", 0)
    assert real_chain.state["tried"] == []


def test_low_scoring_text_job_answer_is_not_escalated_and_reaches_its_own_check(e2e, monkeypatch, low_score):
    """TJCHAIN-2. Flag 1, the full chain, so an escalation has somewhere to go; the local answer scores 0.30."""
    from llm_router import quality_feedback
    monkeypatch.setenv(FLAG, "1")
    _wide_pass(e2e)
    out = _job_of(2000)
    assert out["status"] == "accepted" and out["model"] == LOCAL
    assert e2e.state["tried"] == [LOCAL]  # not sent on to CLOUD[0]
    assert low_score.check.call_count == 1  # the job's own check saw the answer
    assert low_score.check.call_args.args[0] == JOB and low_score.check.call_args.args[2] == GOOD
    assert not low_score.score.called  # nothing scored it, so nothing was recorded for the quality breaker either
    assert quality_feedback.get_quality_stats(LOCAL, "generate", "moderate") is None


@pytest.mark.parametrize("exempt", [None, "validated_edit"], ids=["no-exemption", "another-exempt-reason"])
def test_the_same_score_on_any_other_call_still_escalates_once_and_is_recorded(e2e, low_score, exempt):
    """TJCHAIN-2 is scoped to the text-job reason: another call with the same 0.30 keeps the escalation, unchanged."""
    import asyncio

    from llm_router import quality_feedback
    asyncio.run(router.route_and_call(TaskType.GENERATE, "detail " * 300, complexity_hint="moderate",
                                      eligibility_exempt_reason=exempt))
    _drain()
    tried = e2e.state["tried"]
    assert len(tried) == 2 and tried[0] != tried[1]  # the first answer was thrown away, once
    assert low_score.score.called and not low_score.check.called
    assert quality_feedback.get_quality_stats(tried[0], "generate", "moderate") == pytest.approx((0.3, 1))


@pytest.mark.parametrize("flag", [None, "1"], ids=["flag-unset", "flag-1"])
def test_repeated_text_jobs_never_get_the_local_model_quality_skipped(e2e, monkeypatch, flag):
    """TJCHAIN-2. A 0.30 recorded for each job made should_skip_model drop the local model from the fourth job on (three
    recorded calls under 0.4); flag unset, the pass-only chain is that model alone, so a long-lived server handed back
    every later job."""
    from llm_router import quality_feedback
    if flag:
        monkeypatch.setenv(FLAG, flag)
    _wide_pass(e2e)
    for size in (2000, 2100, 2200, 2300):  # four different prompts: an identical one is answered from the cache
        assert _job_of(size)["status"] == "accepted"
    assert e2e.state["tried"] == [LOCAL] * 4
    assert not quality_feedback.should_skip_model(LOCAL, "generate", "moderate")


@pytest.mark.parametrize("window", [32768, None], ids=["known-window", "unreadable-window"])
def test_dense_text_job_guard(e2e, monkeypatch, window):
    import asyncio

    from tests.test_lc12 import DIFF
    _wide_pass(e2e)
    if window is None:
        # Isolate this guard from the existing unknown-window eligibility refusals.
        monkeypatch.setattr(er, "passed_lane_available", lambda *a, **k: True)
        monkeypatch.setattr(er, "passed_model_leads", lambda models, *a, **k: models[0] == LOCAL)
        monkeypatch.setattr(er, "prefer_passed", lambda models, *a, **k: [LOCAL, *[m for m in models if m != LOCAL]])
        monkeypatch.setattr(er, "live_check_refusal", lambda *a, **k: None)
        monkeypatch.setattr(er, "_route_time_ctx", lambda m: None)
    text = (DIFF + "+" + "1234567890" * 4000)[:39000]
    sink: dict = {}
    out = asyncio.run(T._llm_text_job(JOB, text, None, None, sink))
    _drain()
    if window is None:
        assert sink["status"] == "accepted", sink
        assert e2e.state["tried"] == [LOCAL]
    else:
        assert (sink["status"], sink["attempts"]) == ("fallback", 0), sink
        assert e2e.state["tried"] == []
        assert "digit-aware" in out and "no cloud model was called" in out


def test_ordinary_text_job_same_length_reaches_model(e2e):
    _wide_pass(e2e)
    sink = _job_of(39000)
    assert (sink["status"], sink["attempts"]) == ("accepted", 1), sink
    assert e2e.state["tried"] == [LOCAL]


def _prompts_seen(world, monkeypatch):
    """Record the prompt text of every model call the e2e world makes."""
    seen: list[str] = []
    inner = router._call_text

    async def spy(model, *a, **k):
        seen.append(repr(a) + repr(k))
        return await inner(model, *a, **k)

    monkeypatch.setattr(router, "_call_text", spy)
    return seen


@pytest.mark.parametrize("size", [16100, 39000])
def test_text_job_prompt_reaches_the_model_whole(e2e, monkeypatch, size):
    """TJCHAIN-3. Compaction kept the first 20 and last 10 lines of the fenced diff; the model wrote about what it could
    not see. The tail of the middle of the diff must reach the model."""
    from tests.test_lc12 import DIFF
    _wide_pass(e2e)
    seen = _prompts_seen(e2e, monkeypatch)
    filler = "".join(f"+line {i} MARK{i}\n" for i in range(size // 16))
    sink: dict = {}
    import asyncio
    text = DIFF + filler
    asyncio.run(T._llm_text_job(JOB, text[:size], None, None, sink))
    _drain()
    assert sink["status"] == "accepted", sink
    middle = f"MARK{size // 32}"
    assert any(middle in s for s in seen), "the middle of the input never reached the model"


def test_any_other_large_prompt_is_still_compacted(e2e, monkeypatch):
    """TJCHAIN-3 changes text jobs only."""
    import asyncio
    seen = _prompts_seen(e2e, monkeypatch)
    from unittest.mock import AsyncMock, Mock
    compact = AsyncMock(side_effect=lambda p, t: (p, Mock(tokens_saved_estimate=0)))
    monkeypatch.setattr(router, "compact_structural", compact)
    asyncio.run(router.route_and_call(TaskType.GENERATE, "x" * 20000, temperature=0.2))
    _drain()
    assert compact.await_count == 1
    assert seen


def test_low_scores_of_other_calls_do_not_hand_back_a_text_job(e2e, monkeypatch):
    """TJCHAIN-2. A text job has one key with llm_generate (generate, moderate). Three low llm_generate scores must not
    make the local model skip for a text job; the same key still skips for a normal route."""
    from llm_router import quality_feedback
    _wide_pass(e2e)
    for _ in range(3):
        quality_feedback.record_quality(LOCAL, "generate", "moderate", 0.3)
    assert quality_feedback.should_skip_model(LOCAL, "generate", "moderate")
    out = _job_of(2000)
    assert out["status"] == "accepted", out
    assert e2e.state["tried"] == [LOCAL]
