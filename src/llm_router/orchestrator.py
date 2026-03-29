"""Multi-step orchestration — chain tasks across multiple LLMs.

Decomposes complex tasks into a pipeline of steps, routes each step
to the optimal model, and synthesizes results.
"""

from __future__ import annotations

import json
import logging

from llm_router.config import get_config
from llm_router.router import route_and_call
from llm_router.types import (
    PipelineResult,
    PipelineStep,
    RoutingProfile,
    TaskType,
)

log = logging.getLogger("llm_router.orchestrator")

# Pre-built pipeline templates for common multi-step tasks.
#
# Each template is an ordered list of ``PipelineStep`` objects that the
# ``run_pipeline`` function executes sequentially, passing results between
# steps via template variable substitution.
#
# Templates:
#   research_report      — 3 steps: Research → Analyze → Write Report
#   competitive_analysis — 4 steps: Find competitors → Find reviews → SWOT → Report
#   content_pipeline     — 4 steps: Research → Write → Review → Polish
#   code_review_fix      — 3 steps: Review code → Fix issues → Write tests
PIPELINE_TEMPLATES: dict[str, list[PipelineStep]] = {
    "research_report": [
        PipelineStep(
            task_type=TaskType.RESEARCH,
            prompt_template="Research the following topic thoroughly. Include facts, data, and sources:\n\n{input}",
        ),
        PipelineStep(
            task_type=TaskType.ANALYZE,
            prompt_template="Analyze the following research findings. Identify key patterns, gaps, and insights:\n\n{previous_result}",
        ),
        PipelineStep(
            task_type=TaskType.GENERATE,
            prompt_template="Write a professional report based on this analysis. Include an executive summary, key findings, and recommendations:\n\n{previous_result}",
            system_prompt="You are a professional report writer. Structure your output with clear headings, bullet points, and a logical flow.",
        ),
    ],
    "competitive_analysis": [
        PipelineStep(
            task_type=TaskType.RESEARCH,
            prompt_template="Find detailed information about competitors in this space. Focus on features, pricing, market position, and recent news:\n\n{input}",
        ),
        PipelineStep(
            task_type=TaskType.RESEARCH,
            prompt_template="Find user reviews, sentiment, and market reception for the competitors identified here:\n\n{previous_result}",
        ),
        PipelineStep(
            task_type=TaskType.ANALYZE,
            prompt_template="Create a SWOT analysis and competitive landscape map based on this research:\n\n{previous_result}",
        ),
        PipelineStep(
            task_type=TaskType.GENERATE,
            prompt_template="Write a competitive analysis report with recommendations based on this analysis:\n\n{previous_result}",
        ),
    ],
    "content_pipeline": [
        PipelineStep(
            task_type=TaskType.RESEARCH,
            prompt_template="Research this topic for content creation. Find key facts, interesting angles, and trending aspects:\n\n{input}",
        ),
        PipelineStep(
            task_type=TaskType.GENERATE,
            prompt_template="Write engaging, well-structured content based on this research:\n\n{previous_result}",
            system_prompt="Write in a clear, engaging style. Use examples and analogies. Target a technical but accessible audience.",
        ),
        PipelineStep(
            task_type=TaskType.ANALYZE,
            prompt_template="Review this content for accuracy, clarity, and engagement. Suggest specific improvements:\n\n{previous_result}",
        ),
        PipelineStep(
            task_type=TaskType.GENERATE,
            prompt_template="Incorporate the review feedback and produce the final polished version:\n\nOriginal content with review:\n{previous_result}",
        ),
    ],
    "code_review_fix": [
        PipelineStep(
            task_type=TaskType.ANALYZE,
            prompt_template="Review this code for bugs, security issues, performance problems, and style issues. Be specific about line numbers and severity:\n\n{input}",
        ),
        PipelineStep(
            task_type=TaskType.CODE,
            prompt_template="Fix all issues identified in this code review. Show the complete corrected code:\n\n{previous_result}",
        ),
        PipelineStep(
            task_type=TaskType.CODE,
            prompt_template="Write comprehensive tests for this corrected code. Cover edge cases and the bugs that were fixed:\n\n{previous_result}",
        ),
    ],
}


