"""Shadow-only "local" tier flag (plan ~/.rsi/research/local-usage/PLAN.md, P2).

``LLM_ROUTER_LOCAL_TIER`` = ``off`` (default) | ``shadow``. In shadow the
classifier path records the tier a prompt WOULD get ("local") on the routing
decision; the served route is never changed. Any other value reads as off, so a
typo cannot switch behaviour.

Eligible: task_type in {query, generate, summary, classification, extraction}
at complexity ``simple``. ``TaskType`` today has only query and generate; the
other names are accepted so the rule does not need to change when the
classifier starts emitting them. Never local: anything above simple.
"""

from __future__ import annotations

import os

LOCAL_TIER = "local"
_ELIGIBLE_TASK_TYPES = frozenset({"query", "generate", "summary", "classification", "extraction"})


def mode() -> str:
    raw = os.environ.get("LLM_ROUTER_LOCAL_TIER", "").strip().lower()
    return "shadow" if raw == "shadow" else "off"


def _val(x: object) -> str:
    return str(getattr(x, "value", x) or "").strip().lower()


def would_be_tier(task_type: object, complexity: object) -> str | None:
    """``"local"`` when shadow is on and the prompt qualifies, else ``None``."""
    if mode() != "shadow":
        return None
    if _val(complexity) == "simple" and _val(task_type) in _ELIGIBLE_TASK_TYPES:
        return LOCAL_TIER
    return None
