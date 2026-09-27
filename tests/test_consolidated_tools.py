"""North Star P4-S1: the consolidated `llm_act` front-door alias (non-breaking)."""
from __future__ import annotations

from llm_router.tools import consolidated


class _FakeMcp:
    def __init__(self):
        self.registered = []

    def tool(self, *a, **k):
        def deco(fn):
            self.registered.append(fn.__name__)
            return fn
        return deco


def test_llm_act_is_registered():
    m = _FakeMcp()
    consolidated.register(m, should_register=lambda _n: True)
    assert "llm_act" in m.registered


def test_llm_act_respects_slim_gate():
    m = _FakeMcp()
    consolidated.register(m, should_register=lambda _n: False)  # slim mode off
    assert "llm_act" not in m.registered


async def test_llm_act_delegates_to_llm_delegate(monkeypatch):
    seen = {}

    async def fake_delegate(task, budget_usd=1.0, context="", **kw):
        seen.update(task=task, budget=budget_usd, context=context)
        return '{"outcome": "complete"}'

    monkeypatch.setattr("llm_router.tools.consolidated.llm_delegate", fake_delegate)
    out = await consolidated.llm_act("fix the bug", budget_usd=2.0, context="ctx")
    assert seen == {"task": "fix the bug", "budget": 2.0, "context": "ctx"}
    assert "complete" in out


def test_llm_is_registered():
    m = _FakeMcp()
    consolidated.register(m, should_register=lambda _n: True)
    assert "llm" in m.registered and "llm_act" in m.registered


async def test_llm_dispatches_by_task(monkeypatch):
    calls = {}

    def _fake(name, has_complexity=True):
        async def f(prompt, ctx, complexity=None, system_prompt=None, context=None, **kw):
            calls[name] = {"prompt": prompt, "complexity": complexity, "context": context}
            return f"[{name}]"
        return f

    async def _fake_research(prompt, ctx, system_prompt=None, max_tokens=None, context=None, **kw):
        calls["research"] = {"prompt": prompt, "context": context}
        return "[research]"

    monkeypatch.setattr("llm_router.tools.consolidated.llm_query", _fake("query"))
    monkeypatch.setattr("llm_router.tools.consolidated.llm_analyze", _fake("analyze"))
    monkeypatch.setattr("llm_router.tools.consolidated.llm_code", _fake("code"))
    monkeypatch.setattr("llm_router.tools.consolidated.llm_generate", _fake("generate"))
    monkeypatch.setattr("llm_router.tools.consolidated.llm_research", _fake_research)

    assert await consolidated.llm("x", ctx=None, task="code") == "[code]"
    assert await consolidated.llm("x", ctx=None, task="research") == "[research]"
    assert await consolidated.llm("x", ctx=None, task="auto") == "[query]"          # auto -> query
    assert await consolidated.llm("x", ctx=None, task="generate", tier="best") == "[generate]"
    assert calls["code"]["complexity"] == "moderate"                                # balanced tier
    assert calls["generate"]["complexity"] == "complex"                             # best tier
    assert "complexity" not in calls["research"]                                    # research has none


def test_llm_router_status_registered():
    m = _FakeMcp()
    consolidated.register(m, should_register=lambda _n: True)
    assert "llm_router_status" in m.registered


async def test_llm_router_status_dispatches_by_view(monkeypatch):
    hits = {}

    def _mk(name, wants_period=False):
        if wants_period:
            async def f(period="today"):
                hits[name] = period
                return f"[{name}:{period}]"
        else:
            async def f():
                hits[name] = True
                return f"[{name}]"
        return f

    for n in ("llm_savings", "llm_session_savings", "llm_session_spend",
              "llm_health", "llm_providers"):
        monkeypatch.setattr(f"llm_router.tools.consolidated.{n}", _mk(n))
    monkeypatch.setattr("llm_router.tools.consolidated.llm_usage", _mk("llm_usage", wants_period=True))
    monkeypatch.setattr("llm_router.tools.consolidated.llm_gain", _mk("llm_gain", wants_period=True))

    assert await consolidated.llm_router_status("health") == "[llm_health]"
    assert await consolidated.llm_router_status("spend") == "[llm_session_spend]"
    assert await consolidated.llm_router_status("usage", period="week") == "[llm_usage:week]"
    assert await consolidated.llm_router_status() == "[llm_savings]"          # default -> summary/savings
    assert hits["llm_gain"] if await consolidated.llm_router_status("gain") else True