async def run_pipeline(
    steps: list[PipelineStep],
    initial_input: str,
    *,
    profile: RoutingProfile | None = None,
) -> PipelineResult:
    """Execute a multi-step pipeline, routing each step to the optimal model.

    Steps run sequentially.  Each step's ``prompt_template`` supports three
    variable placeholders that are resolved before the LLM call:

        - ``{input}``: the original user input (``initial_input``), available
          in every step.
        - ``{previous_result}``: the content output from the immediately
          preceding step (or ``initial_input`` for the first step).
        - ``{step_N}``: the content output from step *N* (0-indexed), allowing
          any step to reference any earlier step's output.

    Args:
        steps: Ordered list of pipeline steps to execute.
        initial_input: The original user task/prompt.
        profile: Routing profile override.  Falls back to the global config
            profile if ``None``.

    Returns:
        A ``PipelineResult`` with cumulative cost/latency, per-step responses,
        and ``final_content`` set to the last step's output.
    """
    config = get_config()
    profile = profile or config.llm_router_profile
    result = PipelineResult()

    step_outputs: list[str] = []
    previous = initial_input

    for i, step in enumerate(steps):
        log.info("Pipeline step %d/%d: %s", i + 1, len(steps), step.task_type.value)

        # Resolve template variables
        prompt = step.prompt_template.replace("{input}", initial_input)
        prompt = prompt.replace("{previous_result}", previous)
        for j, output in enumerate(step_outputs):
            prompt = prompt.replace(f"{{step_{j}}}", output)

        response = await route_and_call(
            step.task_type,
            prompt,
            profile=profile,
            system_prompt=step.system_prompt,
            model_override=step.model_override,
        )

        result.steps.append(response)
        result.total_cost_usd += response.cost_usd
        result.total_latency_ms += response.latency_ms
        step_outputs.append(response.content)
        previous = response.content

    result.final_content = previous
    return result


async def auto_orchestrate(
    task_description: str,
    *,
    profile: RoutingProfile | None = None,
) -> PipelineResult:
    """Automatically decompose a complex task into pipeline steps and execute.

    Operates in two phases:

    1. **Decomposition** — Sends the task to a cheap/fast model (forced to
       ``RoutingProfile.BUDGET``) with instructions to break it into 2-5
       sequential steps as a JSON array.  BUDGET is used here because the
       decomposition prompt is a structured classification task that doesn't
       benefit from premium models.

    2. **Execution** — Parses the JSON steps into ``PipelineStep`` objects
       and feeds them to ``run_pipeline``, which routes each step to the
       optimal model per the user's profile.

    If the decomposition response can't be parsed (e.g. the LLM returns
    malformed JSON or wraps it in markdown code fences), falls back to a
    single GENERATE step with the original input.

    The decomposition step's cost and latency are added to the final result
    for accurate total accounting.

    Args:
        task_description: Natural-language description of the complex task.
        profile: Routing profile override for the execution phase.  Falls
            back to the global config profile if ``None``.

    Returns:
        A ``PipelineResult`` whose ``steps`` list includes the decomposition
        response at index 0, followed by the execution step responses.
    """
    config = get_config()
    profile = profile or config.llm_router_profile

    # Step 1: Use a fast model to decompose the task
    decompose_prompt = f"""Analyze this task and break it into 2-5 sequential steps.
For each step, specify:
- task_type: one of "research", "analyze", "generate", "code", "query"
- prompt: what this step should do (reference {{input}} for the original task and {{previous_result}} for the prior step's output)
- system_prompt: optional system instructions for this step (null if not needed)

Return ONLY a JSON array of steps. Example:
[
  {{"task_type": "research", "prompt": "Find information about {{input}}", "system_prompt": null}},
  {{"task_type": "analyze", "prompt": "Analyze: {{previous_result}}", "system_prompt": null}},
  {{"task_type": "generate", "prompt": "Write a report: {{previous_result}}", "system_prompt": "Be concise."}}
]

Task: {task_description}"""

    decompose_response = await route_and_call(
        TaskType.ANALYZE,
        decompose_prompt,
        profile=RoutingProfile.BUDGET,  # Use cheap model for decomposition
        system_prompt="You are a task decomposition expert. Return ONLY valid JSON.",
        temperature=0.2,
    )

    # Parse the pipeline steps.
    # LLMs frequently wrap JSON in markdown code fences (```json ... ```),
    # so we strip those before parsing.
    try:
        content = decompose_response.content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        raw_steps = json.loads(content)
        steps = [
            PipelineStep(
                task_type=TaskType(s["task_type"]),
                prompt_template=s["prompt"],
                system_prompt=s.get("system_prompt"),
            )
            for s in raw_steps
        ]
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning("Failed to parse pipeline: %s. Falling back to single step.", e)
        steps = [
            PipelineStep(task_type=TaskType.GENERATE, prompt_template="{input}"),
        ]

    # Step 2: Execute the pipeline
    result = await run_pipeline(steps, task_description, profile=profile)

    # Include the decomposition cost
    result.steps.insert(0, decompose_response)
    result.total_cost_usd += decompose_response.cost_usd
    result.total_latency_ms += decompose_response.latency_ms

    return result
