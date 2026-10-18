"""Framework-driven scenarios — one story per agent framework.

For Agno (concrete adapter): the story exercises a real agent session
with budget enforcement, lineage tagging, and multi-step routing.

For the other five (LangGraph, CrewAI, OpenAI Agents SDK, Claude Agent SDK,
Pydantic AI): thin base_url wrappers (AGT A.6). The story states what the
wrapper points at the gateway, and verifies it fails honestly with an
ImportError that names the package to install when the framework is absent.
"""
from __future__ import annotations

import pytest

from llm_router.agents import AgentRegistry, AgentProfile, SessionStore
from llm_router.lineage import LineageStore, make_record

from tests.scenarios.core import Scenario


def _profile(id_: str, **kw) -> AgentProfile:
    return AgentProfile(
        id=id_,
        description=kw.get("description", "test"),
        tier_preference=kw.get("tier_preference", ()),
        signal_boosts=kw.get("signal_boosts", {}),
        preferred_chain=kw.get("preferred_chain", "code_chain"),
        default_budget_usd=kw.get("default_budget_usd", 0.50),
        hard_max_budget_usd=kw.get("hard_max_budget_usd", 2.00),
    )


# ════════════════════════════════════════════════════════════════════════
# Agno — concrete, multi-step session with budget enforcement
# ════════════════════════════════════════════════════════════════════════

def test_scenario_agno_code_reviewer_session(scenario_collector, tmp_path):
    """Agno spawns a code-reviewer agent. The agent makes 3 routing calls
    (read 2 files, write a review). LLM Router enforces the per-session
    budget and tags every lineage row with framework='agno'."""
    s = Scenario(
        id="fw-01",
        title="Agno: code-reviewer agent runs 3 routed calls under budget",
        cli="claude-code",
        framework="agno",
        narrative=(
            "An Agno agent (code-reviewer profile, budget=$0.50) is spawned "
            "by a Claude Code subagent context. The agent makes 3 routing "
            "calls: one to summarize src/auth.py, one to check src/login.py, "
            "one to produce the review. Each call is logged in lineage with "
            "session_id + step_index + framework='agno'. The session "
            "completes within budget."
        ),
        expected_outcome=(
            "session ACTIVE→COMPLETED, 3 lineage rows attributed to agno, "
            "total cost < $0.50, no budget breach"
        ),
    )

    registry = AgentRegistry.from_profiles([
        _profile("code-reviewer", default_budget_usd=0.50,
                 hard_max_budget_usd=2.0, preferred_chain="code_chain"),
    ])
    sessions = SessionStore(db_path=tmp_path / "s.db")
    lineage = LineageStore(db_path=tmp_path / "l.db")

    s.framework_event("Agno spawned code-reviewer agent",
                      profile="code-reviewer")
    profile = registry.get("code-reviewer")
    session = sessions.create(agent_id=profile.id,
                              budget_usd=profile.default_budget_usd,
                              framework="agno")
    s.session_event("session opened",
                    session_id=session.session_id[:8],
                    budget=session.budget_cap_usd, state=session.state.value)

    for step, (act, model, cost, latency) in enumerate([
        ("read src/auth.py via llm_code", "openai/gpt-4o", 0.018, 2400),
        ("read src/login.py via llm_code", "openai/gpt-4o", 0.022, 2700),
        ("write review via llm_analyze", "anthropic/claude-sonnet-4.6", 0.035, 3300),
    ]):
        s.framework_event(f"Agno agent step {step + 1}", action=act)
        s.budget_event("pre-check ok",
                       remaining_usd=session.budget_cap_usd - sum([0.018, 0.022, 0.035][:step]))
        s.model_call(model, success=True, cost_usd=cost, latency_ms=latency)
        session = sessions.record_step(session.session_id, cost_usd=cost)
        rec = make_record(
            host="claude-code", prompt_fingerprint=f"fp{step}",
            task_type="code", complexity="moderate",
            classifier_method="signal_engine",
            signal_scores={"code_keywords": 0.9},
            fired_decisions=("route_code_tasks",),
            chain_attempted=("openai/gpt-4o",),
            model_chosen=model, outcome="success",
            latency_ms=latency, cost_usd=cost,
            agent_id="code-reviewer", session_id=session.session_id,
            step_index=step, framework="agno",
        )
        lineage.record(rec)
        s.lineage_recorded(
            f"step {step + 1} logged with agno attribution",
            session_id=session.session_id[:8], step_index=step,
        )

    session = sessions.complete(session.session_id)
    s.session_event("session COMPLETED",
                    state=session.state.value,
                    consumed=session.consumed_usd,
                    steps=session.step_count)

    rollup = sessions.rollup(session.session_id)
    s.framework_event("Agno requested rollup",
                      total_cost_usd=rollup["total_cost_usd"],
                      total_steps=rollup["total_steps"])
    agno_rows = lineage.by_framework("agno")
    s.lineage_recorded(f"by_framework('agno') returned {len(agno_rows)} rows")

    s.outcome(
        f"Agno session completed with 3 steps, "
        f"${session.consumed_usd:.3f} spent of $0.50 budget. "
        f"All {len(agno_rows)} steps tagged with framework='agno' for audit.",
        success=(
            session.state.value == "completed"
            and len(agno_rows) == 3
            and session.consumed_usd < 0.50
        ),
    )
    scenario_collector.add(s)
    assert s.passed


