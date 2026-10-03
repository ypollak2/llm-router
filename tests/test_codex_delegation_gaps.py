"""Codex as a real frontier lane: model, time allowance, config and quota.

Evidence behind each behaviour (17 real agent tasks through ``codex exec``,
2026-10-01..03): gpt-6-astra passed 13/17, the same as Claude on the same tasks;
wall time per task had median 91s, 7 of 17 over 120s, max 223s; the ChatGPT Plus
window then ran dry ("...or try again at 11:33 PM.") and every later call failed.

Everything here uses fakes. No real ``codex exec`` is ever started.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from llm_router import provider_reset
from llm_router.codex_agent import CodexResult

REPO = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO / "src" / "llm_router" / "hooks" / "agent-route.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("agent_route_codex_gaps", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _quota_text(hours_ahead: float = 2.0) -> str:
    """The real captured Codex usage-limit line, with the clock time moved to a
    moment safely in the future (a fixed 11:33 PM would be flaky near 23:33,
    because provider_reset does not persist a reset under 60s away)."""
    when = (datetime.now() + timedelta(hours=hours_ahead)).strftime("%I:%M %p")
    return (
        "You've hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits "
        f"or try again at {when}."
    )


def _ok(model: str, content: str = "answer") -> CodexResult:
    return CodexResult(content=content, model=model, exit_code=0, duration_sec=0.1)


def _fail(model: str, content: str) -> CodexResult:
    return CodexResult(content=content, model=model, exit_code=1, duration_sec=0.1)


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
    monkeypatch.setenv("LLM_ROUTER_ROUTE_BANNER", "off")
    monkeypatch.delenv("LLM_ROUTER_CODEX_AGENT_MODEL", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SUBAGENT_CLI_TIMEOUT", raising=False)
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "reset.json"))
    monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
    return _load_hook()


class _Codex:
    """Scripted stand-in for run_codex: one result per call, calls recorded."""

    def __init__(self, monkeypatch, results):
        self.results = list(results)
        self.calls: list[dict] = []
        monkeypatch.setattr("llm_router.codex_agent.run_codex", self)

    async def __call__(self, prompt, model="gpt-5.5", timeout=None, **kw):
        self.calls.append({"model": model, "timeout": timeout, **kw})
        return self.results.pop(0)

    @property
    def models(self):
        return [c["model"] for c in self.calls]


def _ledger(tmp_path_home: str) -> list[dict]:
    f = Path(tmp_path_home) / "north_star_units.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def _ns3(hook, prompt="analyze the auth module for bugs"):
    return hook._try_codex_subagent_delegation(
        prompt, "analyze", "moderate", "general-purpose", "sess-gaps")


# --------------------------------------------------------------------- 1. model


def test_default_model_is_gpt6_astra(hook, monkeypatch):
    codex = _Codex(monkeypatch, [_ok("gpt-6-astra")])
    assert _ns3(hook) == "answer"
    assert codex.models == ["gpt-6-astra"]


def test_model_is_configurable(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CODEX_AGENT_MODEL", "gpt-5.5")
    codex = _Codex(monkeypatch, [_ok("gpt-5.5")])
    assert _ns3(hook) == "answer"
    assert codex.models == ["gpt-5.5"]


def test_phase2_cli_path_also_uses_the_configured_model(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CODEX_AGENT_MODEL", "gpt-5.5-mini")
    monkeypatch.setattr("llm_router.hooks.chain_builder.needs_claude_tools", lambda *a, **k: True)
    monkeypatch.setattr(hook, "_get_remaining_budget", lambda: 10.0)
    monkeypatch.setattr(hook, "_log_cli_savings", lambda *a, **k: None)
    monkeypatch.setattr(hook, "_govern_run", lambda *a, **k: None)
    codex = _Codex(monkeypatch, [_ok("gpt-5.5-mini")])
    out = hook._try_cli_delegation("fix the failing build", "code", "complex", "s", "general-purpose")
    assert out == "answer"
    assert codex.models == ["gpt-5.5-mini"]


@pytest.mark.parametrize("message", [
    "codex: The model `gpt-6-astra` does not exist or you do not have access to it.",
    "codex: Unknown model gpt-6-astra",
    "codex: model gpt-6-astra is not supported when using Codex with a ChatGPT account",
])
def test_unavailable_model_falls_back_to_gpt55(hook, monkeypatch, message):
    codex = _Codex(monkeypatch, [_fail("gpt-6-astra", message), _ok("gpt-5.5", "fallback answer")])
    assert _ns3(hook) == "fallback answer"
    assert codex.models == ["gpt-6-astra", "gpt-5.5"]
    # An unavailable model is not a quota event: nothing is benched.
    assert not provider_reset.is_provider_reset_blocked("codex")


def test_a_plain_failure_does_not_burn_a_second_codex_call(hook, monkeypatch):
    codex = _Codex(monkeypatch, [_fail("gpt-6-astra", "codex: empty completion (no output)")])
    assert _ns3(hook) is None
    assert codex.models == ["gpt-6-astra"]


# ---------------------------------------------------------------------- 2. time


def test_default_timeout_is_300s_not_120s(hook, monkeypatch):
    codex = _Codex(monkeypatch, [_ok("gpt-6-astra")])
    _ns3(hook)
    assert 290 <= codex.calls[0]["timeout"] <= 300  # 300 minus microseconds of setup


def test_timeout_env_override_is_honoured(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_CLI_TIMEOUT", "45")
    codex = _Codex(monkeypatch, [_ok("gpt-6-astra")])
    _ns3(hook)
    assert 40 <= codex.calls[0]["timeout"] <= 45


def test_registered_hook_timeout_outlives_the_delegation_timeout():
    """A hook killed at the host's wall clock dies silently, so the registered
    per-hook timeout must exceed the longest allowance delegation can use."""
    from llm_router.install_hooks import _AGENT_ROUTE_HOOK_TIMEOUT_SEC

    hook = _load_hook()
    assert _AGENT_ROUTE_HOOK_TIMEOUT_SEC >= hook._CODEX_DEFAULT_TIMEOUT_SEC + 20


def test_fallback_attempt_shares_one_allowance(hook, monkeypatch):
    """Primary then fallback must fit inside ONE allowance, not two stacked."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock["t"])

    class _Slow(_Codex):
        async def __call__(self, prompt, model="gpt-5.5", timeout=None, **kw):
            clock["t"] += 100  # the primary burns 100s then reports unavailable
            return await super().__call__(prompt, model=model, timeout=timeout, **kw)

    codex = _Slow(monkeypatch, [
        _fail("gpt-6-astra", "codex: unknown model gpt-6-astra"), _ok("gpt-5.5")])
    assert _ns3(hook) == "answer"
    assert codex.calls[0]["timeout"] == 300
    assert codex.calls[1]["timeout"] == 200


