"""F23: which providers a routed call passed over before the served one, and why.

Evidence: post-deploy-24h 2026-10-17 sections 2-3. A Codex usage-limit bench (10-16 12:47-16:06Z)
made the router serve local Ollama, and the row said only ``router_unhinted``; the D-116 reorder
logged at ``log.debug``; four ollama-first rows outside any bench had no cause at all. Bench-driven
local share could not be told from policy-driven local share.

One per-call list lives in a ``ContextVar``. ``begin()`` starts it at the top of ``route_and_call``;
``note()`` is called where an entry is dropped or skipped; ``log_routing_decision`` reads
``entries_so_far()`` and stores it as compact JSON in ``routing_decisions.skipped_json``. A writer that
never called ``begin()`` (hooks, direct path) stores NULL = unknown, never ``[]``.

Entry shape: ``{"model": "codex/gpt-5", "reason": "bench", "until": 1790000000.0}``.
Ids, enums and epochs only: no prompt text, no exception text.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

# Reasons. Build-time reasons drop an entry before dispatch; the rest skip it inside the loop.
BENCH = "bench"                      # provider_reset bench (usage limit): ``until`` epoch is recorded
BREAKER = "breaker"                  # circuit breaker open (not a bench)
UNAVAILABLE = "unavailable"          # provider not configured / not installed / resource not connected
BLOCKED = "blocked"                  # block_providers / LLM_ROUTER_BLOCK_PROVIDERS
POLICY = "policy"                    # org / repo allow-block policy
CAPABILITY_FILTERED = "capability_filtered"  # local model not validated for this task class (P0.15 gate)
BUDGET = "budget"                    # provider budget exhausted / premium cap / per-task cost cap
ALLOWLIST = "allowlist"              # per-classification provider allow-list
AGENT_POLICY = "agent_policy"        # agent routing policy (provider preference / turn cost)
QUALITY = "quality"                  # quality circuit-breaker skip
LOCAL_TIMEOUT = "local_timeout"      # local model timed out, answer already in hand
FLOOR = "floor"                      # F26: chain was empty after all filters; served from the last-resort floor (``tier``)
FAILED_ELIGIBILITY = "failed_eligibility"  # F106: local model measurably failed this lane (``lane``, ``k``, ``n``, ``source``)

BUILD_REASONS = frozenset({UNAVAILABLE, BLOCKED, POLICY, CAPABILITY_FILTERED, FLOOR, FAILED_ELIGIBILITY})

# Causes the report splits local share into (see routing_report.executed_routes).
CAUSE_BENCH = "bench"
CAUSE_LAST_RESORT = "last_resort"
CAUSE_OTHER_SKIP = "other_skip"
CAUSE_FLOOR = "floor"
CAUSE_POLICY = "policy"
CAUSE_UNKNOWN = "unknown"
CAUSES = (CAUSE_POLICY, CAUSE_FLOOR, CAUSE_BENCH, CAUSE_LAST_RESORT, CAUSE_OTHER_SKIP, CAUSE_UNKNOWN)

_log: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_router_skip_causes", default=None)


def begin() -> None:
    """Start a fresh list for the call running in this context."""
    _log.set([])


def note(model: str, reason: str, **detail: Any) -> None:
    """Record that ``model`` was passed over. No-op outside a ``begin()`` scope."""
    entries = _log.get()
    if entries is None:
        return
    entry: dict[str, Any] = {"model": str(model), "reason": reason}
    entry.update({k: v for k, v in detail.items() if v is not None})
    entries.append(entry)


def note_many(models: list[str], reason: str) -> None:
    for m in models:
        note(m, reason)


def entries_so_far() -> list[dict[str, Any]] | None:
    """The entries so far, or None when this context never began a list (unknown)."""
    entries = _log.get()
    return None if entries is None else list(entries)


def to_json(entries: list[dict[str, Any]] | None) -> str | None:
    """Compact JSON (no spaces: the report matches ``"reason":"bench"`` with LIKE); None stays None."""
    if entries is None:
        return None
    return json.dumps(entries, separators=(",", ":"), sort_keys=True)


def served_by_last_resort(
    served_provider: str, task_type: str, skipped: list[dict[str, Any]] | None,
    failed_models: list[str],
) -> bool | None:
    """True when a local Q&A model served after the loop passed over or failed a non-local,
    non-Claude entry that D-116 had placed ahead of it (``qa_policy.strip_local_for_qa``).

    None = unknown (no skip list was kept). Build-time drops do not count: an entry that never
    reached the loop was not ahead of the local model, it was absent from the chain.
    """
    from llm_router.contracts import CLAUDE_PROVIDERS
    from llm_router.qa_policy import QA_STRIP_PROVIDERS, QA_TASK_TYPES

    if skipped is None:
        return None
    if served_provider not in QA_STRIP_PROVIDERS or task_type not in QA_TASK_TYPES:
        return False

    def _ahead(model: str) -> bool:
        prov = model.split("/", 1)[0] if "/" in model else model
        return prov not in QA_STRIP_PROVIDERS and prov not in CLAUDE_PROVIDERS

    loop_skips = [e["model"] for e in skipped if e.get("reason") not in BUILD_REASONS]
    return any(_ahead(m) for m in loop_skips + list(failed_models))
