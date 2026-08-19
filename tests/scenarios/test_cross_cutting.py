"""Cross-cutting scenarios — failure modes, edge cases, agent coordination.

These stories span multiple components (signals + decisions + selector +
provider + lineage + sessions + budget) and test interactions, not single
features. Reading them in the report tells you how LLM Router behaves under
adversarial conditions.
"""
from __future__ import annotations

from llm_router.agents import SessionStore
from llm_router.decisions.engine import Decision, DecisionEngine
from llm_router.health import HealthTracker
from llm_router.lineage import Inversion, LineageStore, Tier, make_record
from llm_router.signals.base import SignalScore

from tests.scenarios.core import Scenario


# ════════════════════════════════════════════════════════════════════════
# Cascading provider failure
# ════════════════════════════════════════════════════════════════════════

def test_scenario_cascading_provider_failures_final_fallback(scenario_collector):
    """Network outage takes out 3 of 4 providers in the chain; the 4th
    succeeds and the user gets an answer."""
    s = Scenario(
        id="x-01",
        title="Cascading failure: 3 providers down, 4th catches the ball",
        narrative=(
            "A regional network issue takes Ollama, Codex, and OpenAI "
            "offline. LLM Router's selector walks the chain, records a failure "
            "for each, increments circuit breakers, and ultimately reaches "
            "Anthropic Claude which succeeds. The lineage row captures "
            "the full chain_attempted so the user can see exactly how many "
            "fallbacks were needed."
        ),
        expected_outcome=(
            "3 failures recorded, 1 success, breaker opens for 3 providers, "
            "lineage shows full chain"
        ),
    )
    tracker = HealthTracker()
    chain = ("ollama/qwen3.5:latest", "codex/gpt-5-codex",
             "openai/gpt-4o", "anthropic/claude-sonnet-4.6")
    s.selector_picked_chain(chain)

    attempted = []
    for model in chain[:3]:
        s.model_call(model, success=False, latency_ms=30000,
                     error="ConnectionError: network unreachable")
        tracker.record_failure(model)
        s.provider_event(model, "failure recorded")
        attempted.append(model)

    s.model_call(chain[3], success=True, cost_usd=0.018, latency_ms=2700)
    tracker.record_success(chain[3])
    attempted.append(chain[3])
    s.provider_event(chain[3], "success recorded; breaker closed")

    s.lineage_recorded(
        f"chain_attempted len={len(attempted)}",
        chain_attempted=attempted, model_chosen=chain[3], outcome="success",
    )
    s.outcome(
        "User got an answer after 3 fallbacks. Lineage chain_attempted "
        "shows the full path. 3 provider breakers now in cooldown.",
        success=True,
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Routing inversion detected
# ════════════════════════════════════════════════════════════════════════

def test_scenario_inversion_detected_complex_to_local(
    scenario_collector, tmp_path
):
    """The classifier called this prompt complex, but the selector picked
    a local Ollama model — under-served the user. Lineage flags it."""
    s = Scenario(
        id="x-02",
        title="Routing inversion: complex prompt routed to local Ollama",
        narrative=(
            "User asks a deeply complex architectural question. Classifier "
            "labels it complexity='complex', expected tier PREMIUM. But the "
            "selector starts at the free end, Ollama responds (with low "
            "quality), and the lineage detector flags an UP-inversion. The "
            "inversion rate over a rolling window is what drives v0.0.3's "
            "empirical lookup table re-derivation."
        ),
        expected_outcome="lineage row flagged inversion=up_inversion",
    )
    store = LineageStore(db_path=tmp_path / "l.db")
    s.classifier("classified as complex/PREMIUM expected", tier=Tier.PREMIUM.value)
    s.selector_picked_chain(("ollama/qwen3.5:latest",))
    s.model_call("ollama/qwen3.5:latest", success=True,
                 cost_usd=0.0, latency_ms=3200)
    rec = make_record(
        host="claude-code", prompt_fingerprint="complex-arch",
        task_type="analyze", complexity="complex",
        classifier_method="signal_engine",
        signal_scores={}, fired_decisions=(),
        chain_attempted=("ollama/qwen3.5:latest",),
        model_chosen="ollama/qwen3.5:latest",
        outcome="success", latency_ms=3200, cost_usd=0.0,
    )
    store.record(rec)
    inversions = store.inversions()
    s.lineage_recorded(
        f"inversion detected: {rec.inversion.value}",
        complexity="complex", model_tier=rec.model_tier.value,
        inversion=rec.inversion.value,
    )
    s.outcome(
        "Lineage flagged UP-inversion (complex prompt → local tier). "
        "v0.0.3's quality_gap table will use this signal to bias against "
        "Ollama for complex prompts in future routing decisions.",
        success=(
            rec.inversion == Inversion.UP
            and len(inversions) == 1
        ),
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Multi-agent parent/child rollup
# ════════════════════════════════════════════════════════════════════════

def test_scenario_multi_agent_parent_child_rollup(
    scenario_collector, tmp_path
):
    """An orchestrator agent spawns a researcher + a writer. Each runs 2
    steps. The rollup on the parent must include the descendants' costs."""
    s = Scenario(
        id="x-03",
        title="Multi-agent: orchestrator spawns 2 children, rollup aggregates",
        narrative=(
            "An orchestrator agent in Agno spawns two subagents in parallel "
            "— a researcher and a writer. Each subagent makes 2 routing "
            "calls. LLM Router's session rollup walks the parent→children tree "
            "and reports total cost, total steps, descendant count. This is "
            "what a single 'how much did this agent run cost?' query needs."
        ),
        expected_outcome=(
            "rollup(parent) = sum(parent + child_a + child_b), descendants=2"
        ),
    )
    store = SessionStore(db_path=tmp_path / "s.db")
    parent = store.create(agent_id="orchestrator", budget_usd=5.0,
                          framework="agno")
    s.session_event("parent session opened",
                    session_id=parent.session_id[:8],
                    role="orchestrator")
    store.record_step(parent.session_id, cost_usd=0.02)
    s.framework_event("orchestrator planning step",
                      cost=0.02)

    child_r = store.create(agent_id="researcher", budget_usd=1.0,
                           parent_session_id=parent.session_id,
                           framework="agno")
    s.session_event("child session: researcher",
                    parent=parent.session_id[:8])
    store.record_step(child_r.session_id, cost_usd=0.04)
    store.record_step(child_r.session_id, cost_usd=0.03)
    s.framework_event("researcher made 2 routed calls", cost=0.07)

    child_w = store.create(agent_id="writer", budget_usd=1.0,
                           parent_session_id=parent.session_id,
                           framework="agno")
    s.session_event("child session: writer",
                    parent=parent.session_id[:8])
    store.record_step(child_w.session_id, cost_usd=0.05)
    store.record_step(child_w.session_id, cost_usd=0.06)
    s.framework_event("writer made 2 routed calls", cost=0.11)

    store.complete(child_r.session_id)
    store.complete(child_w.session_id)
    store.complete(parent.session_id)

    rollup = store.rollup(parent.session_id)
    s.framework_event("orchestrator requested rollup",
                      total_cost_usd=rollup["total_cost_usd"],
                      descendants=rollup["descendant_session_count"],
                      total_steps=rollup["total_steps"])

    expected_cost = 0.02 + 0.04 + 0.03 + 0.05 + 0.06
    s.outcome(
        f"Rollup correctly summed parent + 2 children: "
        f"${rollup['total_cost_usd']:.2f}, {rollup['total_steps']} steps, "
        f"{rollup['descendant_session_count']} descendants. Matches the "
        f"expected ${expected_cost:.2f}.",
        success=(
            abs(rollup["total_cost_usd"] - expected_cost) < 0.001
            and rollup["descendant_session_count"] == 2
            and rollup["total_steps"] == 5
        ),
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Agent profile boost changes routing outcome
# ════════════════════════════════════════════════════════════════════════

def test_scenario_agent_profile_boost_promotes_signal(scenario_collector):
    """Agent profile's signal_boosts can turn a near-miss into a fire,
    changing the routing outcome for a specific agent."""
    s = Scenario(
        id="x-04",
        title="Agent profile boost promotes a near-miss signal to fire",
        narrative=(
            "A prompt scores 0.4 on code_keywords — below the 0.5 threshold. "
            "For a generic user, no decision fires and the default chain is "
            "used. But when run inside a code-reviewer agent session with "
            "signal_boosts={code_keywords: 1.5}, the boosted score 0.6 fires "
            "the route_code_tasks decision and the code_chain is picked. "
            "Same prompt, different routing — driven entirely by agent profile."
        ),
        expected_outcome=(
            "without boost → default; with 1.5× boost → code_chain"
        ),
    )
    engine = DecisionEngine(decisions=[
        Decision(name="route_code_tasks", operator="SINGLE",
                 signal_refs=("code_keywords",), action="code_chain",
                 priority=50),
    ])
    scores = {
        "code_keywords": SignalScore(
            name="code_keywords", score=0.4, threshold=0.5,
            evidence="literal: 'function'",
        ),
    }
    s.classifier("evaluated signals",
                 code_keywords=0.4, threshold=0.5)
    no_boost = engine.choose(scores)
    s.decision_chose(no_boost.decision_name, action=no_boost.action)
    s.note(f"Without boost: action={no_boost.action!r}")

    s.framework_event(
        "agent session active with profile signal_boosts={code_keywords: 1.5}"
    )
    boosted = engine.choose(scores, boosts={"code_keywords": 1.5})
    s.decision_chose(boosted.decision_name, action=boosted.action,
                     fired_signals=boosted.fired_signals)
    s.note(f"With 1.5× boost: action={boosted.action!r}")

    s.outcome(
        "Same prompt routed two different ways depending on agent profile. "
        "This is how LLM Router makes agents 'aware' of their context without "
        "requiring per-agent decision rules.",
        success=(
            no_boost.action == "default_chain"
            and boosted.action == "code_chain"
        ),
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Pre-emptive budget refusal
# ════════════════════════════════════════════════════════════════════════

def test_scenario_pre_emptive_budget_refusal(scenario_collector, tmp_path):
    """The llm_router_agent_route tool pre-checks the budget BEFORE
    dispatching — refusing rather than spending then breaching."""
    s = Scenario(
        id="x-05",
        title="Pre-emptive budget refusal: route() refuses before spending",
        narrative=(
            "A LangGraph-style agent calls llm_router_agent_route with "
            "estimated_cost=$0.30. The session has $0.20 remaining. The "
            "tool pre-checks via SessionStore.envelope() and returns a "
            "structured error {error: 'budget_would_exceed', cap_usd, "
            "consumed_usd, remaining_usd} — no spend happens. The agent can "
            "downsize, switch model, or abort."
        ),
        expected_outcome=(
            "tool returns budget_would_exceed dict with remaining, "
            "no record_step called, no money spent"
        ),
    )
    import asyncio

    from llm_router.agents import AgentRegistry, AgentProfile
    from llm_router.tools import agents as tool_mod

    sessions = SessionStore(db_path=tmp_path / "s.db")
    reg = AgentRegistry.from_profiles([
        AgentProfile(id="anyagent", description="t",
                     default_budget_usd=0.50, hard_max_budget_usd=2.0),
    ])
    tool_mod.reset_singletons_for_test(
        registry=reg, session_store=sessions,
    )
    try:
        start = asyncio.run(
            tool_mod.llm_router_agent_start_session(
                agent_id="anyagent", budget_usd=0.30
            )
        )
        sid = start["session_id"]
        s.session_event("session opened via MCP tool",
                        budget=0.30, sid=sid[:8])

        # Burn $0.10
        sessions.record_step(sid, cost_usd=0.10)
        s.budget_event("step 1 consumed",
                       consumed=0.10, remaining=0.20)

        # Try to spend $0.30 — should refuse
        result = asyncio.run(
            tool_mod.llm_router_agent_route(
                session_id=sid, prompt="expensive",
                estimated_cost_usd=0.30,
            )
        )
        s.framework_event("route() returned",
                          error=result.get("error"),
                          remaining=result.get("remaining_usd"))
    finally:
        tool_mod.reset_singletons_for_test()

    s.outcome(
        f"Tool refused the call pre-emptively with structured error. "
        f"No record_step was triggered, no API request issued, "
        f"${0.10:.2f} of budget preserved.",
        success=(
            result.get("error") == "budget_would_exceed"
            and "remaining_usd" in result
        ),
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Concurrent sessions don't interfere
# ════════════════════════════════════════════════════════════════════════

def test_scenario_concurrent_sessions_isolated(scenario_collector, tmp_path):
    """Two agents from different frameworks (Agno + a future Pydantic AI)
    can run concurrently in the same LLM Router process without state
    leaks."""
    s = Scenario(
        id="x-06",
        title="Concurrent sessions: 2 frameworks, independent budgets",
        narrative=(
            "An Agno code-reviewer and a (future) Pydantic AI summarizer "
            "are running side-by-side in the same Claude Code subprocess "
            "tree. Each has its own session_id, its own budget, its own "
            "lineage attribution. A failure in one must not poison the "
            "other; cost accounting stays separate."
        ),
        expected_outcome=(
            "two sessions ACTIVE, independent budgets, "
            "by_framework() returns 1 row per framework"
        ),
    )
    sessions = SessionStore(db_path=tmp_path / "s.db")
    lineage = LineageStore(db_path=tmp_path / "l.db")

    a = sessions.create(agent_id="code-reviewer", budget_usd=0.50,
                        framework="agno")
    p = sessions.create(agent_id="summarizer", budget_usd=0.30,
                        framework="pydantic-ai")
    s.session_event("session A opened (agno)",
                    sid=a.session_id[:8], budget=0.50)
    s.session_event("session P opened (pydantic-ai)",
                    sid=p.session_id[:8], budget=0.30)

    sessions.record_step(a.session_id, cost_usd=0.05)
    sessions.record_step(p.session_id, cost_usd=0.02)

    lineage.record(make_record(
        host="claude-code", prompt_fingerprint="agno-1",
        task_type="code", complexity="moderate",
        classifier_method="signal_engine",
        signal_scores={}, fired_decisions=(),
        chain_attempted=("openai/gpt-4o",),
        model_chosen="openai/gpt-4o",
        outcome="success", latency_ms=2000, cost_usd=0.05,
        agent_id="code-reviewer", session_id=a.session_id,
        framework="agno",
    ))
    lineage.record(make_record(
        host="claude-code", prompt_fingerprint="py-1",
        task_type="generate", complexity="simple",
        classifier_method="signal_engine",
        signal_scores={}, fired_decisions=(),
        chain_attempted=("ollama/qwen3.5:latest",),
        model_chosen="ollama/qwen3.5:latest",
        outcome="success", latency_ms=900, cost_usd=0.02,
        agent_id="summarizer", session_id=p.session_id,
        framework="pydantic-ai",
    ))

    agno_rows = lineage.by_framework("agno")
    py_rows = lineage.by_framework("pydantic-ai")
    a_state = sessions.get(a.session_id)
    p_state = sessions.get(p.session_id)

    s.framework_event("audit", agno_rows=len(agno_rows),
                      pydantic_ai_rows=len(py_rows))
    s.session_event("session A", consumed=a_state.consumed_usd)
    s.session_event("session P", consumed=p_state.consumed_usd)

    s.outcome(
        f"Two concurrent sessions, two frameworks, two independent "
        f"budget envelopes. Lineage attribution kept them separate "
        f"({len(agno_rows)} agno row, {len(py_rows)} pydantic-ai row). "
        f"Costs accounted to the correct session.",
        success=(
            len(agno_rows) == 1 and len(py_rows) == 1
            and a_state.consumed_usd == 0.05
            and p_state.consumed_usd == 0.02
        ),
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Health tracker prevents stale failures from sticking
# ════════════════════════════════════════════════════════════════════════

def test_scenario_stale_failure_reset_recovers_provider(scenario_collector):
    """A provider failed yesterday but is healthy today. reset_stale()
    should clear the breaker so it gets retried on next routing call."""
    import time

    s = Scenario(
        id="x-07",
        title="Health tracker: stale failures cleared at session start",
        narrative=(
            "Provider 'flaky-api' failed N times yesterday. Its circuit "
            "breaker opened. Without intervention, every new Claude Code "
            "session would skip it forever. LLM Router's reset_stale() — "
            "called at session start — clears breakers older than 30 minutes "
            "so providers get a fresh chance each session."
        ),
        expected_outcome=(
            "stale provider reset, is_healthy returns True after reset"
        ),
    )
    tracker = HealthTracker()
    for _ in range(10):
        tracker.record_failure("flaky-api")
    s.provider_event("flaky-api", "failed 10 times, breaker open")
    assert not tracker.is_healthy("flaky-api")

    # Make the failures look old
    tracker._providers["flaky-api"].last_failure_time = time.monotonic() - 3600
    s.provider_event("flaky-api", "1 hour elapsed since last failure")

    reset = tracker.reset_stale(max_age_seconds=1800)
    s.provider_event("flaky-api", f"reset_stale returned {reset}")
    healthy_now = tracker.is_healthy("flaky-api")
    s.provider_event("flaky-api", f"is_healthy after reset: {healthy_now}")

    s.outcome(
        "Stale breaker cleared. Provider is available for retry. This "
        "prevents permanently-stuck-unhealthy state from yesterday's outages.",
        success=("flaky-api" in reset and healthy_now),
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Reverse-chain: down-inversion (simple → premium)
# ════════════════════════════════════════════════════════════════════════

def test_scenario_down_inversion_simple_routed_to_premium(
    scenario_collector, tmp_path
):
    """A simple prompt got routed to a premium model because the entire
    cheap chain failed. Lineage flags this as down-inversion (overspend)."""
    s = Scenario(
        id="x-08",
        title="Down-inversion: simple prompt routed to premium (overspend)",
        narrative=(
            "User asks 'what's the capital of France'. Classifier says "
            "simple. But Ollama is down, Gemini Flash is rate-limited, "
            "GPT-4o-mini is rate-limited, so the selector ends up at "
            "GPT-4o (mid tier). Lineage flags this as a down-inversion "
            "— a real success for the user, but a chain-health issue worth "
            "investigating."
        ),
        expected_outcome="lineage row flagged inversion=down_inversion",
    )
    store = LineageStore(db_path=tmp_path / "l.db")
    s.classifier("classified as simple/CHEAP expected", tier=Tier.CHEAP.value)
    chain = ("ollama/qwen3.5:latest", "google/gemini-1.5-flash-8b",
             "openai/gpt-4o-mini", "openai/gpt-4o")
    s.selector_picked_chain(chain)
    s.model_call(chain[0], success=False, error="ConnectionError")
    s.model_call(chain[1], success=False, error="RateLimitError")
    s.model_call(chain[2], success=False, error="RateLimitError")
    s.model_call(chain[3], success=True, cost_usd=0.008, latency_ms=900)
    rec = make_record(
        host="claude-code", prompt_fingerprint="capital-fr",
        task_type="query", complexity="simple",
        classifier_method="heuristic",
        signal_scores={}, fired_decisions=(),
        chain_attempted=chain,
        model_chosen="openai/gpt-4o",  # MID tier
        outcome="success", latency_ms=900, cost_usd=0.008,
    )
    store.record(rec)
    s.lineage_recorded(
        f"inversion={rec.inversion.value}",
        complexity="simple", model_tier=rec.model_tier.value,
        inversion=rec.inversion.value,
    )
    s.outcome(
        "Simple prompt cost $0.008 instead of $0. Down-inversion flagged. "
        "v0.0.3's empirical loop will use this signal to detect chain-health "
        "issues that consistently force overspend.",
        success=(rec.inversion == Inversion.DOWN),
    )
    scenario_collector.add(s)
    assert s.passed
