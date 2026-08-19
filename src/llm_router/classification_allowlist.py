"""T4-M2: per-classification provider allow-list.

Operators pin which providers may see which classes of prompt so
sensitive task types never reach the wrong vendor. The canonical
example: a tenant happy to send RESEARCH queries to a hosted
web-grounded model still wants CODE prompts to stay on the on-prem
provider that signed the data-processing addendum.

Config:

* ``LLM_ROUTER_CLASSIFICATION_ALLOWLIST`` — JSON dict mapping the
  ``TaskType`` value (lowercase string, e.g. ``"code"``, ``"query"``,
  ``"research"``) to a list of allowed provider names. Example::

      {"code": ["openai", "anthropic"], "research": ["perplexity"]}

  Task types not present in the config are unrestricted — operators
  opt in per-classification, not opt out. An unset env var means no
  enforcement regardless of mode.

* ``LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE`` — ``off`` (default) /
  ``warn`` / ``strict``.

  * **off** — no enforcement. The router never calls the check, or it
    returns ``(off, True)``.
  * **warn** — the candidate is allowed but a structured warning is
    emitted so operators can dashboard violations before flipping
    strict. Equivalent to "shadow mode."
  * **strict** — the candidate is refused. The router skips to the
    next chain entry; if the whole chain is refused, ``PermissionDenied``
    bubbles up the existing T1-M3 surface.

Per-tenant overrides land in Phase 3b alongside the rest of the
multi-tenant config plane — Q-P-2 hybrid A→B path keeps the
single-org-per-instance scope here.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-013 (privacy /
data-residency slice — first slice was T4-M1 prompt redaction).
"""
from __future__ import annotations

import json
import os

from llm_router.logging import get_logger
from llm_router.types import TaskType

log = get_logger("llm_router.classification_allowlist")


_ENV_CONFIG = "LLM_ROUTER_CLASSIFICATION_ALLOWLIST"
_ENV_MODE = "LLM_ROUTER_CLASSIFICATION_ALLOWLIST_MODE"

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_STRICT = "strict"


def _resolve_mode() -> str:
    """Read the env mode. Synonyms: strict/hard, warn/soft/shadow."""
    raw = (os.environ.get(_ENV_MODE) or "").strip().lower()
    if raw in {"strict", "hard"}:
        return MODE_STRICT
    if raw in {"warn", "soft", "shadow"}:
        return MODE_WARN
    return MODE_OFF


def _resolve_allowlist() -> dict[str, set[str]]:
    """Parse the env config into ``{classification: {provider, ...}}``.

    Failures fall back to an empty dict and a structured warning —
    the goal is to never break routing if an operator misconfigures
    the env. Empty dict means "no entry for any classification" which
    in turn means every (classification, provider) is allowed; that's
    the safest fail-open posture for a freshly-misconfigured
    deployment. Operators who need fail-closed should validate the
    env at boot time, not bolt it onto every turn.
    """
    raw = (os.environ.get(_ENV_CONFIG) or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        log.warning("classification_allowlist_parse_failed", error=str(err))
        return {}
    if not isinstance(parsed, dict):
        log.warning(
            "classification_allowlist_not_dict",
            type=type(parsed).__name__,
        )
        return {}
    out: dict[str, set[str]] = {}
    for k, v in parsed.items():
        if not isinstance(v, list):
            log.warning("classification_allowlist_entry_not_list", key=str(k))
            continue
        out[str(k).strip().lower()] = {
            str(p).strip().lower() for p in v if isinstance(p, str)
        }
    return out


def check_classification_provider(
    task_type: TaskType, provider: str
) -> tuple[str, bool]:
    """Return ``(mode, allowed)`` for this ``(task_type, provider)``.

    Caller is responsible for acting on the tuple:

    * ``mode == "off"`` → ignore (always ``True``).
    * ``mode == "warn"`` → log + audit, allow.
    * ``mode == "strict"`` and ``not allowed`` → skip the candidate.

    Returning ``(mode, True)`` for classifications missing from the
    allow-list is the deliberate opt-in posture — operators dial up
    enforcement per task type as they validate each one.
    """
    mode = _resolve_mode()
    if mode == MODE_OFF:
        return MODE_OFF, True
    allowlist = _resolve_allowlist()
    classification = task_type.value.strip().lower()
    allowed = allowlist.get(classification)
    if allowed is None:
        # No entry for this classification → unrestricted under any mode.
        return mode, True
    return mode, provider.strip().lower() in allowed


__all__ = [
    "MODE_OFF",
    "MODE_WARN",
    "MODE_STRICT",
    "check_classification_provider",
]
