"""Storage layer validators that route decisions to cheap models.

These validators wrap the routing_hints functions and integrate with adapters.
"""

from __future__ import annotations

from typing import Any

from llm_router.routing_hints import (
    validate_budget_cap_semantically,
    classify_audit_severity,
    validate_config_upgrade,
)


async def validate_budget_before_write(provider: str, amount: float) -> tuple[bool, str]:
    """Validate budget cap before persisting.

    Routing Point 3.1: Routes semantic validation to llm_query.

    Args:
        provider: Provider name
        amount:   Budget cap in USD

    Returns:
        (is_valid, reasoning) tuple
    """
    return await validate_budget_cap_semantically(provider, amount)


async def classify_event_severity(
    event_type: str, resource: str, actor_id: str, detail: dict[str, Any]
) -> tuple[str, str]:
    """Classify audit event severity.

    Routing Point 3.2: Routes context-aware classification to llm_query.

    Args:
        event_type: Type of audit event
        resource:   Resource affected
        actor_id:   Actor performing action
        detail:     Event details

    Returns:
        (severity, reasoning) tuple
    """
    return await classify_audit_severity(event_type, resource, actor_id, detail)


async def validate_config_migration_path(
    old_version: int, new_version: int, old_keys: set[str], new_keys: set[str]
) -> tuple[bool, str]:
    """Validate config migration is safe.

    Routing Point 3.3: Routes compatibility checks to llm_query.

    Args:
        old_version: Current version
        new_version: Target version
        old_keys:    Keys in current config
        new_keys:    Keys in target config

    Returns:
        (can_migrate, reasoning) tuple
    """
    return await validate_config_upgrade(old_version, new_version, old_keys, new_keys)
