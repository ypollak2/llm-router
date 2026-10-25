"""P2.3 score, margin and the pre-registered D1-D8 doors."""
from __future__ import annotations

import filecmp
import importlib.util
import itertools
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from llm_router import chain_builder, failopen, map_reader, resource_map as rm, router
from llm_router.repo_config import RepoConfig
from llm_router.types import Complexity, RoutingProfile, TaskType
from tests import test_tq007_daily_cap_downgrade as tq

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "src/llm_router/hooks/agent-route.py"
BASE_D = [
    "ollama/qwen3:8b", "anthropic/claude-sonnet-4-6", "codex/gpt-5.5",
    "gemini_cli/gemini-2.5-pro", "openai/gpt-4o-mini",
]
D1 = [BASE_D[i] for i in (0, 3, 1, 2, 4)]
T0 = 1_760_000_000


def iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def row(name, pressure=0.2, **extra):
    provider = "anthropic" if name == "claude" else name
    chain_ids = {
        "claude": "anthropic/claude-subscription", "codex": "codex/codex-subscription",
        "gemini_cli": "gemini_cli/gemini-subscription",
    }
    capabilities = {
        "claude": ["generate", "tools", "vision", "thinking"],
        "codex": ["generate", "tools"], "gemini_cli": ["generate", "tools", "vision"],
    }
    r = {
        "resource": name, "kind": "subscription",
        "chain_id": chain_ids.get(name, provider + "/subscription"),
        "status": "connected", "capabilities": capabilities.get(name, ["generate", "tools", "vision"]),
        "quota": {"pressure": rm.quota_value(pressure, "measured", T0)},
        "marginal_cost": {"value": 0.0, "label": "synthetic"}, "competence": None,
    }
    r.update(extra)
    return r


def m1(claude=0.20, codex=0.97, gemini=0.10, policy="balanced"):
    rows = [row("claude", claude), row("codex", codex), row("gemini_cli", gemini)]
    rm._prioritise(rows, policy, time.time())
    return {"schema": rm.SCHEMA, "policy": policy, "resources": rows,
            "generated_at": iso(time.time())}


def put(data):
    rm.write_map(data)
    p = rm.map_path()
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def load_hook():
    spec = importlib.util.spec_from_file_location("p23_agent_route", HOOK)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


# Copied from tests/audit/test_chain_correctness.py; no dependency on tests.audit.
class _FakeConfig:
    def __init__(
        self,
        *,
        available_providers: set[str],
        ollama_models: list[str] | None = None,
        openai_compat_models: list[str] | None = None,
        claude_subscription: bool = False,
        gemini_subscription: bool = False,
        claw_code: bool = False,
        routing_policy: str = "balanced",
        agentic_model: str = "",
    ) -> None:
        self.available_providers = available_providers
        self._ollama_models = ollama_models or []
        self._openai_compat_models = openai_compat_models or []
        self.llm_router_claude_subscription = claude_subscription
        self.llm_router_gemini_subscription = gemini_subscription
        self.llm_router_claw_code = claw_code
        self.llm_router_routing_policy = routing_policy
        self.llm_router_agentic_model = agentic_model
        self.ollama_budget_models = ",".join(self._ollama_models)
        self.openai_compat_models = ",".join(self._openai_compat_models)

    def all_ollama_models(self) -> list[str]:
        return list(self._ollama_models)

    def all_openai_compat_models(self) -> list[str]:
        return list(self._openai_compat_models)


