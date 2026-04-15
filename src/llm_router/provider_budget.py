"""Provider budget tracking -- know how much you have left on each external LLM.

Tracks per-provider monthly spend from the usage DB, and compares against
user-configured monthly limits per provider. Used to pick the best external
LLM fallback when Claude quota is tight.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from llm_router.config import get_config


# Known cost tiers for external models (approximate $/1K tokens blended).
# "Blended" means an average of input and output token costs, weighted toward
# typical usage patterns. Used to estimate remaining capacity from budget headroom.
EXTERNAL_MODEL_COST: dict[str, float] = {
    # OpenAI
    "openai/gpt-4o": 0.0075,
    "openai/gpt-4o-mini": 0.0003,
    "openai/o3": 0.03,
    "openai/o4-mini": 0.003,
    # Gemini
    "gemini/gemini-2.5-pro": 0.003,
    "gemini/gemini-2.5-flash": 0.0003,
    "gemini/gemini-2.5-flash-lite": 0.0001,
    # Perplexity
    "perplexity/sonar": 0.001,
    "perplexity/sonar-pro": 0.003,
    # DeepSeek
    "deepseek/deepseek-chat": 0.0007,
    "deepseek/deepseek-reasoner": 0.002,
    # Mistral
    "mistral/mistral-large-latest": 0.006,
    "mistral/mistral-small-latest": 0.0006,
    # Groq (free tier / very cheap)
    "groq/llama-3.3-70b-versatile": 0.0003,
}
"""Approximate blended cost per 1K tokens for each external model. Used by
``rank_external_models`` to break ties between models of equal quality and
by budget calculations to estimate how many tokens remaining budget can buy."""

# Quality score for complex task routing (0-1, higher = better for complex tasks)
EXTERNAL_MODEL_QUALITY: dict[str, float] = {
    "openai/o3": 0.98,
    "gemini/gemini-2.5-pro": 0.95,
    "openai/gpt-4o": 0.90,
    "deepseek/deepseek-reasoner": 0.88,
    "mistral/mistral-large-latest": 0.82,
    "openai/gpt-4o-mini": 0.72,
    "gemini/gemini-2.5-flash": 0.70,
    "deepseek/deepseek-chat": 0.70,
    "groq/llama-3.3-70b-versatile": 0.68,
    "perplexity/sonar-pro": 0.80,
    "perplexity/sonar": 0.65,
}
"""Subjective quality scores (0.0-1.0) reflecting each model's capability on
complex coding and analysis tasks. Used by ``rank_external_models`` as the
primary sort key -- higher-quality models are preferred when budget allows."""


@dataclass
class ProviderBudget:
    """Budget status for a single external LLM provider.

    Combines the user-configured monthly spending limit with actual spend
    from the usage database to provide remaining-budget calculations.

    Attributes:
        provider: Provider name (e.g. "openai", "gemini").
        monthly_limit: Maximum USD to spend per month. 0 means unlimited
            (no budget cap configured).
        spent_this_month: USD spent so far this calendar month, from the
            usage database.
        is_available: Whether an API key is configured for this provider.
    """

    provider: str
    monthly_limit: float
    spent_this_month: float
    is_available: bool

    @property
    def remaining(self) -> float:
        """USD remaining in this month's budget.

        Returns:
            ``float('inf')`` if no limit is set (monthly_limit <= 0),
            otherwise the difference clamped to >= 0.
        """
        if self.monthly_limit <= 0:
            return float("inf")
        return max(0.0, self.monthly_limit - self.spent_this_month)

    @property
    def pct_used(self) -> float:
        """Fraction of monthly budget consumed, 0.0-1.0.

        Returns:
            0.0 if no limit is set, otherwise spent/limit clamped to <= 1.0.
        """
        if self.monthly_limit <= 0:
            return 0.0
        return min(1.0, self.spent_this_month / self.monthly_limit)

    @property
    def has_budget(self) -> bool:
        """Whether this provider can accept new calls.

        True if the provider has an API key AND either has no budget limit
        or has remaining budget.
        """
        return self.is_available and (self.monthly_limit <= 0 or self.remaining > 0)


async def get_provider_spend() -> dict[str, float]:
    """Query this month's cumulative spend per provider from the usage database.

    Reads from the ``usage`` table (external LLM calls only). Returns an empty
    dict if the database file doesn't exist yet (first run).

    Returns:
        Dict mapping provider name to USD spent this calendar month.
    """
    import asyncio

    config = get_config()
    db_path = config.llm_router_db_path
    # Offload synchronous Path.exists() to thread pool to avoid blocking event loop
    exists = await asyncio.to_thread(db_path.exists)
    if not exists:
        return {}

    db = await aiosqlite.connect(str(db_path))
    try:
        cursor = await db.execute(
            "SELECT provider, COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE timestamp >= datetime('now', 'start of month') "
            "GROUP BY provider"
        )
        rows = await cursor.fetchall()
        return {provider: float(spent) for provider, spent in rows}
    finally:
        await db.close()


async def get_provider_budgets() -> dict[str, ProviderBudget]:
    """Build budget status for all configured external providers.

    Monthly budget limits are read from environment variables following the
    naming convention ``LLM_ROUTER_BUDGET_<PROVIDER>=<USD>``, e.g.
    ``LLM_ROUTER_BUDGET_OPENAI=10.00``. A value of 0 (or missing env var)
    means unlimited.

    Anthropic (Claude) is excluded because its budget is tracked separately
    via the subscription usage system (``claude_usage.py``).

    Returns:
        Dict mapping provider name to its ``ProviderBudget``.
    """
    config = get_config()
    spend = await get_provider_spend()

    # Provider monthly limits -- configurable via env vars.
    # Format: LLM_ROUTER_BUDGET_OPENAI=10.00 (USD/month)
    import os
    limits: dict[str, float] = {}
    for provider in config.available_providers:
        env_key = f"LLM_ROUTER_BUDGET_{provider.upper()}"
        limit_str = os.environ.get(env_key, "0")
        try:
            limits[provider] = float(limit_str)
        except ValueError:
            limits[provider] = 0.0

    budgets = {}
    for provider in config.available_providers:
        # Skip Claude -- it's tracked separately via subscription
        if provider == "anthropic":
            continue
        budgets[provider] = ProviderBudget(
            provider=provider,
            monthly_limit=limits.get(provider, 0.0),
            spent_this_month=spend.get(provider, 0.0),
            is_available=True,
        )

    return budgets


def rank_external_models(
    budgets: dict[str, ProviderBudget],
    task_type: str = "code",
    min_quality: float = 0.80,
) -> list[tuple[str, float, float]]:
    """Rank available external models for complex task fallback routing.

    Filters models by minimum quality score and provider budget availability,
    then sorts by quality descending (best model first) with cost ascending
    as a tiebreaker (cheaper preferred among equals).

    Also injects Codex local agent models as free candidates if the Codex CLI
    is installed, since they use the user's OpenAI subscription at no
    additional API cost.

    Args:
        budgets: Provider budget status from ``get_provider_budgets()``.
        task_type: The task type being routed (currently unused but reserved
            for future task-specific quality adjustments).
        min_quality: Minimum quality score threshold (0.0-1.0). Models below
            this are excluded from candidates.

    Returns:
        List of ``(model_string, quality_score, cost_per_1k)`` tuples sorted
        by quality descending, then cost ascending.
    """
    candidates = []

    for model, quality in EXTERNAL_MODEL_QUALITY.items():
        if quality < min_quality:
            continue

        provider = model.split("/")[0]
        budget = budgets.get(provider)
        if not budget or not budget.has_budget:
            continue

        cost = EXTERNAL_MODEL_COST.get(model, 0.01)
        candidates.append((model, quality, cost))

    # Add Codex local agent as a candidate (free -- uses OpenAI subscription)
    from llm_router.codex_agent import is_codex_available
    if is_codex_available():
        candidates.append(("codex/gpt-5.4", 0.97, 0.0))   # free, very capable
        candidates.append(("codex/o3", 0.95, 0.0))          # free, deep reasoning
        candidates.append(("codex/gpt-4o", 0.88, 0.0))      # free, balanced

    # Sort by quality descending, then cost ascending
    candidates.sort(key=lambda x: (-x[1], x[2]))
    return candidates
