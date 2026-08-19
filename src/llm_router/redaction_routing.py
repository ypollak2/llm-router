"""T4-M1: prompt redaction at the routing chokepoint.

Closes the first slice of G-013 (``enterprise/redaction.py`` shipped
but had zero callers from the routing path). When the
``LLM_ROUTER_REDACTION`` env switch is set to ``on``, the prompt that
``route_and_call`` receives is scrubbed via
``enterprise.redaction.redact_prompt`` **before** it reaches any
provider. The audit row carries per-pattern hit counts so operators
can observe the scrub rate without persisting any PII.

Modes via ``LLM_ROUTER_REDACTION``:

* **off** (default) — no-op. Preserves pre-T4-M1 behaviour (prompt
  passes through unchanged). Operators who haven't reviewed which
  patterns llm_router redacts must opt in explicitly.
* **on** — every routed turn's prompt is redacted before dispatch.
  Audit detail records ``redactions={pii: N, email: N, ...}``.

The redaction policy is the ``RedactionPolicy.default()`` from
``enterprise.redaction``; per-tenant / per-classification policies
land in T4-M2 (per-classification provider allow-list) and T4-XL1
(full ZDR plumbing).

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-013.
"""
from __future__ import annotations

import os

from llm_router.logging import get_logger
from llm_router.plugins.redaction import get_redactor
from llm_router.profile import is_enterprise

log = get_logger("llm_router.redaction_routing")


class RedactionUnavailable(RuntimeError):
    """Redaction was requested but could not be applied, so the turn was refused.

    A distinct type rather than a bare RuntimeError: a caller that wants to degrade
    deliberately (retry, drop to a local model, surface a message) needs to tell "the
    prompt could not be scrubbed" apart from every other failure. Raising something
    unrecognisable would push callers toward `except Exception`, which is how the
    fail-open this replaced came to exist.
    """


_REDACTION_ENV = "LLM_ROUTER_REDACTION"
_AFFIRMATIVE = {"on", "1", "true", "yes", "strict"}


def _redaction_enabled() -> bool:
    # G-012: an explicit affirmative env always enables; an explicit
    # non-affirmative value (e.g. ``off``) always disables — including
    # the documented enterprise operator opt-out. Only when the env is
    # unset does the deployment profile decide: enterprise defaults
    # redaction on, developer keeps it off.
    raw = (os.environ.get(_REDACTION_ENV) or "").strip().lower()
    if raw in _AFFIRMATIVE:
        return True
    if raw == "" and is_enterprise():
        return True
    return False


def maybe_redact(prompt: str) -> tuple[str, dict[str, int]]:
    """Return ``(scrubbed_prompt, counts)`` per the env switch.

    * Off (default) → returns the prompt unchanged and an empty
      counts dict. The caller can avoid extra audit-detail entries by
      checking ``if counts: ...``.
    * On → calls the registered redactor with the default policy. The
      redacted prompt is what the provider sees; the counts dict
      records how many of each pattern type fired.

    FAIL-CLOSED. When redaction is enabled and cannot be applied — no redactor
    registered, or the redactor raises — this raises `RedactionUnavailable` instead
    of returning the prompt. The turn fails; the PII does not leave.

    This replaced a fail-open path that logged a warning and returned the ORIGINAL
    prompt. It was found when a mutation run captured
    ``{'error': 'redactor died', 'event': 'redaction_failed'}`` followed by
    "please review code by alice@example.com" arriving at the dispatcher intact.

    Someone sets ``LLM_ROUTER_REDACTION=on`` for exactly one reason: to keep PII out of a
    third-party model. Fail-open turned that request into a best-effort attempt whose
    failure was reported only in a log nobody reads. The caller could not tell, so it
    could not react — the guarantee silently became a hope.

    The old docstring argued that operators wanting fail-closed guarantees "should
    refuse to start llm_router without a valid redaction policy at boot time, not bolt it
    onto every turn". That does not hold: boot-time validation cannot catch a redactor
    that is present and valid at startup and raises on a particular prompt, which is
    precisely the observed failure.

    THE COST, STATED: a broken redactor now breaks the turn. That is the point — a
    failed request is recoverable, a leaked secret is not — but it does convert a
    silent degradation into a loud failure, and callers that previously always got a
    string must now handle `RedactionUnavailable`.
    """
    if not _redaction_enabled():
        return prompt, {}

    redactor = get_redactor()
    if redactor is None:
        # Enabled but unconfigured. Previously returned the prompt unchanged, so a
        # missing registration silently disabled the feature it was asked to provide.
        log.error("redaction_unavailable", reason="no_redactor_registered")
        raise RedactionUnavailable(
            "LLM_ROUTER_REDACTION is enabled but no redactor is registered — refusing to "
            "send the prompt unredacted."
        )

    try:
        result = redactor.redact_prompt(prompt)
    except Exception as err:
        log.error("redaction_failed", error=str(err))
        raise RedactionUnavailable(
            f"redaction failed ({err}) — refusing to send the prompt unredacted."
        ) from err
    return result.text, dict(result.counts)


__all__ = [
    "RedactionUnavailable",
    "maybe_redact",
]
