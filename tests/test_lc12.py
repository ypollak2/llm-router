"""LC-12: a local-check pass routes its lanes without the env flag (D-272); Ollama-only routing message (D-274).

Tmp homes only; fake digests, fake Ollama settings, fake route; no network, no model.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import eligibility_routing as er
from llm_router.commands import local_check as LCC
from llm_router.local_check import display, routing as lr, runtimes
from llm_router.tools import text as T
from llm_router.types import LLMResponse
from tests.test_lc10i_wiring import MODEL, deps, passing, run
from tests.test_lc8_routing import BARE, CTX, DIGEST, JOB, NOW, lane_file

DIFF = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
FLAG = "LLM_ROUTER_LOCAL_TEXT_JOBS"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A text-job call where the only model call is a counter; the digest inventory, router ctx/think and clock are fixed."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_IGNORE_ELIGIBILITY", raising=False)
    monkeypatch.delenv(FLAG, raising=False)
    er.reset_cache()
    calls: list[int] = []

    async def fake_route(*a, **k):
        calls.append(1)
        return LLMResponse(content="feat: touch src/a.py", model="ollama/x", input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider="ollama")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(T, "route_and_call", fake_route)
    monkeypatch.setattr(T, "_announce_routing", noop)
    monkeypatch.setattr(T, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(display, "inventory", lambda: ([BARE], {BARE: DIGEST}))
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX)
    monkeypatch.setattr(er, "_route_time_think", lambda: False)
    yield SimpleNamespace(home=tmp_path, calls=calls)
    er.reset_cache()


def call_job() -> str:
    return asyncio.run(T.llm_text_job(JOB, DIFF, SimpleNamespace()))


def passed(world) -> None:
    lane_file(world.home)
    lr.enable(BARE, JOB, "local_check")


def test_unset_flag_with_passed_lane_takes_the_local_path(world):
    passed(world)
    call_job()
    assert world.calls == [1]


def test_unset_flag_without_a_pass_is_not_taken(world):
    out = call_job()
    assert world.calls == [] and "status=fallback" in out and "local-check" in out


def test_unset_flag_pass_but_digest_stale_is_not_taken(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(display, "inventory", lambda: ([], {BARE: "sha256:" + "c" * 16}))
    call_job()
    assert world.calls == []


def test_unset_flag_pass_but_settings_mismatch_is_not_taken(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX // 2)       # router window below the measured ctx
    call_job()
    assert world.calls == []


def test_unset_flag_pass_but_lane_disabled_is_not_taken(world):
    passed(world)
    lr.disable(BARE, JOB, "manual")
    call_job()
    assert world.calls == []


def test_unset_flag_pass_but_lane_demoted_is_not_taken(world):
    passed(world)
    lr.disable(BARE, JOB, "demoted: fallbacks")
    call_job()
    assert world.calls == []


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_flag_zero_with_pass_is_not_taken(world, monkeypatch, value):
    passed(world)
    monkeypatch.setenv(FLAG, value)
    out = call_job()
    assert world.calls == [] and "turned off" in out


def test_flag_one_without_a_pass_keeps_todays_behaviour(world, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    call_job()
    assert world.calls == [1]


def test_tool_is_listed_only_when_flag_on_or_unset_with_an_enabled_lane(world, monkeypatch):
    assert T._local_text_jobs_enabled() is False                 # unset, nothing switched on
    lr.enable(BARE, JOB, "local_check")
    assert T._local_text_jobs_enabled() is True                  # unset, a lane is on
    monkeypatch.setenv(FLAG, "0")
    assert T._local_text_jobs_enabled() is False                 # global off wins
    monkeypatch.setenv(FLAG, "1")
    assert T._local_text_jobs_enabled() is True


def test_passed_lane_available_reports_false_not_raise_on_error(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(display, "inventory", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert er.passed_lane_available(JOB, 10, NOW) is False


# ---------------------------------------------------------------------------- D-274
NOTE = "measured; routing for this runtime is not supported yet (Ollama only in this release)"


def lmstudio_deps(tmp_path: Path):
    return deps(tmp_path, passing(), endpoints=[runtimes.Endpoint("lmstudio", "http://127.0.0.1:1234", [MODEL])])


def test_judged_pass_on_non_ollama_prints_the_note_and_not_routing_on(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    d, sink, _ = lmstudio_deps(tmp_path)
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    assert sink.text.count(NOTE) == 1
    assert "routing: on" not in sink.text
    assert not lr.is_enabled(MODEL, "commit_message")


def test_status_after_a_non_ollama_pass_prints_the_note_not_routing_on(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    d, _, _ = lmstudio_deps(tmp_path)
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    d, sink, _ = deps(tmp_path)
    assert run(["status"], d) == 0
    assert NOTE in sink.text and "routing: on" not in sink.text


def test_ollama_pass_still_says_routing_on(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    d, sink, _ = deps(tmp_path, passing())
    assert run(["--yes", "--model", MODEL], d) == LCC.EXIT_OK
    assert "routing: on for commit_message" in sink.text and NOTE not in sink.text
