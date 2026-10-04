"""The rolling 5-hour Codex budget: spend the window on the hardest work.

A ChatGPT Plus account ran dry after about 17 agent tasks per 5h window, and
Codex passes the same share of real tasks as Claude (13/17 each), so the budget
(default 15, below the ~17 seen) goes to complex / deep-reasoning work once less
than half of it remains. Fakes only: no real `codex exec` is started.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import codex_window
from llm_router.codex_agent import CodexResult

HOUR = 3600
REPO = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO / "src" / "llm_router" / "hooks" / "agent-route.py"

DEEP_PROMPT = "Prove that the sum of two even integers is even and derive the general bound"
PLAIN_PROMPT = "analyze the auth module for bugs"


def _load_hook():
    spec = importlib.util.spec_from_file_location("agent_route_codex_window", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _seed(n: int, age_sec: float = 60.0) -> None:
    """Write ``n`` delegations ``age_sec`` old straight into the state file.

    (record_delegation(now=past) would prune the newer stamps as "future".)
    """
    now = time.time()
    path = codex_window._state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"delegations": [now - age_sec - i for i in range(n)]}))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", raising=False)
    monkeypatch.delenv("LLM_ROUTER_CODEX_AGENT_MODEL", raising=False)


# ----------------------------------------------------------------- the counter


def test_budget_default_env_override_and_bad_value(monkeypatch):
    assert codex_window.budget() == 15
    monkeypatch.setenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", "6")
    assert codex_window.budget() == 6
    monkeypatch.setenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", "lots")
    assert codex_window.budget() == 15
    monkeypatch.setenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", "-3")
    assert codex_window.budget() == 0


def test_counter_persists_and_reports_the_reset_of_the_oldest_slot():
    t0 = time.time() - 2 * HOUR
    codex_window.record_delegation(now=t0)
    codex_window.record_delegation(now=t0 + 600)
    s = codex_window.snapshot()
    assert (s.used, s.budget, s.remaining) == (2, 15, 13)
    assert s.resets_at == pytest.approx(t0 + 5 * HOUR)
    assert json.loads(codex_window._state_file().read_text())["delegations"]


def test_delegations_age_out_after_five_hours():
    now = time.time()
    codex_window.record_delegation(now=now - 5 * HOUR - 10)  # just outside
    codex_window.record_delegation(now=now - 5 * HOUR + 60)  # just inside
    s = codex_window.snapshot(now)
    assert s.used == 1
    assert s.resets_at == pytest.approx(now + 60)


def test_a_corrupt_state_file_fails_open_and_recovers():
    path = codex_window._state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert codex_window.snapshot().used == 0
    codex_window.record_delegation()
    assert codex_window.snapshot().used == 1


@pytest.mark.parametrize("used,tier", [
    (0, "open"), (7, "open"),        # 8 of 15 left: not yet under half
    (8, "tight"), (14, "tight"),     # 7 left is under 7.5
    (15, "exhausted"), (20, "exhausted"),
])
def test_tier_boundaries_for_a_budget_of_15(used, tier):
    _seed(used)
    assert codex_window.snapshot().tier == tier


# ------------------------------------------------------------------ admission


def test_open_window_admits_moderate_work():
    _seed(3)
    assert codex_window.admit(complexity="moderate", deep_reasoning=False).allowed


def test_tight_window_admits_only_complex_or_deep_reasoning():
    _seed(10)
    assert not codex_window.admit(complexity="moderate", deep_reasoning=False).allowed
    assert not codex_window.admit(complexity="simple", deep_reasoning=False).allowed
    assert codex_window.admit(complexity="complex", deep_reasoning=False).allowed
    assert codex_window.admit(complexity="moderate", deep_reasoning=True).allowed


def test_exhausted_window_admits_nothing_and_says_when_it_resumes():
    _seed(15)
    adm = codex_window.admit(complexity="complex", deep_reasoning=True)
    assert not adm.allowed and adm.tier == "exhausted"
    assert re.search(r"resumes \d\d:\d\d", adm.reason)


def test_window_rolls_over_as_the_oldest_slots_age_out():
    now = time.time()
    path = codex_window._state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # all 15 used just under 5h ago -> exhausted now
    path.write_text(json.dumps({"delegations": [now - 5 * HOUR + 120 + i for i in range(15)]}))
    assert not codex_window.admit(complexity="complex", deep_reasoning=True, now=now).allowed
    later = now + 300  # they have all left the window
    assert codex_window.admit(complexity="moderate", deep_reasoning=False, now=later).allowed


def test_zero_budget_blocks_all_delegation(monkeypatch):
    """0 is a kill switch (blocks everything), not "no cap"."""
    monkeypatch.setenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", "0")
    adm = codex_window.admit(complexity="complex", deep_reasoning=True)
    assert not adm.allowed and "budget is 0" in adm.reason
    res = codex_window.reserve(complexity="complex", deep_reasoning=True)
    assert not res.allowed and res.token is None
    assert codex_window.snapshot().used == 0


# ---------------------------------------------------------------- status line


def test_status_line_shape_with_use_and_without():
    assert codex_window.status_line() == "codex: 0/15 used this window"
    _seed(3)
    assert re.fullmatch(r"codex: 3/15 used this window, resets \d\d:\d\d", codex_window.status_line())


def test_status_line_reset_is_the_local_clock_time_of_the_oldest_slot():
    t0 = time.time() - HOUR
    codex_window.record_delegation(now=t0)
    expected = time.strftime("%H:%M", time.localtime(t0 + 5 * HOUR))
    assert codex_window.status_line().endswith(f"resets {expected}")


def test_llm_router_status_shows_the_codex_line():
    from rich.console import Console

    from llm_router.ui.status_premium import PremiumStatusCommand

    _seed(4)
    console = Console(record=True, width=120, force_terminal=False)
    PremiumStatusCommand(console=console).print_status()
    out = console.export_text()
    assert re.search(r"codex: 4/15 used this window, resets \d\d:\d\d", out)


# ------------------------------------------------------ the hook's behaviour


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    monkeypatch.setenv("LLM_ROUTER_ROUTE_BANNER", "off")
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "reset.json"))
    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    return _load_hook()


class _Codex:
    def __init__(self, monkeypatch, results=None):
        self.results = list(results or [])
        self.calls: list[str] = []
        monkeypatch.setattr("llm_router.codex_agent.run_codex", self)

    async def __call__(self, prompt, model="gpt-5.5", timeout=None, **kw):
        self.calls.append(model)
        if self.results:
            return self.results.pop(0)
        return CodexResult(content="answer", model=model, exit_code=0, duration_sec=0.1)


def _ledger() -> list[dict]:
    f = Path(os.environ["LLM_ROUTER_HOME"]) / "north_star_units.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def _ns3(hook, prompt=PLAIN_PROMPT, complexity="moderate"):
    return hook._try_codex_subagent_delegation(
        prompt, "analyze", complexity, "general-purpose", "sess-window")


def test_every_dispatch_is_counted(hook, monkeypatch):
    _Codex(monkeypatch)
    assert _ns3(hook) == "answer"
    assert codex_window.snapshot().used == 1
    assert _ns3(hook) == "answer"
    assert codex_window.snapshot().used == 2


def test_a_fallback_dispatch_is_a_second_slot(hook, monkeypatch):
    _Codex(monkeypatch, [
        CodexResult(content="codex: unknown model gpt-6-astra", model="gpt-6-astra",
                    exit_code=1, duration_sec=0.1),
    ])
    assert _ns3(hook) == "answer"
    assert codex_window.snapshot().used == 2


def test_skipped_delegations_spend_nothing(hook, monkeypatch):
    codex = _Codex(monkeypatch)
    # unsuitable: a task type Codex is not used for
    assert hook._try_codex_subagent_delegation(
        "write a poem", "generate", "moderate", "general-purpose", "s") is None
    assert codex.calls == []
    assert codex_window.snapshot().used == 0


def test_open_window_delegates_moderate_work(hook, monkeypatch):
    _seed(5)
    codex = _Codex(monkeypatch)
    assert _ns3(hook) == "answer"
    assert len(codex.calls) == 1


def test_tight_window_declines_moderate_work_and_records_why(hook, monkeypatch):
    _seed(9)
    codex = _Codex(monkeypatch)
    assert _ns3(hook) is None
    assert codex.calls == []  # nothing dispatched
    assert codex_window.snapshot().used == 9  # and nothing spent
    row = [r for r in _ledger() if r["outcome"] == "window_tight"]
    assert len(row) == 1
    assert "only complex or deep-reasoning" in row[0]["reason"]
    assert (row[0]["window_used"], row[0]["window_budget"]) == (9, 15)


def test_tight_window_still_delegates_complex_work(hook, monkeypatch):
    _seed(9)
    codex = _Codex(monkeypatch)
    assert _ns3(hook, complexity="complex") == "answer"
    assert len(codex.calls) == 1
    assert codex_window.snapshot().used == 10


def test_tight_window_still_delegates_deep_reasoning(hook, monkeypatch):
    _seed(9)
    codex = _Codex(monkeypatch)
    assert _ns3(hook, prompt=DEEP_PROMPT, complexity="moderate") == "answer"
    assert len(codex.calls) == 1


def test_exhausted_window_stops_delegating_and_records_why(hook, monkeypatch):
    _seed(15)
    codex = _Codex(monkeypatch)
    assert _ns3(hook, prompt=DEEP_PROMPT, complexity="complex") is None
    assert codex.calls == []
    row = [r for r in _ledger() if r["outcome"] == "window_exhausted"]
    assert len(row) == 1 and re.search(r"resumes \d\d:\d\d", row[0]["reason"])


def test_delegation_resumes_when_the_window_rolls_over(hook, monkeypatch):
    _seed(15, age_sec=5 * HOUR + 30)  # all of them older than the window
    codex = _Codex(monkeypatch)
    assert _ns3(hook) == "answer"
    assert len(codex.calls) == 1


def test_phase2_path_honours_the_window_too(hook, monkeypatch):
    _seed(15)
    monkeypatch.setattr("llm_router.hooks.chain_builder.needs_claude_tools", lambda *a, **k: True)
    monkeypatch.setattr(hook, "_get_remaining_budget", lambda: 10.0)
    monkeypatch.setattr("llm_router.gemini_cli_agent.is_gemini_cli_available", lambda: False)
    codex = _Codex(monkeypatch)
    assert hook._try_cli_delegation("fix the build", "code", "complex", "s") is None
    assert codex.calls == []
    assert any(r["outcome"] == "window_exhausted" and r["path"] == "cli_delegation"
               for r in _ledger())


def test_a_broken_counter_does_not_stop_delegation(hook, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("counter broke")

    monkeypatch.setattr(codex_window, "admit", _boom)
    monkeypatch.setattr(codex_window, "reserve", _boom)
    monkeypatch.setattr(codex_window, "release", _boom)
    monkeypatch.setattr(codex_window, "record_delegation", _boom)
    codex = _Codex(monkeypatch)
    assert _ns3(hook) == "answer"
    assert len(codex.calls) == 1


# ------------------------------------------------- reserve: decide + count, once


def test_reserve_counts_the_slot_and_hands_back_a_token():
    adm = codex_window.reserve(complexity="moderate", deep_reasoning=False)
    assert adm.allowed and adm.token is not None
    assert codex_window.snapshot().used == 1


def test_a_declined_reserve_writes_nothing():
    _seed(15)
    adm = codex_window.reserve(complexity="complex", deep_reasoning=True)
    assert not adm.allowed and adm.token is None
    assert codex_window.snapshot().used == 15


def test_reserve_applies_the_tight_rule_but_not_for_a_fallback():
    _seed(9)  # tight: 6 of 15 left
    assert not codex_window.reserve(complexity="moderate", deep_reasoning=False).allowed
    assert codex_window.snapshot().used == 9
    assert codex_window.reserve(
        complexity="moderate", deep_reasoning=False, enforce_tier=False).allowed
    assert codex_window.snapshot().used == 10


def test_release_gives_the_slot_back():
    _seed(5)
    adm = codex_window.reserve(complexity="moderate", deep_reasoning=False)
    assert codex_window.snapshot().used == 6
    codex_window.release(adm.token)
    assert codex_window.snapshot().used == 5
    codex_window.release(adm.token)  # twice is harmless
    codex_window.release(None)
    assert codex_window.snapshot().used == 5


def test_reserve_fails_open_when_the_state_path_is_unusable(monkeypatch):
    def _boom():
        raise OSError("no state dir")

    monkeypatch.setattr(codex_window, "_state_file", _boom)
    adm = codex_window.reserve(complexity="moderate", deep_reasoning=False)
    assert adm.allowed and adm.token is None


_CHILD = """
import sys, time
from llm_router import codex_window
go_at = float(sys.argv[1])
while time.time() < go_at:
    time.sleep(0.001)
