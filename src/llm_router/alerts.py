"""Best-effort operational alert sink for critical llm_router events.

llm_router already *detects* and *acts on* critical conditions (audit-chain
tamper via ``verify_chain``, runaway-agent circuit-breaker trips, agent
emergency stops, budget Postgres fail-open), but historically only
*logged* them. This module adds an active alert path so a silent failure
pages instead of scrolling past in a log.

Design: **fail-open, never raises.** An alert that can't be delivered must
never break the routed turn or admin action that triggered it. Every path
here swallows its exceptions. Emitting always logs at ``critical`` via the
standard structlog logger; if ``LLM_ROUTER_ALERT_WEBHOOK`` is set, it also
POSTs a small JSON envelope there. Disable entirely with
``LLM_ROUTER_ALERTS_DISABLED=1``.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from llm_router.logging import get_logger

log = get_logger("llm_router.alerts")

_AFFIRMATIVE = {"1", "on", "true", "yes"}

# Canonical event names. Callers pass one of these so the log/webhook
# stream is greppable and dashboards can key on a stable set.
AUDIT_TAMPER = "audit_chain_tamper"
RUNAWAY_BREAKER_TRIP = "runaway_agent_breaker_trip"
AGENT_EMERGENCY_STOP = "agent_emergency_stop"
BUDGET_POSTGRES_FALLBACK = "budget_postgres_fallback"
INVOICE_DISCREPANCY = "invoice_discrepancy"


def _env_on(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _AFFIRMATIVE


def emit_alert(
    event: str,
    *,
    severity: str = "critical",
    detail: dict | None = None,
) -> None:
    """Best-effort critical alert sink; fail open so alerting never breaks callers."""
    if _env_on("LLM_ROUTER_ALERTS_DISABLED"):
        return

    payload_detail = detail or {}

    try:
        log.critical(event, severity=severity, **payload_detail)
    except Exception:
        pass

    webhook = (os.environ.get("LLM_ROUTER_ALERT_WEBHOOK") or "").strip()
    if not webhook:
        return

    try:
        body = json.dumps(
            {"event": event, "severity": severity, "detail": payload_detail}
        ).encode("utf-8")
        request = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3):
            pass
    except Exception as exc:
        try:
            log.warning("alert_webhook_failed", error=str(exc))
        except Exception:
            pass


__all__ = [
    "emit_alert",
    "AUDIT_TAMPER",
    "RUNAWAY_BREAKER_TRIP",
    "AGENT_EMERGENCY_STOP",
    "BUDGET_POSTGRES_FALLBACK",
    "INVOICE_DISCREPANCY",
]
