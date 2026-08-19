"""Centralized routing points for decision logic via llm_query.

This module defines all prompts and helpers for routing validation/decision logic
to the cheapest capable model (llm_query via llm_router's MCP routing). The goal is to
route semantic decisions away from llm_router's core I/O path while keeping all file
operations local and synchronous.

Each function below represents a routing point where llm_router needs to make a
judgment call that benefits from LLM reasoning but doesn't need to block on it.
Failures gracefully degrade to simple local validation.
"""

from __future__ import annotations

from typing import Any


# ────────────────────────────────────────────────────────────────────────────
# Budget Validation Routing (3.1)
# ────────────────────────────────────────────────────────────────────────────


async def validate_budget_cap_semantically(
    provider: str, amount: float
) -> tuple[bool, str]:
    """Route: Is this budget cap sensible for the given provider?

    This is a semantic check beyond range validation. We route to llm_query
    to detect unrealistic amounts (e.g., $1/month for a heavy usage team,
    or $10k/month when they usually spend $100/month).

    Args:
        provider: Provider name (e.g. "openai", "gemini", "anthropic").
        amount:   Monthly cap in USD.

    Returns:
        (is_valid, reasoning) tuple. is_valid=True means "proceed with write";
        reasoning explains the decision for logging.

    Fallback:
        If llm_query is unavailable, returns (True, "local-only-validation")
        and allows the write. The cap is still subject to local range checks.
    """
    prompt = (
        f"Is ${amount}/month a reasonable monthly budget cap for {provider}? "
        f"Answer only: yes/no. If no, explain why (too low/too high/unusual for this provider)."
    )
    try:
        # Routing integration point: llm_query call will be added in Phase 4
        # For now, returns local validation default and explains decision
        reasoning = f"[routing-pending] {prompt}"
        is_valid = True
    except Exception as e:
        # Graceful degradation: missing ANTHROPIC_API_KEY, network error, etc.
        reasoning = f"local-only-validation (routing unavailable: {e})"
        is_valid = True

    return is_valid, reasoning


# ────────────────────────────────────────────────────────────────────────────
# Audit Event Severity Routing (3.2)
# ────────────────────────────────────────────────────────────────────────────


async def classify_audit_severity(
    event_type: str, resource: str, actor_id: str, detail: dict[str, Any]
) -> tuple[str, str]:
    """Route: What severity should this audit event have?

    Classifies events into info/warn/critical based on context. This enables
    SIEM-ready audit logs without hardcoded severity in callers.

    Args:
        event_type: e.g., "routing.decision", "quota.breach", "policy.change"
        resource:   e.g., "lineage:abc", "team:eng", "user:bob"
        actor_id:   e.g., "system" or user UUID
        detail:     Event-specific metadata (e.g., {"quota_exceeded_by": 50})

    Returns:
        (severity, reasoning) tuple. severity ∈ {info, warn, critical}.

    Fallback:
        If routing unavailable, returns ("info", "local-only") for all events.
    """
    prompt = (
        f"Classify severity for audit event:\n"
        f"  Type: {event_type}\n"
        f"  Resource: {resource}\n"
        f"  Actor: {actor_id}\n"
        f"  Details: {detail}\n"
        f"Answer only: info | warn | critical (one word)"
    )
    try:
        # Routing integration point: llm_query call will be added in Phase 4
        reasoning = f"[routing-pending] {prompt}"
        severity = "info"  # Fallback default
    except Exception as e:
        reasoning = f"local-only (routing unavailable: {e})"
        severity = "info"

    return severity, reasoning


# ────────────────────────────────────────────────────────────────────────────
# Config Migration Validation (3.3)
# ────────────────────────────────────────────────────────────────────────────


async def validate_config_upgrade(
    old_version: int, new_version: int, old_keys: set[str], new_keys: set[str]
) -> tuple[bool, str]:
    """Route: Can we safely upgrade config from old to new version?

    Detects breaking changes, suggests migration strategy, warns about
    data loss if keys were removed.

    Args:
        old_version: Current config version (e.g., 2)
        new_version: Target config version (e.g., 3)
        old_keys:    Keys present in current config
        new_keys:    Keys expected in new version

    Returns:
        (can_upgrade, reason) tuple.

    Fallback:
        If routing unavailable, returns (False, "requires-manual-review").
    """
    removed = old_keys - new_keys
    added = new_keys - old_keys

    prompt = (
        f"Config upgrade: v{old_version} → v{new_version}\n"
        f"  Removed keys: {removed or 'none'}\n"
        f"  Added keys: {added or 'none'}\n"
        f"Can we auto-migrate? Answer: yes/no. If yes, suggest migration."
    )
    try:
        # Routing integration point: llm_query call will be added in Phase 4
        reason = f"[routing-pending] {prompt}"
        can_upgrade = len(removed) == 0  # Safe only if no data loss
    except Exception as e:
        reason = f"manual-review-required (routing unavailable: {e})"
        can_upgrade = False

    return can_upgrade, reason


