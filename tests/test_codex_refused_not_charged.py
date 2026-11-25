"""#818 N-C (16.1 SHOULD): a Codex attempt REFUSED before any process started (reason_code ``no_ignore_rules``,
``binary_missing``, ``meter_reserved``) must not be charged to the daily budget or the 5 h window. The refusal is
counted on its own (``agent_route_codex_refusals.json``) so nothing is hidden. A dispatched attempt is charged as before."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from llm_router import codex_meter, codex_window, failopen, paths
from llm_router.codex_agent import CodexResult

HOOK_PATH = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "agent-route.py"
PROMPT = "analyze the auth module for bugs"


def _load_hook():
    spec = importlib.util.spec_from_file_location("agent_route_refused", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


class _Codex:
    def __init__(self, monkeypatch, results):
        self.results = list(results)
        self.calls = 0
        monkeypatch.setattr("llm_router.codex_agent.run_codex", self)

    async def __call__(self, prompt, model="gpt-5.5", timeout=None, **kw):
        self.calls += 1
        return self.results.pop(0)


def _refused(code):
    return CodexResult(content="refused before start", model="gpt-6-astra", exit_code=1, duration_sec=0.0, reason_code=code)


@pytest.fixture
def hook(monkeypatch, tmp_path):
    (tmp_path / "codex").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    monkeypatch.setenv("LLM_ROUTER_ROUTE_BANNER", "off")
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "reset.json"))
    for name in ("LLM_ROUTER_CODEX_AGENT_MODEL", "LLM_ROUTER_CODEX_BENCH_PROBE", "LLM_ROUTER_CODEX_WINDOW_BUDGET",
                 "LLM_ROUTER_CODEX_RESERVE_PCT", "LLM_ROUTER_SUBAGENT_CLI_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    failopen.reset_cache()
    failopen.clear()
    return _load_hook()


def _ns3(hook):
    return hook._try_codex_subagent_delegation(PROMPT, "analyze", "moderate", "general-purpose", "sess-refused")


def _daily() -> int:
    f = paths.state_path(codex_meter.DAILY_COUNTER_FILE)
    return json.loads(f.read_text())["count"] if f.exists() else 0


def _refusals() -> dict:
    f = paths.state_path("agent_route_codex_refusals.json")
    return json.loads(f.read_text()) if f.exists() else {}


@pytest.mark.parametrize("code", ["no_ignore_rules", "binary_missing", "meter_reserved"])
def test_a_refused_attempt_is_not_charged_and_is_counted_apart(hook, monkeypatch, code):
    codex = _Codex(monkeypatch, [_refused(code)])
    assert _ns3(hook) is None
    assert codex.calls == 1
    assert _daily() == 0
    assert codex_window.snapshot().used == 0
    ref = _refusals()
    assert ref["count"] == 1 and ref["by_reason"] == {code: 1}


def test_three_refusals_leave_budget_and_window_untouched(hook, monkeypatch):
    """The reviewer's repro: 3 refused attempts left budget count 3 and 3 window entries."""
    _Codex(monkeypatch, [_refused("no_ignore_rules")] * 3)
    for _ in range(3):
        _ns3(hook)
    assert _daily() == 0 and codex_window.snapshot().used == 0
    assert _refusals()["count"] == 3


def test_a_dispatched_failure_is_still_charged(hook, monkeypatch):
    _Codex(monkeypatch, [CodexResult(content="boom", model="gpt-6-astra", exit_code=1, duration_sec=0.4, reason_code="cli_error")])
    assert _ns3(hook) is None
    assert _daily() == 1
    assert codex_window.snapshot().used == 1
    assert _refusals() == {}


def test_a_dispatched_success_is_still_charged(hook, monkeypatch):
    _Codex(monkeypatch, [CodexResult(content="answer", model="gpt-6-astra", exit_code=0, duration_sec=0.2)])
    assert _ns3(hook) is not None
    assert _daily() == 1
    assert codex_window.snapshot().used == 1
    assert _refusals() == {}


def test_fallback_refused_after_a_real_first_dispatch_keeps_the_charge_and_the_canary(hook, monkeypatch):
    """First model really dispatched ('model unavailable'), fallback refused: budget stays 1, canary is not released."""
    from llm_router import codex_bench
    monkeypatch.setenv("LLM_ROUTER_CODEX_AGENT_MODEL", "gpt-6-astra")
    unavailable = CodexResult(content="model gpt-6-astra does not exist or you do not have access to it", model="gpt-6-astra",
                              exit_code=1, duration_sec=0.3, reason_code="cli_error")
    _Codex(monkeypatch, [unavailable, _refused("no_ignore_rules")])
    released = []
    monkeypatch.setattr(codex_bench, "release_canary", lambda claim: released.append(claim))
    monkeypatch.setattr(hook, "_canary_claim", object())
    assert hook._MODEL_UNAVAILABLE_RE.search(unavailable.content)
    assert _ns3(hook) is None
    assert _daily() == 1
    assert released == []
    assert _refusals()["count"] == 1
    assert codex_window.snapshot().used == 1  # the real dispatch keeps its slot; the refused fallback's slot is back


def test_a_failed_window_spend_clears_the_token_so_a_later_refusal_releases_nothing(hook, monkeypatch):
    released = []
    monkeypatch.setattr(codex_window, "release", lambda token, now=None: released.append(token))
    hook._spent_token = 123.0  # the previous attempt's token
    monkeypatch.setattr(codex_window, "reserve", lambda **k: (_ for _ in ()).throw(RuntimeError("lock gone")))
    assert hook._spend_window_slot() is True
    assert hook._spent_token is None
    hook._refund_refused_dispatch("no_ignore_rules")
    assert released == []


def test_the_window_status_line_shows_refusals_before_start(hook, monkeypatch):
    assert "refused" not in codex_window.status_line()
    _Codex(monkeypatch, [_refused("no_ignore_rules"), _refused("binary_missing"), _refused("no_ignore_rules")])
    for _ in range(3):
        _ns3(hook)
    line = codex_window.status_line()
    assert "refused before start: 3 today (binary_missing 1, no_ignore_rules 2)" in line
    assert line.startswith("codex: 0/")
