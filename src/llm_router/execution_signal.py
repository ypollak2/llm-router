"""Canonical 'does completing this prompt need LOCAL EXECUTION / repo ops?' signal.

Sibling of ``operational_signal`` — and deliberately SEPARATE from it. Broadening
``detect_operational`` to catch execution work would over-route ordinary prose
(that predicate is tuned for *delegate-worthy* work: a change verb AND an
objective-verification cue). This predicate answers a narrower, different
question: does the request ask us to **run local commands or perform repo/VCS
operations** (run tests, git rebase, apply a migration, deploy) whose completion
a *text-only* door structurally cannot perform?

Why it exists (GAP-ENF-1 / INV-ROUTE-006): execution work that has no
verification cue — "run the migration and commit" — does not trip
``detect_operational``, so under enforcement it was routed to the text-only
completion door (``llm``) and then blocked the moment it reached Bash: a
dead-end. Enforcement uses THIS signal to name the tool-capable door
(``llm_act``) for such work instead.

Like its sibling it is HIGH-PRECISION (a false positive hijacks an ordinary
prompt into a heavy tool loop): it fires only on an imperative execution/VCS
action against a concrete command/repo object, and never on an
explanatory/interrogative prompt or a prose deliverable. It reuses
``operational_signal``'s explanatory-lead and content-object guards so the two
predicates can never drift apart on what counts as "not real work".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Reuse the sibling's guards so the two signals share one definition of
# "explanatory/interrogative" and "prose deliverable" — never drift.
from llm_router.operational_signal import _CONTENT_OBJECT_RE, _EXPLANATORY_LEAD_RE

# An imperative execution / version-control action. These are verbs that, in a
# repo context, denote RUNNING something or mutating repo/VCS state — not merely
# authoring text (which is `write`/`implement` → llm_code, handled elsewhere).
_EXEC_VERB_RE = re.compile(
    r"\b(run|execute|deploy|redeploy|rebase|bisect|cherry-pick|revert|stash|"
    r"checkout|push|pull|commit|merge|apply|install|reinstall|compile|rebuild|"
    r"regenerate|restart|relaunch|launch|rollback|roll\s+back|provision|"
    r"bootstrap|migrate|benchmark|profile)\b",
    re.IGNORECASE,
)

# A concrete command / repo / tooling object that anchors the verb to real local
# execution. DELIBERATELY concrete — generic verbs ("run", "apply") only fire
# when paired with one of these, so "run a marathon" / "apply for a job" stay
# silent. Tool and VCS names (git, pytest, docker) are strong anchors on their own.
_EXEC_OBJECT_RE = re.compile(
    r"\b(?:"
    r"test\s+suite|tests?|suite|migrations?|scripts?|the\s+build|pipeline|"
    r"ci|cd|deploy(?:ment)?|lock\s?file|dependenc(?:y|ies)|branch(?:es)?|"
    r"commits?|pull\s+request|pr|merge|hooks?|docker|container|k8s|kubernetes|"
    r"terraform|helm|cluster|server|service|makefile|npm|yarn|pnpm|pip|poetry|"
    r"uv|pytest|tox|git|schema|database|db|package|binary|image|the\s+repo|"
    r"the\s+app|the\s+code|linter?|formatter"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExecutionSignal:
    """Result of execution-need detection, with the matched axes for transparency
    logging (an enforced route must record WHY it fired)."""
    fires: bool
    verb: str | None = None
    obj: str | None = None
    reason: str = ""


def detect_execution(prompt: str) -> ExecutionSignal:
    """High-precision local-execution / repo-ops detection. Fires iff an execution
    verb AND a concrete command/repo object are present and the prompt is not
    leading-explanatory or a prose deliverable."""
    p = prompt or ""
    if _EXPLANATORY_LEAD_RE.search(p):
        return ExecutionSignal(False, reason="explanatory/interrogative lead")
    if _CONTENT_OBJECT_RE.search(p):
        return ExecutionSignal(False, reason="prose/content deliverable, not execution")
    verb_m = _EXEC_VERB_RE.search(p)
    obj_m = _EXEC_OBJECT_RE.search(p)
    if verb_m and obj_m:
        verb, obj = verb_m.group(0), obj_m.group(0)
        return ExecutionSignal(
            True, verb=verb, obj=obj,
            reason=f"execution verb {verb!r} + repo/command object {obj!r}",
        )
    return ExecutionSignal(
        False,
        verb=verb_m.group(0) if verb_m else None,
        obj=obj_m.group(0) if obj_m else None,
        reason="missing execution verb or repo/command object",
    )


def needs_execution(prompt: str) -> bool:
    """True when completing the prompt needs local command execution / repo ops."""
    return detect_execution(prompt).fires
