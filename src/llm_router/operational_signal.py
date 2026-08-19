"""Canonical 'does this prompt need agentic tool-execution?' signal.

Drives the classifier-selectable ``llm_delegate`` route. Because that route is
ENFORCED (hard-routed), this predicate is deliberately HIGH-PRECISION — the
opposite bias from ``context_signal`` — a false positive would hijack an ordinary
prompt into a heavy multi-step delegation. So it fires only when BOTH signals are
present: a code-mutating verb AND an objective-verification demand, and never on
an explanatory/interrogative prompt (which describes rather than requests work).

Shared single source of truth: ``enforce-route.py`` uses this to override an
enforced route to ``llm_delegate`` without touching the classifier in
``auto-route.py``. Mirrors ``context_signal.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A code-mutating action verb (imperative). Deliberately excludes read-only verbs
# (explain/describe/run-alone) — those don't need a delegated agent loop.
_CHANGE_VERB_RE = re.compile(
    r"\b(implement|fix|refactor|migrate|build|create|write|add|rename|delete|"
    r"remove|optimi[sz]e|debug|patch|upgrade|integrate|wire|scaffold|port|"
    r"generate|update|replace|extend|rewrite)\b",
    re.IGNORECASE,
)

# An objective-verification demand — the discriminator between a one-shot
# completion (llm_code) and work that must be RUN and checked (llm_delegate).
# DELIBERATELY TIGHT: bare words like "pass"/"test"/"ensure"/"green"/"coverage"
# are far too common in ordinary prose (mountain pass, personality test, ensure
# examples, button green, insurance coverage) and would hijack normal prompts
# into an enforced delegation. Only unambiguous software-verification phrases fire.
_VERIFY_CUE_RE = re.compile(
    r"\b(?:"
    r"make\s+it\s+pass|so\s+(?:it|the\s+tests?)\s+passes?|"
    r"tests?\s+(?:still\s+|all\s+)?pass(?:es|ing)?|passing\s+tests?|"
    r"unit\s+test|integration\s+test|regression\s+test|test\s+suite|"
    r"test\s+that\s+(?:checks?|asserts?|verifies)|suite\s+(?:still\s+)?pass(?:es)?|"
    r"ci\s+(?:is\s+)?green|build\s+(?:is\s+)?green|"
    r"lint(?:er)?\s+(?:pass(?:es)?|clean|is\s+clean)|type[-\s]?check(?:s|ing)?|"
    r"code\s+coverage|test\s+coverage|exit\s+0|returns?\s+0|"
    r"assertion\s+(?:pass|hold)|verify\s+(?:it|that|the\s+\w+)"
    r")\b",
    re.IGNORECASE,
)

# Prose/content deliverables: even with a change verb + a stray cue, a request to
# produce writing is not an operational task. Short-circuits to non-operational.
_CONTENT_OBJECT_RE = re.compile(
    r"\b(blog\s+post|article|essay|poem|summary|explanation|paragraph|sentence|"
    r"story|report|guide|advice|email|caption|rubric|itinerary|checklist|"
    r"tutorial|readme\s+section|documentation\s+for|"
    # Educational / prose deliverables that happen to contain software-verification
    # words (audit-found false positives — these are content, not code work):
    r"test\s+plan|quiz|exam|worksheet|curriculum|lesson(\s+plan)?|exercise|"
    r"course|presentation|slide\s+deck|memo|newsletter|scenario)\b",
    re.IGNORECASE,
)

# Leading explanatory/interrogative intent → the user wants understanding, not
# execution. Anchored to the START so an operational prompt that merely contains
# "how" mid-sentence is not falsely excluded.
_EXPLANATORY_LEAD_RE = re.compile(
    r"^\s*(explain|describe|summari[sz]e?|why\b|what\s|how\s|should\s+i|"
    r"compare\b|when\s+should|which\b|is\s+it|does\s+it|can\s+you\s+explain)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OperationalSignal:
    """Result of operational detection, with the matched axes for transparency
    logging (an enforced route must record WHY it fired)."""
    fires: bool
    verb: str | None = None
    cue: str | None = None
    reason: str = ""


def detect_operational(prompt: str) -> OperationalSignal:
    """High-precision operational-intent detection. Fires iff a change verb AND a
    verification cue are present and the prompt is not leading-explanatory."""
    p = prompt or ""
    if _EXPLANATORY_LEAD_RE.search(p):
        return OperationalSignal(False, reason="explanatory/interrogative lead")
    if _CONTENT_OBJECT_RE.search(p):
        return OperationalSignal(False, reason="prose/content deliverable, not operational")
    verb_m = _CHANGE_VERB_RE.search(p)
    cue_m = _VERIFY_CUE_RE.search(p)
    if verb_m and cue_m:
        verb, cue = verb_m.group(0), cue_m.group(0)
        return OperationalSignal(
            True, verb=verb, cue=cue,
            reason=f"change verb {verb!r} + verification cue {cue!r}",
        )
    return OperationalSignal(
        False,
        verb=verb_m.group(0) if verb_m else None,
        cue=cue_m.group(0) if cue_m else None,
        reason="missing change verb or verification cue",
    )


def is_operational(prompt: str) -> bool:
    """True when the prompt needs agentic tool-execution + objective verification."""
    return detect_operational(prompt).fires
