"""Dynamic routing table builder — creates optimized chains at session startup.

At session start, discovers available providers and builds custom routing tables
that reflect the actual configured APIs and running services. This is much faster
than discovering on every routing call.

The dynamic tables follow the same preference order as static profiles.py,
but remove unavailable providers, resulting in shorter, more efficient chains.
"""

from __future__ import annotations

from llm_router.logging import get_logger
from llm_router.profiles import (
    ROUTING_TABLE,
    RoutingProfile,
    TaskType,
    provider_from_model,
)
from llm_router.discover import get_available_providers

log = get_logger("llm_router.dynamic_routing")

# Cache for dynamic routing tables (built at session startup)
_dynamic_routing_table: dict[tuple[RoutingProfile, TaskType], list[str]] | None = None
_discovery_complete = False


def build_dynamic_routing_table(
    available_providers: set[str] | None = None,
) -> dict[tuple[RoutingProfile, TaskType], list[str]]:
    """Build custom routing tables based on discovered available providers.
    
    Takes the static routing table from profiles.py and filters each chain
    to only include providers that are actually available.
    
    The filtering preserves the preference order — high-priority models stay
    first as long as their provider is available.
    
    Args:
        available_providers: Set of available provider names. If None, discovers automatically.
    
    Returns:
        Custom routing table with all unavailable providers filtered out.
    """
    if available_providers is None:
        available_providers = get_available_providers()
    
    # Always allow codex and ollama (they're local/special and handled separately)
    available_providers = available_providers | {"codex", "ollama"}
    
    dynamic_table: dict[tuple[RoutingProfile, TaskType], list[str]] = {}
    
    for (profile, task_type), chain in ROUTING_TABLE.items():
        # Filter chain to only available providers
        filtered_chain = [
            model for model in chain
            if provider_from_model(model) in available_providers
        ]
        dynamic_table[(profile, task_type)] = filtered_chain
    
    return dynamic_table


def initialize_dynamic_routing(available_providers: set[str] | None = None) -> None:
    """Initialize dynamic routing tables at session startup.
    
    Call this once when the server starts. It discovers available providers
    and builds custom routing tables that will be used for all subsequent
    routing decisions.
    
    Args:
        available_providers: Optional set of providers. If None, discovers automatically.
    """
    global _dynamic_routing_table, _discovery_complete
    
    if _discovery_complete:
        return  # Already initialized
    
    try:
        if available_providers is None:
            available_providers = get_available_providers()
        
        _dynamic_routing_table = build_dynamic_routing_table(available_providers)
        _discovery_complete = True
        
        # Log summary
        provider_names = ", ".join(sorted(available_providers))
        total_chains = len(ROUTING_TABLE)
        dynamic_chains = sum(1 for chain in _dynamic_routing_table.values() if chain)
        
        log.info(
            "Dynamic routing initialized",
            available_providers=provider_names,
            provider_count=len(available_providers),
            total_chains=total_chains,
            usable_chains=dynamic_chains,
        )
    except Exception as e:
        log.warning("Failed to initialize dynamic routing, will fall back to static: %s", e)
        _discovery_complete = True


def get_dynamic_routing_table() -> dict[tuple[RoutingProfile, TaskType], list[str]] | None:
    """Get the dynamically built routing table.
    
    Returns None if discovery hasn't been initialized yet.
    Caller should fall back to static ROUTING_TABLE if this returns None.
    
    Returns:
        Custom routing table, or None if not yet initialized.
    """
    return _dynamic_routing_table


def get_dynamic_model_chain(
    profile: RoutingProfile,
    task_type: TaskType,
) -> list[str] | None:
    """Get a single chain from the dynamic routing table.
    
    Returns None if the chain doesn't exist or discovery hasn't been initialized.
    
    Args:
        profile: Routing profile (budget/balanced/premium)
        task_type: Task type (query/code/research/etc)
    
    Returns:
        Model chain for this profile/task combo, or None if unavailable.
    """
    if _dynamic_routing_table is None:
        return None
    
    return _dynamic_routing_table.get((profile, task_type))


def reset_dynamic_routing() -> None:
    """Reset dynamic routing tables (for testing).
    
    Clears the cached tables so the next initialize call will rediscover.
    """
    global _dynamic_routing_table, _discovery_complete
    _dynamic_routing_table = None
    _discovery_complete = False
