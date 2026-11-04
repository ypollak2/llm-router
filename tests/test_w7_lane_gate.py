"""W7 text-job-bind: the hook's flag and lane gate (before the router import) answers exactly as the MCP path does.

The gate exists so that a job the router would hand back anyway (no lane, a lane that cannot be verified, the
off switch) is denied without paying for the model libraries first. That is only safe if it can never say "go on"
where `llm_text_job` says "fallback", nor "fallback" where it would have run. Both are run here over the same real
state files, digests, clock and settings (the fixtures of `tests/test_lc12.py`), and compared.
"""

from __future__ import annotations

import asyncio
import time as real_time
from types import SimpleNamespace

import pytest

from llm_router import eligibility_routing as er
from llm_router.local_check import display, routing as lr
from llm_router.text_job_bind import engine
from llm_router.tools import text as T
from llm_router.types import LLMResponse
from tests.test_lc8_routing import BARE, CTX, DIGEST, JOB, NOW, lane_file

DIFF = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
HINT = "Recent commit subjects in this repository (match their style and tense):\n- feat: a thing"
FLAG = "LLM_ROUTER_LOCAL_TEXT_JOBS"


@pytest.fixture
def world(tmp_path, monkeypatch):
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
    clock = SimpleNamespace(time=lambda: NOW, monotonic=real_time.monotonic, sleep=real_time.sleep)
    monkeypatch.setattr(T, "time", clock)
    monkeypatch.setattr(engine, "time", clock)
    monkeypatch.setattr(display, "inventory", lambda: ([BARE], {BARE: DIGEST}))
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX)
    monkeypatch.setattr(er, "_route_time_think", lambda: False)
    yield SimpleNamespace(home=tmp_path, calls=calls)
    er.reset_cache()


def passed(world) -> None:
    lane_file(world.home)
    lr.enable(BARE, JOB, "local_check")


def mcp_path(world, monkeypatch) -> tuple[str, str]:
    """What `llm_text_job` does in the hook's mode (a truthy flag is removed first, as generate_with_text_job does)."""
    import os

    if os.environ.get(FLAG, "").strip().lower() in ("1", "true", "on", "yes"):
        monkeypatch.delenv(FLAG)
    world.calls.clear()
    sink: dict = {}
    asyncio.run(T._llm_text_job(JOB, DIFF, SimpleNamespace(), HINT, sink))
    return sink["status"], sink.get("fallback", "")


def hook_gate() -> tuple[str, str]:
    g = engine.lane_gate(JOB, DIFF, HINT)
    return ("go", "") if g is None else (g.status, g.reason)


def _no_pass(world, monkeypatch):
    pass


def _stale_digest(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(display, "inventory", lambda: ([], {BARE: "sha256:" + "c" * 16}))


def _digest_unreadable(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(display, "inventory", lambda: (_ for _ in ()).throw(OSError("ollama is down")))


def _ctx_mismatch(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(er, "_route_time_ctx", lambda m: CTX // 2)


def _think_mismatch(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(er, "_route_time_think", lambda: True)


def _disabled(world, monkeypatch):
    passed(world)
    lr.disable(BARE, JOB, "manual")


def _demoted(world, monkeypatch):
    passed(world)
    lr.disable(BARE, JOB, "demoted: fallbacks")


def _ok(world, monkeypatch):
    passed(world)


def _off(value):
    def setup(world, monkeypatch):
        passed(world)
        monkeypatch.setenv(FLAG, value)
    return setup


def _flag_on_no_pass(world, monkeypatch):
    monkeypatch.setenv(FLAG, "1")


def _flag_on_with_pass(world, monkeypatch):
    passed(world)
    monkeypatch.setenv(FLAG, "1")


STATES = {
    "passed lane": _ok,
    "no pass at all": _no_pass,
    "digest changed since the pass": _stale_digest,
    "digest cache unreadable": _digest_unreadable,
    "router ctx below the measured ctx": _ctx_mismatch,
    "think setting differs from the pass": _think_mismatch,
    "lane disabled by hand": _disabled,
    "lane demoted": _demoted,
    "flag 0": _off("0"), "flag false": _off("false"), "flag off": _off("off"), "flag nonsense": _off("nonsense"),
    "flag 1 without a pass": _flag_on_no_pass,
    "flag 1 with a pass": _flag_on_with_pass,
}


@pytest.mark.parametrize("state", list(STATES))
def test_the_hook_gate_and_the_mcp_path_decide_the_same_over_the_same_state(world, monkeypatch, state):
    STATES[state](world, monkeypatch)
    verdict = hook_gate()
    status, fallback = mcp_path(world, monkeypatch)
    if verdict[0] == "go":
        assert world.calls == [1] and status == "accepted", (state, status, fallback)
    else:
        assert world.calls == [] and status == "fallback" and verdict == ("fallback", fallback), (state, verdict, status, fallback)


@pytest.mark.parametrize("state,want", [
    ("passed lane", "go"), ("no pass at all", "no_lane"), ("digest changed since the pass", "lane_unverified"),
    ("router ctx below the measured ctx", "lane_unverified"), ("lane demoted", "no_lane"), ("flag 0", "flag_off"),
    ("flag 1 with a pass", "go"), ("flag 1 without a pass", "no_lane"),
])
def test_the_matrix_is_not_vacuous_the_expected_verdicts(world, monkeypatch, state, want):
    STATES[state](world, monkeypatch)
    status, reason = hook_gate()
    assert (reason or status) == want


def test_both_paths_ask_the_router_the_same_question_with_the_same_arguments(world, monkeypatch):
    passed(world)
    seen: list[tuple] = []
    real = er.passed_lane_available

    def spy(job, length, now, **kw):
        seen.append((job, length, now))
        return real(job, length, now, **kw)

    monkeypatch.setattr(er, "passed_lane_available", spy)
    engine.lane_gate(JOB, DIFF, HINT)
    mcp_path(world, monkeypatch)
    assert len(seen) == 2 and seen[0] == seen[1], seen
    assert seen[0][1] == len(DIFF) + len(HINT)
    # an input past the router's own cut is measured after the cut, in both
    big = DIFF + "+" + "y" * 60_000
    seen.clear()
    engine.lane_gate(JOB, big, HINT)
    asyncio.run(T._llm_text_job(JOB, big, SimpleNamespace(), HINT, {}))
    assert seen[0] == seen[1] and seen[0][1] <= 40_000 + len(HINT)


def test_a_gate_that_cannot_be_read_does_not_decide(world, monkeypatch):
    passed(world)
    monkeypatch.setattr(er, "passed_lane_available", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert engine.lane_gate(JOB, DIFF, HINT) is None  # the full path decides


def test_a_denied_gate_leaves_the_routers_own_fallback_row(world):
    from llm_router import text_job_ledger as L

    engine.lane_gate(JOB, DIFF, HINT)  # no pass in this world
    rows = [r for r in (world.home / "text_job_outcomes.jsonl").read_text().splitlines() if r.strip()]
    assert len(rows) == 1 and '"status":"fallback"' in rows[0].replace(" ", "") and L.STATUS_FALLBACK in rows[0]