def stub_base(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    # Importing llm_router.gateway setdefaults LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS
    # for the rest of the process (see tests/test_okf_cli_dispatch_scope.py); the
    # replay's BASE must not depend on which tests ran before it on the worker.
    for leaked in ("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "LLM_ROUTER_BLOCK_PROVIDERS"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr("llm_router.dynamic_routing.get_dynamic_model_chain", lambda *a, **k: None)
    for name in ("get_model_failure_rates", "get_model_latency_stats", "get_model_acceptance_scores"):
        monkeypatch.setattr(router.cost, name, AsyncMock(return_value={}))
    monkeypatch.setattr("llm_router.policy.load_org_policy", lambda *a, **k: None)
    monkeypatch.setattr(router, "get_repo_config", lambda *a, **k: RepoConfig())
    monkeypatch.setattr(router, "is_codex_available", lambda: True)
    monkeypatch.setattr(router, "is_gemini_cli_available", lambda: True)
    monkeypatch.setattr(router, "get_active_agent", lambda: "claude_code")
    monkeypatch.setattr("llm_router.agentic_registry.best_agentic_model", lambda *a, **k: None)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.50)


async def base_chain(policy, tt=TaskType.CODE, profile=RoutingProfile.QUOTA_BALANCED):
    config = _FakeConfig(
        available_providers={"anthropic", "openai"},
        ollama_models=["ollama/qwen3:8b"], routing_policy=policy,
    )
    return await router._build_and_filter_chain(
        tt, profile, None, Complexity.MODERATE, Complexity.MODERATE, config,
    )


def test_formula_and_knownness():
    r = row("claude", .2, effective_priority=2, reset_at=iso(T0 + 3600),
            marginal_cost={"value": .1})
    r["quota"]["weekly_pressure"] = rm.quota_value(.7, "estimated", T0)
    value, e = rm.score(r, {"now": T0, "priority_ordered": True})
    assert value == pytest.approx(1 / 2 - 10 * .7 + .05 - .1)
    assert set(e) == {"resource", "score", "terms", "inputs"}
    assert e["inputs"] == {
        "effective_priority": 2., "pressure": .7, "reset_in_h": 1., "marginal_cost": .1,
    }
    assert e["terms"] == {
        "priority": .5, "pressure": -7., "reset": .05, "marginal_cost": -.1,
    }
    for provenance in ("default", "stale"):
        r["quota"] = {"pressure": rm.quota_value(.1, provenance)}
        assert rm.score(r)[1]["terms"]["pressure"] == "unknown"
    r["reset_at"] = iso(T0 - 1)
    assert rm.score(r, {"now": T0})[1]["terms"]["reset"] == "unknown"
    r["reset_at"] = iso(T0 + 36)
    assert rm.score(r, {"now": T0})[1]["terms"]["reset"] == .2
    r["reset_at"] = datetime.fromtimestamp(T0 + 3600, timezone.utc).replace(tzinfo=None).isoformat()
    assert rm.score(r, {"now": T0})[1]["inputs"]["reset_in_h"] == 1.
    for ordered in (None, False, 1, "true"):
        assert rm.score(r, {"priority_ordered": ordered})[1]["terms"]["priority"] == "unknown"
    r["effective_priority"] = True
    assert rm.score(r, {"priority_ordered": True})[1]["terms"]["priority"] == "unknown"
    assert rm.score(None, None)[0] == 0.0


@pytest.mark.parametrize("changes,decision,eligible", [
    ({"status": None}, {}, False), ({"status": "absent"}, {}, False),
    ({"capabilities": None}, {}, False), ({"capabilities": []}, {}, False),
    ({}, {"needs": ["tools", 3]}, True), ({}, {"needs": ["audio"]}, False),
    ({}, {"needs": "audio"}, True),
    ({"competence": {"code": {"a": {"eligible": False}}}}, {"task_type": "code"}, False),
    ({"competence": {"code": {"a": {"eligible": False}, "b": {}}}},
     {"task_type": "code"}, True),
    ({"competence": {"code": {}}}, {"task_type": "code"}, True),
    ({"competence": None}, {"task_type": "code"}, True),
])
def test_eligibility(changes, decision, eligible):
    r = row("claude", **changes)
    ordered, explains = rm.order_resources([r], decision)
    assert (rm._eligible(r, decision) is None) is eligible
    assert ordered == ([r] if eligible else [])
    assert ("rank" in explains[0]) is eligible


def test_groups_ties_and_bad_iterables():
    known, unknown, high = row("known", .94), row("unknown"), row("high", .95)
    unknown["quota"] = {}
    assert rm.score(unknown)[0] > rm.score(known)[0]
    assert rm.order_resources([high, unknown, known])[0] == [known, unknown, high]
    a = row("a", .1, marginal_cost={})
    b = row("b", .2)
    assert rm.order_resources([b, a])[0] == [a, b]
    a["kind"] = b["kind"] = "api"
    assert rm.order_resources([a, b])[0] == [b, a]
    a, b = row("a"), row("b")
    assert rm.order_resources([a, b])[0] == [a, b]
    for bad in (None, 1, "rows"):
        assert rm.order_resources(bad) == ([], [])


def test_order_chain_slots_and_d6():
    chain = ["codex/a", "anthropic/x", "codex/b", "gemini_cli/g", "anthropic/y", "ollama/l"]
    assert rm.order_chain(chain, m1(), {"task_type": "code"})[0] == [
        "gemini_cli/g", "anthropic/x", "anthropic/y", "codex/a", "codex/b", "ollama/l",
    ]
    chain += ["openai/a"]
    held = rm.order_chain(chain, m1(), hold_providers={"anthropic"}, exempt="codex/a")[0]
    assert held == [
        "codex/a", "anthropic/x", "gemini_cli/g", "codex/b",
        "anthropic/y", "ollama/l", "openai/a",
    ]
    data = m1()
    data["resources"][2]["status"] = "absent"
    assert rm.order_chain(chain, data)[0] == [
        "anthropic/x", "anthropic/y", "codex/a", "codex/b",
        "gemini_cli/g", "ollama/l", "openai/a",
    ]
    assert rm.order_chain(chain, None) == (chain, [])
    wrapped = [{"id": x} for x in chain]
    assert [m["id"] for m in rm.order_chain(wrapped, m1(), key=lambda m: m["id"])[0]] == [
        "gemini_cli/g", "anthropic/x", "anthropic/y", "codex/a", "codex/b",
        "ollama/l", "openai/a",
    ]


def test_margin_grid():
    minimum, count = float("inf"), 0
    grid = itertools.product(
        (.95, .97, 1.), (0., .5, .79, .7999),
        (None, 1, 2, 3), (None, 1, 2, 3),
        (.25, 1, 5, 168, None), (.25, 1, 5, 168, None),
    )
    for pa, pb, ra, rb, ha, hb in grid:
        a, b = row("a", pa, effective_priority=ra), row("b", pb, effective_priority=rb)
        for r, h in ((a, ha), (b, hb)):
            if h is not None:
                r["reset_at"] = iso(T0 + h * 3600)
        decision = {"now": T0, "priority_ordered": True}
        assert rm.order_resources([a, b], decision)[0] == [b, a]
        gap = rm.score(b, decision)[0] - rm.score(a, decision)[0]
        assert gap > .3
        minimum = min(minimum, gap)
        count += 1
    assert count == 4800
    assert minimum == pytest.approx(.301)
    print("P2.3 minimum margin:", minimum)


async def drive(monkeypatch, *, tt=TaskType.CODE, profile=RoutingProfile.QUOTA_BALANCED,
                cap=False, override=None):
    """Use the TQ-007 harness, capturing the entire final dispatch chain."""
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "qwen3:8b")
    real_route = router.route_and_call
    real_score, real_sink, real_strip = (
        router._quota_score_chain, router.sink_unvalidated, router._strip_local_for_qa,
    )
    events, captured, scored = [], [], []

    async def route(*args, **kwargs):
        kwargs.update(profile=profile, model_override=override)
        return await real_route(*args, **kwargs)

    def score(*args, **kwargs):
        events.append("score")
        result = real_score(*args, **kwargs)
        scored.append(result)
        return result

    def sink(*args, **kwargs):
        events.append("sink")
        return real_sink(*args, **kwargs)

    def strip(*args, **kwargs):
        events.append("strip")
        return real_strip(*args, **kwargs)

    async def dispatch(**kwargs):
        captured.append(list(kwargs["models_to_try"]))
        return tq._response(kwargs["models_to_try"][0])

    with monkeypatch.context() as m:
        m.setattr(router, "route_and_call", route)
        m.setattr(router, "_quota_score_chain", score)
        m.setattr(router, "sink_unvalidated", sink)
        m.setattr(router, "_strip_local_for_qa", strip)
        m.setattr(router, "_dispatch_model_loop", dispatch)
        await tq._run(BASE_D, task_type=tt, total_cap=.0001 if cap else None)
    assert len(captured) == 1
    return captured[0], scored, events


