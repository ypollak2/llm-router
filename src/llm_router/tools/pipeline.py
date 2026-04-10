"""Orchestration tools — llm_orchestrate, llm_pipeline_templates."""

from __future__ import annotations

from llm_router.config import get_config
from llm_router.orchestrator import PIPELINE_TEMPLATES, auto_orchestrate, run_pipeline
from llm_router.types import Tier
from llm_router import state as _state


async def llm_orchestrate(
    task: str,
    template: str | None = None,
) -> str:
    """Multi-step orchestration — automatically decomposes complex tasks across multiple LLMs.

    Chains research, analysis, generation, and coding steps together, routing each
    to the optimal model. Use templates for common patterns or let the AI decompose.

    Free tier: up to 2-step pipelines. Pro tier: unlimited steps + auto-decomposition.

    Args:
        task: Description of the complex task to accomplish.
        template: Optional pipeline template: "research_report", "competitive_analysis", "content_pipeline", "code_review_fix". Omit for auto-decomposition.
    """
    config = get_config()

    # Auto-decomposition requires Pro
    if not template:
        tier_error = _state._check_tier("multi_step")
        if tier_error:
            return tier_error

    # Free tier: templates limited to 2-step max
    if template and template in PIPELINE_TEMPLATES:
        steps = PIPELINE_TEMPLATES[template]
        if config.llm_router_tier == Tier.FREE and len(steps) > 2:
            return (
                f"Template '{template}' has {len(steps)} steps — free tier allows up to 2. "
                "Upgrade to Pro for unlimited pipeline steps: https://llm-router.dev/pricing"
            )

    if template and template in PIPELINE_TEMPLATES:
        steps = PIPELINE_TEMPLATES[template]
        result = await run_pipeline(steps, task)
    else:
        result = await auto_orchestrate(task)

    output = result.final_content
    output += f"\n\n---\n{result.summary()}"
    return output


async def llm_pipeline_templates() -> str:
    """List available pipeline templates for multi-step orchestration."""
    lines = ["## Available Pipeline Templates\n"]
    descriptions = {
        "research_report": "Research → Analyze → Write Report (3 steps)",
        "competitive_analysis": "Research competitors → Find reviews → SWOT analysis → Report (4 steps)",
        "content_pipeline": "Research → Write → Review → Polish (4 steps)",
        "code_review_fix": "Review code → Fix issues → Write tests (3 steps)",
    }
    for name, desc in descriptions.items():
        step_types = [s.task_type.value for s in PIPELINE_TEMPLATES[name]]
        lines.append(f"- **{name}**: {desc}")
        lines.append(f"  Steps: {' → '.join(step_types)}")
    lines.append("")
    lines.append('Use: `llm_orchestrate(task="...", template="research_report")`')
    return "\n".join(lines)


def register(mcp, should_register=None) -> None:
    """Register orchestration tools with the FastMCP instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_orchestrate"):
        mcp.tool()(llm_orchestrate)
    if gate("llm_pipeline_templates"):
        mcp.tool()(llm_pipeline_templates)
