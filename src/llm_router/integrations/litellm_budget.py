"""LiteLLM BudgetManager integration for llm-router.

When teams run a LiteLLM Proxy with budget management enabled, llm-router
can read per-user/per-team budget state from the proxy's SQLite database
and incorporate it into routing pressure calculations.

This allows enterprises to:
  - Set per-developer or per-team monthly spend limits in LiteLLM
  - Have llm-router automatically route away from expensive providers
    as those budgets approach their limits
  - Unify budget enforcement across both LiteLLM Proxy and direct
    llm-router routing

Configuration::

    # Point at LiteLLM Proxy's SQLite DB (usually litellm.db)
    export LLM_ROUTER_LITELLM_BUDGET_DB=/path/to/litellm.db

    # Optional: which user/team key to track (defaults to aggregate)
    export LLM_ROUTER_LITELLM_USER=my-team

Usage (called by budget.py when configured)::

    from llm_router.integrations.litellm_budget import get_litellm_spend
    spend = await get_litellm_spend(provider="openai")
    # Returns USD spend from LiteLLM DB for this provider this month
"""

from __future__ import annotations

import os
from pathlib import Path


_DB_ENV = "LLM_ROUTER_LITELLM_BUDGET_DB"
_USER_ENV = "LLM_ROUTER_LITELLM_USER"


def is_litellm_budget_enabled() -> bool:
    """Return True when a LiteLLM budget DB path is configured."""
    db = os.environ.get(_DB_ENV, "")
    return bool(db) and Path(db).exists()


async def get_litellm_spend(provider: str | None = None) -> dict[str, float]:
    """Query the LiteLLM budget DB for monthly spend per provider.

    Returns a dict mapping provider → USD spend this calendar month.
    Returns an empty dict if not configured or the query fails.

    Args:
        provider: If given, return only this provider's spend.
                  If None, return all providers.

    Returns:
        ``{"openai": 3.50, "anthropic": 12.00}``
    """
    import asyncio

    db_path = os.environ.get(_DB_ENV, "")
    if not db_path:
        return {}
    # Offload synchronous Path.exists() to thread pool to avoid blocking event loop
    exists = await asyncio.to_thread(Path(db_path).exists)
    if not exists:
        return {}

    user_filter = os.environ.get(_USER_ENV, "")

    try:
        import aiosqlite

        # LiteLLM stores spend in the `spend_logs` table.
        # Schema: user, model, provider, spend, startTime, endTime, ...
        # We aggregate by provider for the current calendar month.
        query = """
            SELECT
                LOWER(model) as provider_key,
                COALESCE(SUM(spend), 0) as total_spend
            FROM spend_logs
            WHERE startTime >= datetime('now', 'start of month')
        """
        params: list = []

        if user_filter:
            query += " AND user = ?"
            params.append(user_filter)

        if provider:
            query += " AND LOWER(model) LIKE ?"
            params.append(f"{provider.lower()}%")

        query += " GROUP BY provider_key"

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

        result: dict[str, float] = {}
        for row in rows:
            model_key: str = row[0] or ""
            spend = float(row[1] or 0.0)
            # Extract provider from model key (e.g. "openai/gpt-4o" → "openai")
            prov = model_key.split("/")[0] if "/" in model_key else model_key
            if prov:
                result[prov] = result.get(prov, 0.0) + spend

        return result

    except Exception:
        return {}


async def get_litellm_budget_cap(provider: str, user: str | None = None) -> float:
    """Return the configured budget cap for *provider* from LiteLLM's DB.

    LiteLLM stores user/team budget configs in the ``budget_limits`` table
    (LiteLLM Proxy >= 1.30).  Returns 0.0 if not found or table doesn't exist.

    Args:
        provider: Provider name (e.g. ``"openai"``).
        user:     LiteLLM user/team key. Defaults to ``LLM_ROUTER_LITELLM_USER``.

    Returns:
        Monthly cap in USD, or 0.0 if not set.
    """
    import asyncio

    db_path = os.environ.get(_DB_ENV, "")
    if not db_path:
        return 0.0
    # Offload synchronous Path.exists() to thread pool to avoid blocking event loop
    exists = await asyncio.to_thread(Path(db_path).exists)
    if not exists:
        return 0.0

    user = user or os.environ.get(_USER_ENV, "")

    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            if user:
                cursor = await db.execute(
                    "SELECT max_budget FROM budget_limits WHERE user_id = ? LIMIT 1",
                    (user,),
                )
            else:
                cursor = await db.execute(
                    "SELECT max_budget FROM budget_limits LIMIT 1"
                )
            row = await cursor.fetchone()
            return float(row[0]) if row and row[0] else 0.0
    except Exception:
        return 0.0