async def test_d1_d2_d3_router_site(monkeypatch):
    put(m1())
    chain, scored, events = await drive(monkeypatch)
    assert chain == D1
    assert scored == [D1]
    assert events == ["sink", "score", "strip"]
    chain, _, _ = await drive(monkeypatch, cap=True)
    assert chain == [BASE_D[i] for i in (0, 3, 2)]
    chain, scored, events = await drive(monkeypatch, tt=TaskType.QUERY)
    assert scored == [[BASE_D[i] for i in (0, 1, 3, 2, 4)]]
    assert chain == [BASE_D[i] for i in (1, 3, 2, 4, 0)]
    balanced, _, events = await drive(monkeypatch, profile=RoutingProfile.BALANCED)
    assert "score" not in events
    put(m1(.97, .10, .20))
    swapped, _, events = await drive(monkeypatch, profile=RoutingProfile.BALANCED)
    assert balanced == swapped
    _, _, events = await drive(monkeypatch, override="codex/gpt-5.5")
    assert "score" not in events


def test_router_helper_fresh_and_d7_fallback(monkeypatch):
    put(m1(.97, .10, .20))
    code = router._quota_score_chain(BASE_D, TaskType.CODE)
    assert code.index(BASE_D[2]) < code.index(BASE_D[3]) < code.index(BASE_D[1])
    qa = router._quota_score_chain(BASE_D, TaskType.QUERY)
    assert qa.index(BASE_D[1]) == 1
    rm.map_path().unlink()
    calls = []
    monkeypatch.setattr(rm, "live_subscription_rows", lambda: calls.append(True) or m1())
    assert router._quota_score_chain(BASE_D, TaskType.CODE) == D1
    assert calls == [True]


