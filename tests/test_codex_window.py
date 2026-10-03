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


def test_zero_budget_disables_delegation(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", "0")
    adm = codex_window.admit(complexity="complex", deep_reasoning=True)
    assert not adm.allowed and "budget is 0" in adm.reason


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
    monkeypatch.setattr(codex_window, "record_delegation", _boom)
    codex = _Codex(monkeypatch)
    assert _ns3(hook) == "answer"
    assert len(codex.calls) == 1
