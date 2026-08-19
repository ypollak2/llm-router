"""Persistent budget cap storage for llm_router.

REFACTORED (Phase 2): Now delegates to StorageService abstraction layer.
All file I/O is handled by storage/service.py, which enforces atomicity,
error recovery, and routing of validation decisions to cheap models.

Caps are stored in ``~/.llm-router/budgets.json`` and take priority over
environment-variable caps (``LLM_ROUTER_BUDGET_OPENAI``, etc.).

Usage::

    from llm_router.budget_store import get_caps, set_cap, remove_cap

    set_cap("openai", 20.0)      # persist $20/month cap
    caps = get_caps()            # {"openai": 20.0}
    remove_cap("openai")         # clear the cap
"""

from __future__ import annotations

from llm_router.storage import storage_service


# ── Public API ────────────────────────────────────────────────────────────────


def get_caps() -> dict[str, float]:
    """Return all persisted budget caps as ``{provider: monthly_cap_usd}``.

    Returns an empty dict when no caps have been set or the file is missing.

    Delegates to StorageService.read_budgets().
    """
    budgets = storage_service.read_budgets()
    return {b.provider: b.amount_usd for b in budgets}


def set_cap(provider: str, amount: float) -> None:
    """Persist a monthly budget cap for *provider*.

    Delegates to StorageService.write_budget(), which handles:
    - Routing Point 3.1: Semantic validation via llm_query
    - Atomic writes (tmp → rename)
    - Error recovery

    Args:
        provider: Provider name (e.g. ``"openai"``, ``"gemini"``).
        amount:   Monthly cap in USD.  Must be > 0.

    Raises:
        ValueError: If *amount* is not positive or validation rejects it.
    """
    storage_service.write_budget(provider=provider, amount=amount, source="cli")


def remove_cap(provider: str) -> bool:
    """Remove the persisted cap for *provider*.

    Delegates to StorageService.delete_budget().

    Args:
        provider: Provider name to clear.

    Returns:
        ``True`` if a cap was removed, ``False`` if none existed.
    """
    return storage_service.delete_budget(provider)


def list_caps() -> dict[str, float]:
    """Alias for :func:`get_caps` — returns all persisted caps."""
    return get_caps()


def get_cap(provider: str) -> float:
    """Return the persisted cap for *provider*, or ``0.0`` if not set."""
    return get_caps().get(provider, 0.0)
