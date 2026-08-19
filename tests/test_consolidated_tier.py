"""North Star 1.0 cutover (staged) — the CONSOLIDATED slim tier.

LLM_ROUTER_SLIM=consolidated registers ONLY the ~11 front-door tools (old tools hidden,
not removed). This lets us run + validate the collapsed surface before the breaking
step that actually deletes the 73 old tools.
"""
from __future__ import annotations

from llm_router.tool_tiers import CONSOLIDATED_TOOLS, make_should_register, tier_summary


def test_consolidated_tier_admits_the_front_doors():
    gate = make_should_register("consolidated")
    for door in ("llm", "llm_act", "llm_router_status", "llm_router_admin", "llm_router_session"):
        assert gate(door), f"consolidated tier must register the {door} door"


def test_consolidated_tier_hides_the_old_tools():
    gate = make_should_register("consolidated")
    for old in ("llm_query", "llm_analyze", "llm_code", "llm_savings", "llm_usage",
                "llm_set_profile", "llm_router_agent_list"):
        assert not gate(old), f"consolidated tier must hide the old tool {old}"


def test_off_tier_still_registers_everything():
    gate = make_should_register("off")
    assert gate("llm_query") and gate("llm") and gate("anything")


def test_consolidated_tier_is_about_eleven_tools():
    assert 9 <= len(CONSOLIDATED_TOOLS) <= 13


def test_tier_summary_mentions_consolidated():
    assert "consolidated" in tier_summary("consolidated").lower()
