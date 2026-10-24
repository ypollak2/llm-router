"""LC-12b: unrecognised flag values fail closed; pass-routing needs the passed model first in the chain;
reconnect disclosure; non-Ollama enable note; an old pass with a matching digest still routes.

Tmp homes only; fake digests, fake route; no network, no model.
"""
# ruff: noqa: F811  (the `world` fixture is imported from test_lc12 and used as an argument)
from __future__ import annotations

import pytest

from llm_router import eligibility_routing as er
from llm_router import router
from llm_router.commands import local_check as LCC
from llm_router.local_check import display, routing as lr
from llm_router.tools import text as T
from llm_router.types import Complexity, TaskType
from tests.test_lc10i_wiring import MODEL, deps, passing, run
from tests.test_lc12 import FLAG, NOTE, call_job, passed, world  # noqa: F401
from tests.test_lc8_routing import BARE, JOB, NOW

SECS_PER_DAY = 86400


@pytest.mark.parametrize("value,expected", [
    ("disable", False), ("nope", False), (" 0 ", False), ("TRUE", True), (" On ", True), ("YES", True),
    ("off", False), ("No", False), ("1", True), ("", None), ("   ", None),
])
def test_flag_value_table(monkeypatch, value, expected):
    monkeypatch.setenv(FLAG, value)
    assert T._local_text_jobs_flag() is expected


def test_flag_unset_is_none(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert T._local_text_jobs_flag() is None


@pytest.mark.parametrize("value", ["disable", "nope", " 0 "])
def test_unrecognised_or_off_value_with_a_pass_is_not_taken(world, monkeypatch, value):
    passed(world)
    monkeypatch.setenv(FLAG, value)
    out = call_job()
    assert world.calls == [] and "status=fallback" in out
    assert T._local_text_jobs_enabled() is False


@pytest.mark.parametrize("value", ["TRUE", " 1 "])
def test_on_value_runs_without_a_pass(world, monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    call_job()
    assert world.calls == [1]


def test_empty_flag_counts_as_unset(world, monkeypatch):
    passed(world)
    monkeypatch.setenv(FLAG, "")
    call_job()
    assert world.calls == [1]


def test_unset_flag_requires_the_passed_model_first_in_the_chain(world, monkeypatch):
    """The router raises (no model called) when the passed model is not in the chain; the tool answers fallback."""
    passed(world)
    seen = {}

    async def route_without_the_model(*a, **k):
        seen["required"] = er._require_passed_first.get()
        raise er.PassedModelNotInChain("not in chain")

    monkeypatch.setattr(T, "route_and_call", route_without_the_model)
    out = call_job()
    assert seen["required"] is True
    assert "status=fallback" in out and "not in the router's chain" in out
    assert er._require_passed_first.get() is False  # reset after the call


def test_flag_on_does_not_require_the_passed_model(world, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    seen = {}

    async def spy(*a, **k):
        seen["required"] = er._require_passed_first.get()
        return await world_route(*a, **k)

    from llm_router.types import LLMResponse

    async def world_route(*a, **k):
        return LLMResponse(content="feat: touch src/a.py", model="ollama/x", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="ollama")

    monkeypatch.setattr(T, "route_and_call", spy)
    call_job()
    assert seen["required"] is False


def _elig(models):
    return router._drop_failed_eligibility(models, TaskType.GENERATE, Complexity.MODERATE, er.TEXT_JOB_REASON)


def test_router_gate_refuses_when_the_passed_model_is_missing_from_the_chain(world):
    passed(world)
    chain = ["gemini/gemini-2.5-flash", "anthropic/claude-sonnet-5"]
    t1 = er.set_text_job_context(JOB, 10)
    t2 = er.set_require_passed_first()
    try:
        with pytest.raises(er.PassedModelNotInChain):
            _elig(chain)
    finally:
        er.reset_require_passed_first(t2)
        er.reset_text_job_context(t1)


def test_router_gate_passes_and_leads_when_the_passed_model_is_in_the_chain(world):
    passed(world)
    chain = ["gemini/gemini-2.5-flash", f"ollama/{BARE}", "anthropic/claude-sonnet-5"]
    t1 = er.set_text_job_context(JOB, 10)
    t2 = er.set_require_passed_first()
    try:
        out = _elig(chain)
    finally:
        er.reset_require_passed_first(t2)
        er.reset_text_job_context(t1)
    assert out[0] == f"ollama/{BARE}" and sorted(out) == sorted(chain)


def test_router_without_the_requirement_keeps_the_chain_when_the_model_is_missing(world):
    passed(world)
    chain = ["gemini/gemini-2.5-flash", "anthropic/claude-sonnet-5"]
    t1 = er.set_text_job_context(JOB, 10)
    try:
        assert _elig(chain) == chain
    finally:
        er.reset_text_job_context(t1)


def test_old_pass_with_a_matching_digest_still_routes(world, monkeypatch):
    """Design section 5 expires only runtimes that report no digest: a 31-day-old Ollama pass routes."""
    passed(world)
    later = NOW + 31 * SECS_PER_DAY
    monkeypatch.setattr(T, "time", type("T", (), {"time": staticmethod(lambda: later)}))
    assert er.passed_lane_available(JOB, 10, later) is True
    call_job()
    assert world.calls == [1]


def test_listing_ignores_enabled_lanes_of_a_non_ollama_model(world):
    lr.enable("lmstudio-only:7b", JOB, "manual")
    assert T._local_text_jobs_enabled() is False
    lr.enable(BARE, JOB, "manual")
    assert T._local_text_jobs_enabled() is True


# ---------------------------------------------------------------------------- CLI text
def test_routing_on_line_discloses_the_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    d, sink, _ = deps(tmp_path, passing())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    assert "routing: on for commit_message" in sink.text
    assert "the llm_text_job tool appears after the next MCP restart or reconnect" in sink.text


def test_manual_enable_on_a_non_ollama_model_prints_the_note(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(display, "inventory", lambda: (["some-ollama:1b"], {}))
    d, sink, _ = deps(tmp_path)
    d.inventory = lambda: ["some-ollama:1b"]
    assert run(["enable", "commit_message", "--model", "lmstudio-model"], d) == 0
    assert NOTE in sink.text and "routing: on" not in sink.text
    d, sink, _ = deps(tmp_path)
    d.inventory = lambda: ["some-ollama:1b"]
    assert run(["status"], d) == 0
    assert NOTE in sink.text and "lmstudio-model commit_message: on" not in sink.text


def test_manual_enable_on_an_ollama_model_says_on_with_the_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    d, sink, _ = deps(tmp_path)
    d.inventory = lambda: ["some-ollama:1b"]
    assert run(["enable", "commit_message", "--model", "some-ollama:1b"], d) == 0
    assert "routing: on for commit_message" in sink.text and "MCP restart or reconnect" in sink.text