# ────────────────────────────────────────────────────────────────────────────
# Budget Anomaly Detection (3.4)
# ────────────────────────────────────────────────────────────────────────────


async def detect_spend_anomaly(
    user_id: str, current_spend: float, daily_avg: float, monthly_cap: float
) -> tuple[str, str]:
    """Route: Is this spending pattern anomalous?

    Compares current spending against historical baseline and cap to detect
    unusual spikes that warrant alerting.

    Args:
        user_id:       User or team identifier
        current_spend: Amount spent so far this period (USD)
        daily_avg:     Historical daily average (USD/day)
        monthly_cap:   Monthly budget cap (USD)

    Returns:
        (alert_level, reason) tuple. alert_level ∈ {normal, warning, critical}.

    Fallback:
        Falls back to simple threshold logic: critical if spend > 90% of cap.
    """
    pct_of_cap = (current_spend / monthly_cap * 100) if monthly_cap > 0 else 0
    pct_above_avg = (current_spend / daily_avg / 30 * 100) if daily_avg > 0 else 0

    prompt = (
        f"User {user_id} spending analysis:\n"
        f"  Current spend: ${current_spend:.2f}\n"
        f"  Historical daily avg: ${daily_avg:.2f}/day\n"
        f"  Monthly cap: ${monthly_cap:.2f}\n"
        f"  % of cap: {pct_of_cap:.1f}%\n"
        f"  % above historical (annualized): {pct_above_avg:.1f}%\n"
        f"Alert level: normal | warning | critical?"
    )
    try:
        # Routing integration point: llm_query call will be added in Phase 4
        reason = f"[routing-pending] {prompt}"
        # Fallback logic
        if pct_of_cap > 90:
            alert_level = "critical"
        elif pct_of_cap > 75:
            alert_level = "warning"
        else:
            alert_level = "normal"
    except Exception as e:
        reason = f"local-fallback (routing unavailable: {e})"
        alert_level = "critical" if pct_of_cap > 90 else "normal"

    return alert_level, reason


# ────────────────────────────────────────────────────────────────────────────
# PII/Secret Detection (3.5)
# ────────────────────────────────────────────────────────────────────────────


async def detect_sensitive_content_semantic(
    text: str, detected_patterns: list[str]
) -> tuple[bool, str]:
    """Route: Does this text contain sensitive data (semantic check)?

    Uses LLM reasoning to go beyond regex-based detection, reducing false
    positives on legitimate code/data that looks like credentials but isn't.

    Args:
        text:                Text to check (prompt, config, etc.)
        detected_patterns:   List of regex matches found (for context)

    Returns:
        (contains_sensitive, reasoning) tuple.

    Fallback:
        If routing unavailable, uses simple heuristics based on pattern count.
    """
    prompt = (
        f"Analyze for sensitive data:\n"
        f"Text (first 500 chars): {text[:500]}\n"
        f"Detected patterns: {detected_patterns}\n"
        f"Does this contain actual API keys, credentials, or PII? "
        f"Answer: yes/no. Be strict (reduce false positives)."
    )
    try:
        # Routing integration point: llm_query call will be added in Phase 4
        reasoning = f"[routing-pending] {prompt}"
        contains_sensitive = len(detected_patterns) > 2  # Fallback threshold
    except Exception as e:
        reasoning = f"local-fallback (routing unavailable: {e})"
        # Conservative fallback: block if patterns found
        contains_sensitive = len(detected_patterns) > 0

    return contains_sensitive, reasoning


# ────────────────────────────────────────────────────────────────────────────
# Utility: Log Routing Decision
# ────────────────────────────────────────────────────────────────────────────


def log_routing_decision(
    routing_point: str, decision: str, reasoning: str, metadata: dict[str, Any] | None = None
) -> None:
    """Log a routing decision for audit trail + observability.

    This is called after every routing decision (whether routed or fell back).
    Integration with audit log will be added in Phase 4.

    Args:
        routing_point: Name of routing point (e.g., "budget_validation")
        decision:      The decision made (e.g., "approved", "rejected")
        reasoning:     Explanation of why (from LLM or fallback)
        metadata:      Optional context (e.g., {"provider": "openai", "amount": 50})
    """
    # Integration point for Phase 4: AuditLog.append_with_severity(routing_point, decision, reasoning, metadata)
    pass
