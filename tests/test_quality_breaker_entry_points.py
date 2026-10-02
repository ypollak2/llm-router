"""NS4 — every routing entry point checks the quality breaker before routing,
and logs the reason when it skips (CLAUDE.md's "an unlogged branch will cost a
day": every branch that can skip routing must log why).

Each test seeds an OPEN class directly in ``quality_breaker.json`` (rather than
building enough fake ``northstar`` history to trip the state machine — that
transition is already covered by ``tests/test_quality_breaker.py``) and checks
the entry point neither attempts the routed call nor stays silent about it.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import quality_breaker as qb

# Exercises the Q&A routing machinery, which is off by default (LLM_ROUTER_QA_ROUTING).
pytestmark = pytest.mark.usefixtures("qa_routing_on")

REPO_SRC = Path(__file__).resolve().parents[1] / "src"
AUTO_ROUTE_HOOK = REPO_SRC / "llm_router" / "hooks" / "auto-route.py"
AGENT_ROUTE_HOOK = REPO_SRC / "llm_router" / "hooks" / "agent-route.py"


def _seed_open_class(home: Path, key: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    # opened_at must be recent — 0.0 (epoch) reads as "cooldown elapsed ages
    # ago" and the very next evaluation flips straight to half_open.
    (home / qb.STATE_FILE).write_text(json.dumps({
        "classes": {key: {"state": qb.OPEN, "opened_at": time.time(), "n": 25, "failure_rate": 0.9}},
    }), encoding="utf-8")


# ── auto-route.py: the "direct" (zero-Claude) lever ─────────────────────────

def _load_auto_route():
    cached = sys.modules.get("auto_route_qb_ep")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_qb_ep", AUTO_ROUTE_HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_qb_ep"] = module
    spec.loader.exec_module(module)
    return module


def test_auto_route_skips_an_open_direct_class_and_logs_why(monkeypatch, tmp_path):
    home = tmp_path / ".llm-router"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    for k, v in {
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
        "LLM_ROUTER_DIRECT_EXECUTION": "1",
        "LLM_ROUTER_ENFORCE": "suggest",
        "LLM_ROUTER_ZERO_CLAUDE": "on",  # so the lever is "direct", not "drafts"
    }.items():
        monkeypatch.setenv(k, v)

    ar = _load_auto_route()
    monkeypatch.setattr(ar, "_router_dir", lambda: home, raising=False)
    monkeypatch.setattr(ar, "log_routing_decision", lambda **kw: None, raising=False)

    # This exact prompt is used (with LLM_ROUTER_ZERO_CLAUDE=off) by
    # tests/test_i5_draft_auto_revert.py, whose draft-only gate requires
    # task_type in {query, research} to fire at all — confirming it
    # classifies as "query" without live LLM classifiers.
    _seed_open_class(home, qb.class_key("direct", "query"))

    import llm_router.hooks.chain_builder as chain_builder
    import llm_router.hooks.direct_executor as de
    attempted: list[str] = []
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 10.0))
    monkeypatch.setattr(chain_builder, "build_chain",
                         lambda c, z, t: attempted.append("build_chain") or [])
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: False)
    monkeypatch.setattr(de, "execute_chain", lambda *a, **k: attempted.append("execute_chain"))
    monkeypatch.setattr(de, "execute_agent", lambda *a, **k: attempted.append("execute_agent"))

    log: list[str] = []
    monkeypatch.setattr(ar, "_debug_log", log.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"prompt": "What does os.path.join do?", "session_id": "sess-qbep1"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        ar.main()
    except SystemExit as e:
        assert e.code in (0, None)

    assert attempted == [], "an open class must not attempt the routed chain at all"
    skips = [line for line in log if "DIRECT SKIP:" in line]
    assert skips, "the skip must be logged, not silent"
    assert any("quality_breaker" in s and "OPEN" in s for s in skips), skips


# ── agent-route.py: the "agent_route" lever ─────────────────────────────────

def _run_agent_route(tmp_path: Path, prompt: str, open_key: str | None) -> tuple[int, dict | None, list]:
    home = tmp_path / ".llm-router"
    home.mkdir(parents=True, exist_ok=True)
    if open_key:
        _seed_open_class(home, open_key)
    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"prompt": prompt, "subagent_type": "general-purpose"},
    })
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["LLM_ROUTER_HOME"] = str(home)
    env["LLM_ROUTER_SUBAGENT_DIRECT"] = "on"
    env["LLM_ROUTER_SUBAGENT_MODEL_PIN"] = "off"
    env["LLM_ROUTER_ALLOW_SUBAGENTS"] = "off"  # so the routed-spawn path doesn't pre-empt the breaker check
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    result = subprocess.run([sys.executable, str(AGENT_ROUTE_HOOK)], input=payload,
                             capture_output=True, text=True, env=env)
    # This prompt's path through main() logs more than one decision before it
    # is done (budget/cost/pressure checks each append their own
    # agent_calls.json entry and, on some branches, their own stdout JSON
    # object) — irrelevant to what's being tested here, which is only
    # whether a breaker_open decision was logged SOMEWHERE, not whether it
    # was the hook's final word. Parse best-effort; only agent_calls.json
    # is asserted on.
    try:
        parsed = json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        parsed = None
    calls = []
    calls_file = home / "agent_calls.json"
    if calls_file.exists():
        calls = json.loads(calls_file.read_text()).get("calls", [])
    return result.returncode, parsed, calls


def test_agent_route_skips_an_open_agent_route_class_and_logs_why(tmp_path):
    prompt = "please analyze this design thoroughly"
    # "analyze" is _classify_task_type's fallback for prompts matching nothing
    # more specific, and this prompt also contains an explicit "analyze" signal.
    key = qb.class_key("agent_route", "analyze")
    code, out, calls = _run_agent_route(tmp_path, prompt, open_key=key)
    assert code == 0
    assert calls, "the call must be logged, not silent"
    breaker_calls = [c for c in calls if c["decision"].startswith("breaker_open:")]
    assert breaker_calls, [c["decision"] for c in calls]
    assert "quality_breaker" in breaker_calls[0]["decision"]


def test_agent_route_routes_normally_when_class_is_closed(tmp_path):
    prompt = "please analyze this design thoroughly"
    code, out, calls = _run_agent_route(tmp_path, prompt, open_key=None)
    assert code == 0
    assert calls
    assert not any(c["decision"].startswith("breaker_open:") for c in calls)


# ── MCP tools: llm() and llm_act() ──────────────────────────────────────────

class _FakeCtx:
    async def info(self, *a, **k):
        pass

    async def report_progress(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_llm_tool_skips_an_open_mcp_llm_class(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _seed_open_class(tmp_path, qb.class_key("mcp_llm", "query"))

    from llm_router.tools import consolidated

    async def _boom(*a, **k):
        raise AssertionError("llm_query must not be called when the class is open")
    monkeypatch.setattr(consolidated, "llm_query", _boom)

    result = await consolidated.llm("what is 2+2", _FakeCtx(), task="query")
    assert result.startswith("[llm_router] quality_breaker:")
    assert "OPEN" in result


@pytest.mark.asyncio
async def test_llm_act_skips_an_open_mcp_llm_act_class(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _seed_open_class(tmp_path, qb.class_key("mcp_llm_act", "agentic"))

    from llm_router.tools import consolidated

    async def _boom(*a, **k):
        raise AssertionError("llm_delegate must not be called when the class is open")
    monkeypatch.setattr(consolidated, "llm_delegate", _boom)

    result = await consolidated.llm_act("refactor this module")
    assert result.startswith("[llm_router] quality_breaker:")


# ── status/northstar visibility (T-07: quiet when nothing is open) ─────────

def test_status_panel_stays_quiet_with_nothing_open(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    from llm_router.ui.status_premium import PremiumStatusCommand

    rendered = PremiumStatusCommand().render_quality_breaker()
    assert str(rendered) == ""


def test_status_panel_shows_open_classes(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    _seed_open_class(tmp_path, qb.class_key("mcp_llm", "query"))
    from llm_router.ui.status_premium import PremiumStatusCommand

    rendered = str(PremiumStatusCommand().render_quality_breaker())
    assert "Quality breaker" in rendered
    assert "mcp_llm:query" in rendered


@pytest.mark.asyncio
async def test_llm_tool_routes_normally_when_class_is_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))

    from llm_router.tools import consolidated

    called = []

    async def _fake_query(*a, **k):
        called.append(1)
        return "the answer"
    monkeypatch.setattr(consolidated, "llm_query", _fake_query)

    result = await consolidated.llm("what is 2+2", _FakeCtx(), task="query")
    assert called == [1]
    assert result == "the answer"