async def test_d5_map_filter_only(monkeypatch):
    stub_base(monkeypatch)
    no_map = await base_chain("balanced")
    rows = [row("codex", .97, effective_priority=1),
            row("gemini_cli", .10, effective_priority=2),
            row("claude", .20, effective_priority=4),
            row("ollama", kind="local", chain_id=BASE_D[0], effective_priority=3),
            row("openai", kind="api", chain_id="openai/api", status="absent")]
    data = m1(policy="cost")
    data["resources"] = rows
    put(data)
    filtered = [m for m in no_map if not m.startswith("openai/")]
    assert await base_chain("balanced") == filtered
    balanced = await base_chain("balanced", profile=RoutingProfile.BALANCED)
    assert balanced != filtered
    rows[0]["quota"]["pressure"]["value"] = .10
    rows[2]["quota"]["pressure"]["value"] = .97
    put(data)
    assert await base_chain("balanced") == filtered
    with monkeypatch.context() as m:
        m.setattr(map_reader, "load_view", lambda *a, **k: None)
        assert await base_chain("balanced", profile=RoutingProfile.BALANCED) == no_map


async def test_d8_compatibility_builder(monkeypatch):
    put(m1())
    monkeypatch.setattr(chain_builder, "_build_dynamic_chain", AsyncMock(return_value=BASE_D))
    monkeypatch.setattr(
        "llm_router.subscription_local_routing.is_subscription_local_active", lambda *a: False,
    )
    assert await chain_builder.build_chain(
        TaskType.CODE, "moderate", RoutingProfile.QUOTA_BALANCED,
    ) == D1
    assert await chain_builder.build_chain(
        TaskType.CODE, "moderate", RoutingProfile.BALANCED,
    ) == BASE_D
    monkeypatch.setattr(chain_builder, "_build_dynamic_chain", AsyncMock(side_effect=RuntimeError))
    monkeypatch.setattr(chain_builder, "_static_chain", lambda *a: list(BASE_D))
    assert await chain_builder.build_chain(TaskType.CODE, "moderate", "quota_balanced") == D1