def test_second_delegation_in_one_hook_run_gets_only_what_is_left(hook, monkeypatch):
    clock = {"t": 50.0}
    monkeypatch.setattr(hook.time, "monotonic", lambda: clock["t"])
    codex = _Codex(monkeypatch, [_fail("gpt-6-astra", "Codex timed out after 300s")])
    assert _ns3(hook) is None
    clock["t"] += 300  # the whole allowance is spent
    res, status = hook._run_codex_agent("again", 300, None)
    assert status == "no_time" and res is None
    assert len(codex.calls) == 1  # no second run was started


# ------------------------------------------------------------------- 3. config


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self._lines = lines
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    @property
    def stdout(self):
        return self._gen()

    @property
    def stderr(self):
        return self._empty()

    async def _gen(self):
        for line in self._lines:
            yield (json.dumps(line) + "\n").encode()

    async def _empty(self):
        return
        yield  # pragma: no cover


@pytest.fixture
def fake_exec(monkeypatch):
    from llm_router import codex_agent

    seen: dict = {}

    def install(lines, returncode=0):
        async def _exec(*args, **kwargs):
            seen["args"] = list(args)
            return _FakeProc(lines, returncode)

        monkeypatch.setattr(codex_agent, "find_codex_binary", lambda: "/fake/codex")
        monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", _exec)
        monkeypatch.setattr("llm_router.safe_subprocess.get_safe_env", lambda: {})
        return seen

    return install


