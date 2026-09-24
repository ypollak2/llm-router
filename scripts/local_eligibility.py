"""Which prompts may the LOCAL agent own? A pre-registered rule (Q15 task 6b).

Fitted 2026-09-24 on the plan+hard suites (local qwen3-coder:30b passed 3/3 on
6 of 13 tasks; failed every question, every cause-hunt, 2 of 3 plan tasks).
Pre-registered BEFORE the held-out run on the easy+brutal suites: its per-task
predictions are committed alongside it, so the held-out score cannot be
tuned after the fact.

Eligible = an EDIT that names its target and states the change. Excluded:
questions, cause-hunting, open-ended plan implementation.
"""
from __future__ import annotations

import re

_QUESTION = re.compile(r"\?|^\s*(which|what|how many|how much|does|is|are|why|name)\b", re.I | re.M)
_CAUSE = re.compile(r"\b(cause|fails?|failing|bug|wrong|broken|breaks?|does not|doesn't|"
                    r"slower|should|contract|docstring says|rule)\b", re.I)
_PLAN = re.compile(r"\bimplement the plan\b", re.I)
_TARGET = re.compile(r"`[^`]+`|\b[\w./-]+\.py\b|\b[a-z]+_[a-z_]+\b|\b[A-Z][A-Za-z]+\.\w+|\b\w+\(\)")
_CHANGE = re.compile(r"^\s*(change|make|add|rename|remove|give|extract|set|replace)\b", re.I | re.M)


def local_eligible(prompt: str) -> tuple[bool, str]:
    """(eligible, reason). The reason names the rule that decided."""
    if _PLAN.search(prompt):
        return False, "open-ended plan"
    if _QUESTION.search(prompt):
        return False, "question"
    if _CAUSE.search(prompt):
        return False, "cause-hunting"
    if not _TARGET.search(prompt):
        return False, "no named target"
    if not _CHANGE.search(prompt):
        return False, "no stated change"
    return True, "named edit"