def test_d4_agent_gate(monkeypatch):
    hook = load_hook()
    records = []
    monkeypatch.setattr(hook, "_record_north_star_unit", lambda *a, **k: records.append(k))
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "quota_balanced")

    def gate():
        return hook._codex_quota_spread_declines("code", "general-purpose", "moderate", "s")

    put(m1())
    assert gate() is True
    assert records[-1]["outcome"] == "quota_spread"
    put(m1(.97, .10))
    assert gate() is False
    for pressure, expected in ((.40, True), (.96, False)):
        data = m1(pressure, 0.)
        data["resources"][1]["quota"]["pressure"]["provenance"] = "default"
        put(data)
        assert gate() is expected
    failures = []
    with monkeypatch.context() as m:
        def broken():
            raise RuntimeError("map")
        m.setattr(map_reader, "load_data", broken)
        m.setattr(failopen, "record", lambda code, exc: failures.append(code))
        assert gate() is False
    assert failures == ["CHZ-FO-AGENT-ROUTE-QUOTA-SCORE"]
    put(m1())   # a map on which the gate would decline, so "env unset" is not vacuous
    assert gate() is True
    monkeypatch.delenv("LLM_ROUTER_PROFILE")
    monkeypatch.delenv("LLM_ROUTER_COST_PROFILE", raising=False)
    assert gate() is False
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "quota_balanced")
    rm.map_path().unlink()
    assert gate() is False
    monkeypatch.delenv("LLM_ROUTER_PROFILE")
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", " quota_balanced ")
    put(m1())
    assert gate() is True


def test_d4h_fresh_process_and_hook_copies(tmp_path):
    put(m1())
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"),
               HOME=os.environ["HOME"], LLM_ROUTER_HOME=os.environ["LLM_ROUTER_HOME"],
               LLM_ROUTER_PROFILE="quota_balanced")
    script = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("p23_fresh", sys.argv[1])
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
hook._record_north_star_unit = lambda *a, **k: None
assert hook._codex_quota_spread_declines("code", "general-purpose", "moderate", "s")
assert "llm_router.router" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(HOOK)], env=env,
        capture_output=True, text=True, timeout=20, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    other = REPO / "hooks/agent-route.py"
    assert filecmp.cmp(HOOK, other, shallow=False)
    for path in (HOOK, other):
        version = next(s for s in path.read_text().splitlines()
                       if s.startswith("# llm_router-hook-version:"))
        assert version == "# llm_router-hook-version: 25"


def test_load_data_freshness_and_cache(monkeypatch):
    data = m1()
    put(data)
    view = map_reader.load_view()
    assert view is not None
    assert map_reader.load_data() == data
    monkeypatch.setattr(rm, "load_map", lambda *a: pytest.fail("cache not used"))
    assert map_reader.load_data() == data
    assert map_reader.load_data(now=time.time() + map_reader.MAX_AGE_S + 60) is None
    assert map_reader.load_data(now=time.time() - 600) is None
    rm.map_path().unlink()
    assert map_reader.load_data() is None


def test_live_rows_isolate_failures(monkeypatch):
    calls, failures = [], []

    def builder(seat, inp, now):
        assert seat.present and seat.status == "connected" and seat.kind == "live"
        assert inp.codex_sessions is None
        calls.append(now)
        return row("codex")

    def broken(*args):
        raise RuntimeError("reading")

    monkeypatch.setattr(rm, "_claude_row", broken)
    monkeypatch.setattr(rm, "_codex_row", builder)
    monkeypatch.setattr(rm, "_gemini_row", builder)
    monkeypatch.setattr(failopen, "record", lambda code, exc: failures.append(code))
    data = rm.live_subscription_rows(T0)
    assert data["schema"] == rm.SCHEMA and data["policy"] is None
    assert len(data["resources"]) == 2
    assert calls == [T0, T0]
    assert failures == ["CHZ-FO-QUOTA-SCORE-LIVE-ROW"]