def test_codex_never_loads_user_config_but_stays_on_openai(fake_exec):
    from llm_router.codex_agent import run_codex

    seen = fake_exec([{"type": "item.completed", "item": {"text": "OK"}}])
    res = asyncio.run(run_codex("hi", model="gpt-6-astra"))
    args = seen["args"]
    assert res.success
    # The user's Codex config registers llm-router's own MCP server; loading it
    # made the delegated agent call the router again (an observed loop).
    assert "--ignore-user-config" in args
    assert args[args.index("-m") + 1] == "gpt-6-astra"
    assert "model_provider=openai" in args  # still pinned despite ignoring config


# -------------------------------------------------------------------- 4. quota




def test_run_codex_surfaces_the_usage_limit_event(fake_exec):
    """The limit arrives only as top-level `error` / `turn.failed` events, not as
    an item. Before they were read the caller saw 'empty completion (no output)'
    and the reset time never reached provider_reset."""
    from llm_router.codex_agent import run_codex

    text = _quota_text()
    # The event sequence captured from a real quota-exhausted run.
    fake_exec([
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "error", "message": text},
        {"type": "turn.failed", "error": {"message": text}},
    ], returncode=1)
    res = asyncio.run(run_codex("hi", model="gpt-6-astra"))
    assert not res.success
    assert "usage limit" in res.content
    assert "try again at" in res.content
    assert res.content.count("usage limit") == 1  # the two events are not doubled
    assert provider_reset.parse_reset_epoch(res.content) is not None


