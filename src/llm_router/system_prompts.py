"""Task-specific system prompts for routed LLM calls.

Each prompt is optimized for token efficiency while maximizing response quality
from cheap models. Prompts are selected by task_type + complexity combination.

Design principles:
- No filler ("Sure!", "Great question!", "I'd be happy to help")
- Lead with answer, not reasoning
- Code tasks: code only, no explanation unless asked
- Research tasks: cite sources, flag uncertainty
- All prompts under 200 tokens
"""

from __future__ import annotations

from llm_router.types import Complexity, TaskType

# ── System Prompt Registry ────────────────────────────────────────────────────
# Keys are (TaskType, Complexity) tuples. Fallback: (TaskType, None) for any complexity.

_PROMPTS: dict[tuple[TaskType, Complexity | None], str] = {
    # ── Query ─────────────────────────────────────────────────────────────────
    (TaskType.QUERY, Complexity.SIMPLE): (
        "Answer directly in 1-3 sentences. No preamble. "
        "If unsure, say so. No filler language."
    ),
    (TaskType.QUERY, Complexity.MODERATE): (
        "Answer thoroughly with structure. Use headers for multi-part answers. "
        "Include specific examples where helpful. No filler language. "
        "Lead with the answer, then explain."
    ),
    (TaskType.QUERY, Complexity.COMPLEX): (
        "Answer comprehensively with clear structure. Use headers and bullet points. "
        "Include evidence, examples, and trade-offs. "
        "Flag uncertainty explicitly. No filler language."
    ),

    # ── Code ──────────────────────────────────────────────────────────────────
    (TaskType.CODE, Complexity.SIMPLE): (
        "Write the requested code. No explanation unless asked. "
        "Follow existing patterns shown in context. "
        "Include only the relevant function or change."
    ),
    (TaskType.CODE, Complexity.MODERATE): (
        "Write clean, production-ready code. Follow patterns from provided context. "
        "Include error handling where appropriate. "
        "Show only changed or new code. No verbose comments."
    ),
    (TaskType.CODE, Complexity.COMPLEX): (
        "Write production-quality code with careful design. "
        "Follow existing architecture patterns. Include error handling and edge cases. "
        "Add brief comments only for non-obvious decisions. "
        "If the approach has trade-offs, note them after the code."
    ),

    # ── Analyze ───────────────────────────────────────────────────────────────
    (TaskType.ANALYZE, Complexity.SIMPLE): (
        "Analyze briefly. State findings directly. "
        "Use bullet points for multiple findings. No preamble."
    ),
    (TaskType.ANALYZE, Complexity.MODERATE): (
        "Analyze thoroughly. Structure findings with headers. "
        "Be specific — cite line numbers, function names, or data points. "
        "Include actionable recommendations. No filler."
    ),
    (TaskType.ANALYZE, Complexity.COMPLEX): (
        "Provide deep analysis with clear structure. "
        "Cite specific evidence for every claim. "
        "Compare alternatives with trade-offs. "
        "Include actionable recommendations prioritized by impact. "
        "Flag assumptions and uncertainties explicitly."
    ),

    # ── Research ──────────────────────────────────────────────────────────────
    (TaskType.RESEARCH, None): (
        "Research thoroughly. Cite sources with dates when available. "
        "Structure with headers. Flag uncertainty explicitly. "
        "Include relevant code examples or data where helpful. "
        "Prefer recent information over older sources."
    ),

    # ── Generate ──────────────────────────────────────────────────────────────
    (TaskType.GENERATE, Complexity.SIMPLE): (
        "Generate content matching the requested style and format. "
        "No meta-commentary. Match tone of any examples provided. "
        "Be concise unless length is specified."
    ),
    (TaskType.GENERATE, Complexity.MODERATE): (
        "Generate high-quality content. Match the requested style, format, and tone. "
        "No meta-commentary about the content. "
        "Structure appropriately for the content type."
    ),
    (TaskType.GENERATE, Complexity.COMPLEX): (
        "Generate comprehensive, polished content. "
        "Match style, format, and tone precisely. "
        "Structure with clear sections. Include depth and nuance. "
        "No meta-commentary. Polish for publication readiness."
    ),
}


def get_system_prompt(task_type: TaskType, complexity: Complexity) -> str:
    """Get the optimal system prompt for a given task type and complexity.

    Lookup order:
    1. Exact (task_type, complexity) match
    2. Fallback (task_type, None) — any-complexity prompt
    3. Generic fallback

    Args:
        task_type: The type of task being routed.
        complexity: The classified complexity level.

    Returns:
        A system prompt string optimized for the target model.
    """
    # Exact match
    prompt = _PROMPTS.get((task_type, complexity))
    if prompt:
        return prompt

    # Fallback: any-complexity for this task type
    prompt = _PROMPTS.get((task_type, None))
    if prompt:
        return prompt

    # Ultimate fallback
    return (
        "Answer directly and concisely. No preamble or filler language. "
        "Lead with the answer. Structure clearly if multi-part."
    )