a = codex_window.reserve(complexity="complex", deep_reasoning=True)
print("1" if (a.allowed and a.token is not None) else "0")
"""


def test_eight_processes_at_14_of_15_admit_at_most_one():
    """The review's race: admit() read the window unlocked and record_delegation()
    wrote later, so 8 processes behind a barrier all saw 14/15 and all dispatched
    (final used=20, budget=15). reserve() decides and counts under one lock.

    The barrier is a shared wall-clock start time: every child imports first,
    then spins until the same instant before it reserves."""
    _seed(14)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    go_at = time.time() + 12
    procs = [
        subprocess.Popen([sys.executable, "-c", _CHILD, repr(go_at)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(8)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=90)
        assert p.returncode == 0, err
        outs.append(out.strip())
    assert len(outs) == 8
    admitted = outs.count("1")
    assert admitted == 1, f"{admitted} of 8 admitted at 14/15: {outs}"
    assert codex_window.snapshot().used == 15


# ------------------------------------------- the hook reserves and gives back


def test_the_hook_holds_one_slot_for_a_delegation_and_spends_it(hook, monkeypatch):
    _seed(14)
    _Codex(monkeypatch)
    assert _ns3(hook, complexity="complex") == "answer"
    assert codex_window.snapshot().used == 15  # reserved once, spent by the call


def test_a_delegation_declined_before_any_codex_call_releases_its_slot(hook, monkeypatch):
    _seed(3)
    codex = _Codex(monkeypatch)
    monkeypatch.setattr(hook, "_delegation_time_left", lambda configured: 0)  # no time left
    assert _ns3(hook) is None
    assert codex.calls == []
    assert codex_window.snapshot().used == 3  # the reservation was handed back


def test_phase2_gemini_path_releases_the_codex_reservation(hook, monkeypatch):
    _seed(3)
    monkeypatch.setattr("llm_router.hooks.chain_builder.needs_claude_tools", lambda *a, **k: True)
    monkeypatch.setattr(hook, "_get_remaining_budget", lambda: 10.0)
    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.gemini_cli_agent.is_gemini_cli_available", lambda: False)
    codex = _Codex(monkeypatch)
    assert hook._try_cli_delegation("fix the build", "code", "complex", "s") is None
    assert codex.calls == []
    assert codex_window.snapshot().used == 3


def test_a_usage_limit_hit_mid_run_still_counts(hook, monkeypatch):
    _seed(3)
    _Codex(monkeypatch, [
        CodexResult(content="You've hit your usage limit. Try again in 3 hours.",
                    model="gpt-6-astra", exit_code=1, duration_sec=0.1),
        CodexResult(content="You've hit your usage limit. Try again in 3 hours.",
                    model="gpt-5.5", exit_code=1, duration_sec=0.1),
    ])
    assert _ns3(hook) is None
    assert codex_window.snapshot().used == 5  # both dispatches spent a slot


def test_a_fallback_is_refused_when_the_window_filled_in_between(hook, monkeypatch):
    _seed(14)
    codex = _Codex(monkeypatch, [
        CodexResult(content="codex: unknown model gpt-6-astra", model="gpt-6-astra",
                    exit_code=1, duration_sec=0.1),
    ])
    assert _ns3(hook, complexity="complex") is None  # primary took the 15th slot; the fallback finds none
    assert codex.calls == ["gpt-6-astra"]
    assert codex_window.snapshot().used == 15