def test_quota_on_the_only_model_benches_codex_and_says_why(hook, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_CODEX_AGENT_MODEL", "gpt-5.5")
    text = _quota_text()
    codex = _Codex(monkeypatch, [_fail("gpt-5.5", "codex: " + text)])
    assert _ns3(hook) is None

    until = provider_reset.get_provider_reset_until("codex")
    assert until is not None and time.time() < until <= time.time() + 24 * 3600

    rows = _ledger(os.environ["LLM_ROUTER_HOME"])
    quota = [r for r in rows if r["outcome"] == "codex_quota"]
    assert len(quota) == 1  # recorded, not silent
    assert datetime.fromtimestamp(until).strftime("%H:%M") in quota[0]["reason"]
    assert len(codex.calls) == 1


def test_benched_codex_is_skipped_until_reset_and_recorded(hook, monkeypatch):
    provider_reset.record_provider_reset("codex", time.time() + 7200, reason="usage limit")
    codex = _Codex(monkeypatch, [_ok("gpt-6-astra")])
    assert _ns3(hook) is None
    assert codex.calls == []  # no process was started
    rows = _ledger(os.environ["LLM_ROUTER_HOME"])
    assert [r["outcome"] for r in rows if r["outcome"] == "benched"] == ["benched"]
    assert "benched until" in rows[-1]["reason"]


def test_delegation_resumes_once_the_bench_has_passed(hook, monkeypatch):
    # An expired record is not a bench (all_provider_resets drops it on read).
    path = Path(os.environ["LLM_ROUTER_PROVIDER_RESET_PATH"])
    path.write_text(json.dumps({"providers": {"codex": {
        "until": time.time() - 5, "set_at": time.time() - 9000, "reason": "old"}}}))
    codex = _Codex(monkeypatch, [_ok("gpt-6-astra")])
    assert _ns3(hook) == "answer"
    assert codex.models == ["gpt-6-astra"]


def test_quota_on_primary_only_benches_that_model_and_falls_back(hook, monkeypatch):
    text = "codex: " + _quota_text()
    codex = _Codex(monkeypatch, [
        _fail("gpt-6-astra", text), _ok("gpt-5.5", "from 5.5"),
        _ok("gpt-5.5", "second from 5.5"),
    ])
    assert _ns3(hook) == "from 5.5"
    assert codex.models == ["gpt-6-astra", "gpt-5.5"]
    assert provider_reset.is_provider_reset_blocked("codex:gpt-6-astra")
    assert not provider_reset.is_provider_reset_blocked("codex")  # the account still works

    # The next spawn goes straight to gpt-5.5 instead of re-hitting the dead model.
    hook2 = _load_hook()
    assert _ns3(hook2) == "second from 5.5"
    assert codex.models == ["gpt-6-astra", "gpt-5.5", "gpt-5.5"]


def test_quota_on_both_models_benches_the_account(hook, monkeypatch):
    text = "codex: " + _quota_text()
    codex = _Codex(monkeypatch, [_fail("gpt-6-astra", text), _fail("gpt-5.5", text)])
    assert _ns3(hook) is None
    assert codex.models == ["gpt-6-astra", "gpt-5.5"]
    assert provider_reset.is_provider_reset_blocked("codex")


def test_unparseable_usage_limit_still_benches_for_a_bounded_time(hook, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CODEX_AGENT_MODEL", "gpt-5.5")
    _Codex(monkeypatch, [_fail("gpt-5.5", "codex: You've hit your usage limit.")])
    assert _ns3(hook) is None
    until = provider_reset.get_provider_reset_until("codex")
    assert until is not None
    assert 3500 < until - time.time() <= 3600


def test_phase2_path_skips_a_benched_codex(hook, monkeypatch):
    provider_reset.record_provider_reset("codex", time.time() + 7200, reason="usage limit")
    monkeypatch.setattr("llm_router.hooks.chain_builder.needs_claude_tools", lambda *a, **k: True)
    monkeypatch.setattr(hook, "_get_remaining_budget", lambda: 10.0)
    monkeypatch.setattr("llm_router.gemini_cli_agent.is_gemini_cli_available", lambda: False)
    codex = _Codex(monkeypatch, [_ok("gpt-6-astra")])
    assert hook._try_cli_delegation("fix it", "code", "complex", "s") is None
    assert codex.calls == []
    assert any(r["outcome"] == "benched" for r in _ledger(os.environ["LLM_ROUTER_HOME"]))


# ------------------------------------------------- 5. hook timeout is installed


def test_agent_route_hook_is_registered_with_its_timeout():
    from llm_router.install_hooks import _AGENT_ROUTE_HOOK_TIMEOUT_SEC, _register_hook

    settings: dict = {}
    assert _register_hook(settings, "PreToolUse", "Agent", "/x/agent-route.py",
                          _AGENT_ROUTE_HOOK_TIMEOUT_SEC) == "added"
    entry = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert entry["timeout"] == _AGENT_ROUTE_HOOK_TIMEOUT_SEC
    assert _register_hook(settings, "PreToolUse", "Agent", "/x/agent-route.py",
                          _AGENT_ROUTE_HOOK_TIMEOUT_SEC) == "existing"


def test_an_installed_hook_with_the_old_default_timeout_is_upgraded():
    from llm_router.install_hooks import (
        _AGENT_ROUTE_HOOK_TIMEOUT_SEC,
        _hook_is_registered,
        _register_hook,
    )

    settings: dict = {}
    _register_hook(settings, "PreToolUse", "Agent", "/x/agent-route.py")  # no timeout: 60s kill
    assert not _hook_is_registered(settings, "PreToolUse", "Agent", "/x/agent-route.py",
                                   _AGENT_ROUTE_HOOK_TIMEOUT_SEC)
    assert _register_hook(settings, "PreToolUse", "Agent", "/x/agent-route.py",
                          _AGENT_ROUTE_HOOK_TIMEOUT_SEC) == "updated"
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == _AGENT_ROUTE_HOOK_TIMEOUT_SEC
    assert len(settings["hooks"]["PreToolUse"]) == 1  # upgraded in place, not duplicated


@pytest.mark.parametrize("host_json", ["hooks/hooks.json", ".codex-plugin/hooks.json"])
def test_generated_plugin_hooks_carry_the_agent_route_timeout(host_json):
    from llm_router.install_hooks import _AGENT_ROUTE_HOOK_TIMEOUT_SEC

    data = json.loads((REPO / host_json).read_text())
    handlers = [h for groups in data["hooks"].values() for g in groups for h in g["hooks"]
                if h["command"].endswith("/agent-route.py")]
    assert handlers, "agent-route.py is not registered in " + host_json
    assert all(h.get("timeout") == _AGENT_ROUTE_HOOK_TIMEOUT_SEC for h in handlers)
