"""Dynamic routing table builder — creates optimized chains at session startup.

At session start, discovers available providers and builds custom routing tables
that reflect the actual configured APIs, running services, AND quota status.

This includes:
  1. Provider availability (API keys configured, services running)
  2. Quota status (Claude subscription %, Gemini CLI daily usage)
  3. Service tiers from profile.yaml (free local > free subscriptions > paid)

The dynamic tables follow the same preference order as static profiles.py,
but remove unavailable providers and deprioritize quota-depleted services.
"""

from __future__ import annotations

from pathlib import Path

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

PROFILE_PATH = Path.home() / ".llm-router" / "profile.yaml"


def _load_user_profile() -> dict | None:
    """Load the user's auto-generated profile.yaml if it exists.
    
    Returns:
        Dict with detected services and quota info, or None if profile doesn't exist.
    """
    if not PROFILE_PATH.exists():
        return None
    
    try:
        import yaml
        with open(PROFILE_PATH) as f:
            profile = yaml.safe_load(f)
        return profile
    except Exception as e:
        log.debug("Failed to load profile.yaml: %s", e)
        return None


def _get_quota_pressure() -> dict[str, float]:
    """Extract quota pressure from profile.yaml.
    
    Quota pressure is a value 0.0–1.0 indicating how much of the quota is used:
      - 0.0 = completely free
      - 0.5 = 50% used
      - 1.0 = completely depleted
    
    Returns:
        Dict mapping provider names to their pressure (0.0–1.0).
    """
    profile = _load_user_profile()
    if not profile:
        return {}
    
    pressure = {}
    quotas = profile.get("quotas", {})
    
    # Claude subscription pressure
    if "claude_subscription" in quotas:
        remaining_str = quotas["claude_subscription"].get("remaining_percent", "100%")
        try:
            remaining = float(remaining_str.rstrip("%"))
            pressure["anthropic"] = max(0.0, min(1.0, (100 - remaining) / 100))
        except (ValueError, AttributeError):
            pass
    
    # Gemini CLI daily quota pressure
    if "gemini_cli" in quotas:
        used_str = quotas["gemini_cli"].get("used_today", "0/1500")
        daily_limit = quotas["gemini_cli"].get("daily_limit", 1500)
        try:
            used = int(used_str.split("/")[0])
            pressure["gemini_cli"] = max(0.0, min(1.0, used / daily_limit))
        except (ValueError, AttributeError, IndexError):
            pass
    
    return pressure


def _detect_available_tiers() -> dict[str, bool]:
    """Extract which service tiers are available from profile.yaml.
    
    Returns:
        Dict mapping tier names to whether they're available.
        E.g. {"free_local": True, "free_subscriptions": True, "cheap_apis": True}
    """
    profile = _load_user_profile()
    if not profile:
        return {}
    
    available = {}
    routing_tiers = profile.get("routing", {}).get("model_priority", [])
    
    for tier_info in routing_tiers:
        tier_name = tier_info.get("tier")
        models = tier_info.get("models", [])
        if tier_name and models:
            available[tier_name] = True
    
    return available


def _reorder_by_quota_pressure(
    chain: list[str],
    quota_pressure: dict[str, float],
) -> list[str]:
    """Reorder a chain to deprioritize quota-depleted providers.
    
    When a provider's quota pressure is >= 0.85 (85%+ used), move its models
    to the end of the chain. This keeps routing efficient by trying free/cheap
    options before expensive ones that are running low on quota.
    
    Args:
        chain: Ordered list of model IDs
        quota_pressure: Dict mapping providers to their pressure (0.0–1.0)
    
    Returns:
        Reordered chain with high-pressure models moved to the end.
    """
    if not quota_pressure:
        return chain
    
    high_pressure = []
    normal = []
    
    for model in chain:
        provider = provider_from_model(model)
        pressure = quota_pressure.get(provider, 0.0)
        
        if pressure >= 0.85:
            high_pressure.append(model)
        else:
            normal.append(model)
    
    # Keep normal pressure models first, push high-pressure to the end
    return normal + high_pressure


def build_dynamic_routing_table(
    available_providers: set[str] | None = None,
) -> dict[tuple[RoutingProfile, TaskType], list[str]]:
    """Build custom routing tables based on discovered providers and quota status.
    
    Takes the static routing table from profiles.py and:
      1. Filters each chain to only include available providers
      2. Reorders models to deprioritize quota-depleted services
    
    The filtering and reordering preserves the preference order — high-priority
    models stay first as long as their provider is available and not depleted.
    
    Args:
        available_providers: Set of available provider names. If None, discovers automatically.
    
    Returns:
        Custom routing table with unavailable and quota-depleted providers deprioritized.
    """
    if available_providers is None:
        available_providers = get_available_providers()
    
    # Always allow codex and ollama (they're local/special and handled separately)
    available_providers = available_providers | {"codex", "ollama", "gemini_cli"}
    
    # Get quota pressure information
    quota_pressure = _get_quota_pressure()
    
    dynamic_table: dict[tuple[RoutingProfile, TaskType], list[str]] = {}
    
    for (profile, task_type), chain in ROUTING_TABLE.items():
        # Step 1: Filter chain to only available providers
        filtered_chain = [
            model for model in chain
            if provider_from_model(model) in available_providers
        ]
        
        # Step 2: Reorder by quota pressure (move depleted services to end)
        reordered = _reorder_by_quota_pressure(filtered_chain, quota_pressure)
        
        dynamic_table[(profile, task_type)] = reordered
    
    return dynamic_table


def initialize_dynamic_routing(available_providers: set[str] | None = None) -> None:
    """Initialize dynamic routing tables at session startup.
    
    Call this once when the server starts. It discovers available providers,
    loads quota information from profile.yaml, and builds custom routing tables
    that will be used for all subsequent routing decisions.
    
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
        
        # Log quota pressure if available
        quota_pressure = _get_quota_pressure()
        if quota_pressure:
            pressure_str = ", ".join(
                f"{p}={int(pr*100)}%"
                for p, pr in sorted(quota_pressure.items())
            )
            log.info(
                "Dynamic routing initialized with quota pressure",
                available_providers=provider_names,
                provider_count=len(available_providers),
                quota_pressure=pressure_str,
                total_chains=total_chains,
                usable_chains=dynamic_chains,
            )
        else:
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