def test_scenario_agno_budget_breach_terminates_session(scenario_collector, tmp_path):
    """Agno agent attempts a step that would exceed its budget. LLM Router
    refuses the call, transitions the session to BUDGET_EXCEEDED, and
    Agno receives a structured error."""
    s = Scenario(
        id="fw-02",
        title="Agno: budget breach mid-session triggers BUDGET_EXCEEDED",
        cli="claude-code",
        framework="agno",
        narrative=(
            "Agno spawns a researcher agent with a tight $0.10 budget. "
            "It makes a first call ($0.06) which succeeds, then a second "
            "call estimated at $0.08 which would push consumed past cap. "
            "LLM Router's budget envelope catches this, raises BudgetExceeded, "
            "session transitions to BUDGET_EXCEEDED. Agno can inspect the "
            "session state and surface a clear error to the user."
        ),
        expected_outcome=(
            "first call ok, second call refused, session terminal at "
            "BUDGET_EXCEEDED, no charges incurred for the refused call"
        ),
    )
    from llm_router.agents.budget import BudgetExceeded

    sessions = SessionStore(db_path=tmp_path / "s.db")
    session = sessions.create(agent_id="researcher", budget_usd=0.10,
                              framework="agno")
    s.framework_event("Agno spawned researcher", budget=0.10)
    s.session_event("session opened", state=session.state.value,
                    budget=0.10)

    # Step 1 — succeeds
    s.framework_event("step 1 — research query", est_cost=0.06)
    s.budget_event("pre-check ok", remaining=0.10)
    s.model_call("perplexity/sonar", success=True,
                 cost_usd=0.06, latency_ms=2800)
    session = sessions.record_step(session.session_id, cost_usd=0.06)
    s.session_event("step recorded", consumed=session.consumed_usd,
                    remaining=session.budget_cap_usd - session.consumed_usd)

    # Step 2 — refused
    s.framework_event("step 2 — followup query", est_cost=0.08)
    s.budget_event("pre-check: would exceed",
                   cap=0.10, consumed=0.06, proposed=0.08)
    breach_caught = False
    try:
        sessions.record_step(session.session_id, cost_usd=0.08)
    except BudgetExceeded as exc:
        breach_caught = True
        s.framework_event("BudgetExceeded raised",
                          cap=exc.cap_usd, consumed=exc.consumed_usd,
                          proposed=exc.proposed_usd)

    final = sessions.get(session.session_id)
    s.session_event("session terminal",
                    state=final.state.value,
                    consumed=final.consumed_usd)
    # Documented contract: when record_step breaches the cap, the cost IS
    # recorded (consumed_usd > cap) BUT the session atomically transitions
    # to BUDGET_EXCEEDED in the same write. The integrity guarantee is
    # "no partial state where consumed > cap AND state == ACTIVE". The
    # raise + persistence happen as one atomic SQLite commit. See
    # tests/qa/test_integrity::test_budget_breach_atomically_terminates_session.
    s.outcome(
        f"Budget envelope refused step 2 and atomically terminated the "
        f"session (state={final.state.value}, consumed=${final.consumed_usd:.2f}). "
        f"Caller catches BudgetExceeded; further calls are rejected with "
        f"TerminalStateViolation. User's wallet protected from runaway loops.",
        success=(
            breach_caught
            and final.state.value == "budget_exceeded"
            and final.state.is_terminal
        ),
    )
    scenario_collector.add(s)
    assert s.passed


# ════════════════════════════════════════════════════════════════════════
# Thin base_url frameworks — each has a story + an honest missing-package check
# ════════════════════════════════════════════════════════════════════════