def test_admin_and_session_registered():
    m = _FakeMcp()
    consolidated.register(m, should_register=lambda _n: True)
    assert {"llm_router_admin", "llm_router_session"} <= set(m.registered)


async def test_llm_router_admin_dispatches_by_action(monkeypatch):
    seen = {}

    async def _set(profile):
        seen["set"] = profile
        return "ok-set"

    async def _clear():
        seen["clear"] = True
        return "ok-clear"

    monkeypatch.setattr("llm_router.tools.consolidated.llm_set_profile", _set)
    monkeypatch.setattr("llm_router.tools.consolidated.llm_cache_clear", _clear)
    assert await consolidated.llm_router_admin("set_profile", "budget") == "ok-set"
    assert seen["set"] == "budget"
    assert await consolidated.llm_router_admin("clear_cache") == "ok-clear"
    assert "unknown admin action" in await consolidated.llm_router_admin("bogus")


async def test_llm_router_session_dispatches_by_action(monkeypatch):
    seen = {}

    async def _list():
        return {"agents": []}

    async def _budget(session_id):
        seen["budget"] = session_id
        return {"ok": True}

    async def _lineage(session_id, limit=200):
        seen["lineage"] = (session_id, limit)
        return {"ok": True}

    monkeypatch.setattr("llm_router.tools.consolidated.llm_router_agent_list", _list)
    monkeypatch.setattr("llm_router.tools.consolidated.llm_router_agent_check_budget", _budget)
    monkeypatch.setattr("llm_router.tools.consolidated.llm_router_agent_lineage", _lineage)
    assert await consolidated.llm_router_session("list") == {"agents": []}
    await consolidated.llm_router_session("check_budget", session_id="s1")
    assert seen["budget"] == "s1"
    await consolidated.llm_router_session("lineage", session_id="s2", limit=5)
    assert seen["lineage"] == ("s2", 5)
    assert "error" in await consolidated.llm_router_session("start")   # rich action → use direct tool


async def test_llm_router_status_gain_view_shows_the_estimate_not_the_split(
    tmp_path, monkeypatch
):
    """2026-09-27 product decision: every user-facing savings surface —
    including the MCP `llm_router_status(view="gain")` door — shows ONE
    labelled estimate, never "verified"/"unverified" wording. `gain` reaches
    `dashboard_data.summary().display()` via `commands/gain.py`; this
    exercises the real path end-to-end over a fixture DB, not a mock.
    """
    import sqlite3
    from datetime import datetime, timezone

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE savings_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        session_id TEXT, task_type TEXT,
        estimated_claude_cost_saved REAL NOT NULL, external_cost REAL DEFAULT 0,
        model_used TEXT, host TEXT, input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0, mode TEXT, is_simulated INTEGER)""")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO savings_stats (timestamp, session_id, task_type, "
        "estimated_claude_cost_saved, model_used, host, mode, is_simulated) "
        "VALUES (?, 's1', 'code', 1.25, 'ollama/a', 'claude_code', 'block', 0)",
        (ts,),
    )
    conn.commit()
    conn.close()

    out = await consolidated.llm_router_status("gain", period="today")
    assert "est. saved $1.25 (n=1) vs" in out, out
    assert "verified" not in out.lower(), (
        f"'verified'/'unverified' must not reach the MCP gain view: {out!r}"
    )


def test_deprecated_tools_registry_maps_to_real_doors():
    from llm_router.tools.consolidated import DEPRECATED_TOOLS, door_for_tool
    doors = {"llm", "llm_act", "llm_router_status", "llm_router_admin", "llm_router_session"}
    assert set(DEPRECATED_TOOLS.values()) <= doors, "every mapping must point to a real door"
    # representative mappings
    assert door_for_tool("llm_query") == "llm"
    assert door_for_tool("llm_delegate") == "llm_act"
    assert door_for_tool("llm_savings") == "llm_router_status"
    assert door_for_tool("llm_set_profile") == "llm_router_admin"
    assert door_for_tool("llm_router_agent_list") == "llm_router_session"
    # a door / unmapped name is returned unchanged
    assert door_for_tool("llm") == "llm"
    assert door_for_tool("llm_route") == "llm_route"
