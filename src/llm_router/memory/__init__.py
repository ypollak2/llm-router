"""Personal routing memory system for v6.1.

Learns from user corrections (llm_reroute) and routing feedback (llm_rate)
to build persistent learned profiles that override default routing.

Components:
- profiles.py: Build learned profiles from corrections history
- session-start.py: Inject learned routes as banner + hard overrides
- auto-route.py: Apply learned routes with confidence-based priority
"""

from .profiles import (
    fetch_corrections_history,
    build_learned_profile,
    save_learned_profile,
    load_learned_profile,
    LearnedRoute,
)

__all__ = [
    "fetch_corrections_history",
    "build_learned_profile",
    "save_learned_profile",
    "load_learned_profile",
    "LearnedRoute",
]
