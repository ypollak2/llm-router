"""A1 — live default planner: routes to a model, parses a JSON milestone plan.

No live model: llm_router.router.route_and_call is monkeypatched with a fake.
"""
from __future__ import annotations

import pytest

import llm_router.tools.agentic as tool
from llm_router.agentic.planner import PlanRejected, hybrid_plan


def test_extract_plan_json_fenced_bare_and_junk():
    fenced = '```json\n[{"id":"M1"}]\n```'
    assert tool._extract_plan_json(fenced) == [{"id": "M1"}]
    bare = 'here is the plan: [{"id":"M2"}] done'
    assert tool._extract_plan_json(bare) == [{"id": "M2"}]
    assert tool._extract_plan_json("no json here") is None
    assert tool._extract_plan_json('{"not": "an array"}') is None


class _Resp:
    def __init__(self, content):
        self.content = content


async def _install_fake_route(monkeypatch, content):
    import llm_router.router as router

    async def fake_route(task_type, prompt, **kwargs):
        assert "Task:" in prompt  # the planner prompt reached routing
        return _Resp(content)

    monkeypatch.setattr(router, "route_and_call", fake_route)


async def test_default_planner_parses_routed_plan(monkeypatch):
    await _install_fake_route(
        monkeypatch,
        '[{"id":"M1","description":"do it","acceptance":{"type":"canary","marker":"LIVE_CANARY"}}]',
    )
    plan = await tool._default_planner()("build a thing")
    assert plan[0]["id"] == "M1"
    # and it feeds hybrid_plan → real Milestones with objective checks
    ms = await hybrid_plan("build a thing", tool._default_planner())
    assert ms[0].id == "M1"
    assert ms[0].acceptance({"output": "LIVE_CANARY"}).ok


async def test_default_planner_fails_closed_on_unparseable(monkeypatch):
    await _install_fake_route(monkeypatch, "I couldn't produce a plan, sorry.")
    with pytest.raises(PlanRejected):
        await tool._default_planner()("build a thing")


async def test_default_planner_fails_closed_when_routing_unavailable(monkeypatch):
    import llm_router.router as router

    async def boom(*a, **k):
        raise RuntimeError("router down")

    monkeypatch.setattr(router, "route_and_call", boom)
    with pytest.raises(PlanRejected):
        await tool._default_planner()("x")