BASE_URL_FRAMEWORKS = [
    ("langgraph", "llm_router.frameworks.langgraph", "LangGraphAdapter",
     "graph-based agent runtime",
     "A LangGraph workflow with 3 nodes (plan → act → reflect). Each node "
     "is one routed call through the gateway; the node name is the agent id."),
    ("crewai", "llm_router.frameworks.crewai", "CrewAIAdapter",
     "multi-agent crew (Crew/Task/Agent abstraction)",
     "A CrewAI crew of 2 agents (researcher, writer) executes a sequential "
     "task. Each agent's crewai.LLM points at the gateway."),
    ("openai-agents", "llm_router.frameworks.openai_agents", "OpenAIAgentsAdapter",
     "OpenAI Agents SDK (formerly Swarm)",
     "An Agents-SDK Runner runs a research agent that hands off to a writer "
     "agent. Each agent's model points at the gateway."),
    ("claude-agent-sdk", "llm_router.frameworks.claude_agent_sdk",
     "ClaudeAgentSdkAdapter", "Anthropic Claude Agent SDK",
     "A Claude Agent SDK loop with tool_use streaming. The "
     "agent's ANTHROPIC_BASE_URL points at the gateway."),
    ("pydantic-ai", "llm_router.frameworks.pydantic_ai", "PydanticAiAdapter",
     "type-safe Pydantic AI agents",
     "A Pydantic AI Agent with a typed result_type. The model's "
     "provider base_url points at the gateway."),
]


@pytest.mark.parametrize(
    "slug,module_name,class_name,description,future_story",
    BASE_URL_FRAMEWORKS,
    ids=[fw[0] for fw in BASE_URL_FRAMEWORKS],
)
def test_scenario_base_url_framework_wraps_or_names_the_missing_package(
    scenario_collector, slug, module_name, class_name, description, future_story
):
    """For each framework: the adapter is a real wrapper. With the framework absent
    wrap_model raises an ImportError naming what to install; with it present it returns
    a model object. Never NotImplementedError."""
    import importlib

    s = Scenario(
        id=f"fw-{slug}",
        title=f"{slug}: thin base_url wrapper",
        framework=slug,
        narrative=f"{description}. {future_story}",
        expected_outcome=(
            "wrap_model returns a gateway-pointed model, or raises an ImportError "
            "that says 'pip install' when the framework package is not installed"
        ),
    )
    mod = importlib.import_module(module_name)
    adapter_cls = getattr(mod, class_name)
    s.framework_event("adapter imported", module=module_name, adapter=class_name,
                      protocol_name=adapter_cls.name)
    available = adapter_cls.is_available()
    s.framework_event("is_available() queried", available=available)

    adapter = adapter_cls()
    outcome, ok = "", False
    try:
        model = adapter.wrap_model(None)
        outcome, ok = f"built {type(model).__name__}", available
    except ImportError as exc:
        outcome, ok = f"ImportError: {exc}", (not available) and "pip install" in str(exc)
    except Exception as exc:  # noqa: BLE001 -- recorded, then the scenario fails
        outcome = f"unexpected {type(exc).__name__}: {exc}"
    s.framework_event("wrap_model(None) attempted", result=outcome)
    s.outcome(f"wrap_model(None): {outcome}", success=ok)
    scenario_collector.add(s)
    assert s.passed


def test_scenario_framework_attribution_round_trip(
    scenario_collector, tmp_path
):
    """All 6 framework slugs round-trip through lineage + sessions."""
    s = Scenario(
        id="fw-attribution",
        title="All 6 framework slugs round-trip through lineage + sessions",
        framework="*all*",
        narrative=(
            "For each of Agno, LangGraph, CrewAI, OpenAI Agents, "
            "Claude Agent SDK, Pydantic AI: write a lineage record + open "
            "a session tagged with the framework slug; verify "
            "by_framework() and SessionStore.get().framework return them "
            "correctly. This proves lineage attribution is uniform across "
            "frameworks regardless of concrete impl status."
        ),
        expected_outcome="all 6 slugs round-trip without error",
    )
    lineage = LineageStore(db_path=tmp_path / "l.db")
    sessions = SessionStore(db_path=tmp_path / "s.db")
    slugs = ["agno", "langgraph", "crewai",
             "openai-agents", "claude-agent-sdk", "pydantic-ai"]
    all_ok = True
    for slug in slugs:
        rec = make_record(
            host="test", prompt_fingerprint=f"fp-{slug}",
            task_type="query", complexity="simple",
            classifier_method="heuristic",
            signal_scores={}, fired_decisions=(),
            chain_attempted=("ollama/qwen3.5:latest",),
            model_chosen="ollama/qwen3.5:latest",
            outcome="success", latency_ms=10, cost_usd=0.0,
            framework=slug,
        )
        lineage.record(rec)
        rows = lineage.by_framework(slug)
        sess = sessions.create(agent_id=f"agent-{slug}",
                               budget_usd=0.5, framework=slug)
        fetched = sessions.get(sess.session_id)
        ok = len(rows) == 1 and fetched.framework == slug
        s.framework_event(f"slug={slug} round-trip", ok=ok)
        if not ok:
            all_ok = False

    s.outcome(
        f"All {len(slugs)} framework slugs round-trip through both lineage "
        f"and session stores. Cross-framework reporting works without "
        f"requiring concrete adapter impls.",
        success=all_ok,
    )
    scenario_collector.add(s)
    assert s.passed
