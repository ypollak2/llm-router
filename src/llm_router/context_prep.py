"""Context preparation pipeline — prepares optimized prompts for routed models.

This module transforms raw user prompts into budget-aware, context-enriched
prompts before they're sent to external models. It's the bridge between
classification ("what model should handle this?") and dispatch ("send it").

Pipeline:
  1. Calculate token budget for target model
  2. Retrieve relevant prior context (Sprint 2 — result cache)
  3. Retrieve code context (Sprint 3 — AST/tree-sitter)
  4. Build system prompt (task-specific behavioral rules)
  5. Assemble final prompt within budget constraints

Without this module, cheap models receive raw prompts with no context and
produce generic answers. With it, they receive focused prompts with precisely
the context they need — dramatically improving response quality.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm_router.system_prompts import get_system_prompt
from llm_router.token_budget import (
    TokenBudget,
    calculate_budget,
    estimate_tokens,
    truncate_to_budget,
)
from llm_router.types import Complexity, TaskType


@dataclass(frozen=True)
class PreparedPrompt:
    """A fully prepared prompt ready for dispatch to an external model.

    Attributes:
        system: System prompt with behavioral rules for the model.
        context: Retrieved context (prior Q&A, code symbols, etc.).
        user_prompt: The original user prompt (possibly truncated to fit budget).
        budget: The token budget that governed this preparation.
        context_source: Description of where context came from (for debugging).
    """

    system: str
    context: str
    user_prompt: str
    budget: TokenBudget
    context_source: str

    @property
    def full_system(self) -> str:
        """Combined system prompt + context, ready to pass as system_prompt."""
        if not self.context:
            return self.system
        return f"{self.system}\n\n---\n\n{self.context}"

    @property
    def estimated_total_tokens(self) -> int:
        """Estimated total input tokens for this prepared prompt."""
        return (
            estimate_tokens(self.system)
            + estimate_tokens(self.context)
            + estimate_tokens(self.user_prompt)
        )


def prepare_prompt(
    user_prompt: str,
    task_type: TaskType,
    complexity: Complexity,
    target_model: str,
    *,
    existing_system_prompt: str | None = None,
    project_dir: str | None = None,
) -> PreparedPrompt:
    """Prepare an optimized prompt for routing to an external model.

    This is the main entry point for the context preparation pipeline.
    It calculates budget, retrieves context, and assembles the final prompt.

    Args:
        user_prompt: The raw user prompt text.
        task_type: Classified task type (query, code, analyze, etc.).
        complexity: Classified complexity level.
        target_model: The model identifier this prompt will be sent to.
        existing_system_prompt: If the caller already specified a system prompt,
            it takes priority over the auto-generated one.
        project_dir: Current project directory (for project-scoped cache lookups).

    Returns:
        PreparedPrompt with all components assembled within budget.
    """
    user_tokens = estimate_tokens(user_prompt)
    budget = calculate_budget(target_model, task_type, complexity, user_tokens)

    # ── System prompt ─────────────────────────────────────────────────────────
    if existing_system_prompt:
        system = truncate_to_budget(existing_system_prompt, budget.system_tokens)
    else:
        system = get_system_prompt(task_type, complexity)

    # ── Context retrieval ─────────────────────────────────────────────────────
    context = ""
    context_source = "none"

    # BM25 retrieval from result cache (Sprint 2)
    try:
        from llm_router.result_cache import format_context, search_results

        cached = search_results(
            query=user_prompt,
            task_type=task_type.value,
            project_dir=project_dir,
            budget_tokens=budget.context_tokens,
        )
        if cached:
            context = format_context(cached, max_tokens=budget.context_tokens)
            context_source = f"cache({len(cached)} results)"
    except Exception:
        pass  # Cache unavailable — continue without context

    # AST code context for code tasks (Sprint 3)
    if task_type in (TaskType.CODE, TaskType.ANALYZE) and project_dir:
        try:
            from llm_router.code_context import extract_code_context

            # Allocate 70% of context budget to code, 30% to cache (if both available)
            code_budget = budget.context_tokens * 7 // 10 if context else budget.context_tokens
            code_ctx = extract_code_context(user_prompt, project_dir, code_budget)
            if code_ctx:
                if context:
                    context = code_ctx + "\n\n" + context
                    context_source = f"ast+{context_source}"
                else:
                    context = code_ctx
                    context_source = "ast"
        except Exception:
            pass  # Tree-sitter unavailable or parse failed

    # ── User prompt (truncate if over budget) ─────────────────────────────────
    final_user_prompt = truncate_to_budget(user_prompt, budget.user_tokens)

    return PreparedPrompt(
        system=system,
        context=context,
        user_prompt=final_user_prompt,
        budget=budget,
        context_source=context_source,
    )
