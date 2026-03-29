"""Provider budget tracking — know how much you have left on each external LLM.

Tracks per-provider monthly spend from the usage DB, and compares against
user-configured monthly limits per provider. Used to pick the best external
LLM fallback when Claude quota is tight.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from llm_router.config import get_config


# Known cost tiers for external models (approximate $/1K tokens blended).
# Used to estimate remaining capacity from budget headroom.
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


@dataclass
class ProviderBudget:
    """Budget status for a single provider."""
    provider: str
    monthly_limit: float     # USD, 0 = unlimited
    spent_this_month: float  # USD from usage DB
    is_available: bool       # has API key

    @property
    def remaining(self) -> float:
        if self.monthly_limit <= 0:
            return float("inf")
        return max(0.0, self.monthly_limit - self.spent_this_month)

    @property
    def pct_used(self) -> float:
        if self.monthly_limit <= 0:
            return 0.0
        return min(1.0, self.spent_this_month / self.monthly_limit)

    @property
    def has_budget(self) -> bool:
        return self.is_available and (self.monthly_limit <= 0 or self.remaining > 0)


async def get_provider_spend() -> dict[str, float]:
    """Get this month's spend per provider from the usage DB."""
    config = get_config()
    db_path = config.llm_router_db_path
    if not db_path.exists():
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
    """Get budget status for all configured providers."""
    config = get_config()
    spend = await get_provider_spend()

    # Provider monthly limits — configurable via env vars.
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
        # Skip Claude — it's tracked separately via subscription
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
    """Rank available external models for complex task fallback.

    Returns list of (model, quality_score, cost_per_1k) sorted by
    quality descending, filtered by provider budget availability.
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

    # Add Codex local agent as a candidate (free — uses OpenAI subscription)
    from llm_router.codex_agent import is_codex_available
    if is_codex_available():
        candidates.append(("codex/gpt-5.4", 0.97, 0.0))   # free, very capable
        candidates.append(("codex/o3", 0.95, 0.0))          # free, deep reasoning
        candidates.append(("codex/gpt-4o", 0.88, 0.0))      # free, balanced

    # Sort by quality descending, then cost ascending
    candidates.sort(key=lambda x: (-x[1], x[2]))
    return candidates
