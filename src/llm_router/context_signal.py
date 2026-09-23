"""An 'is this prompt context-dependent?' signal — NOT the one in production.

Intended as the single source of truth for the UserPromptSubmit advisory and the
enforcement hook. Verified 2026-09-15: neither imports it. The advisory uses its
own ``_is_context_dependent`` (hooks/auto-route.py), and enforce-route.py records
that its redundant recheck was deliberately removed. Every reference outside this
module is a test or mutation configuration.

Left in place because it declares a public API, but tests against it do not
verify the live hook predicate, and changing it changes no routing behaviour.

The design intent it describes is still sound: any prompt
the advisory flags as context-dependent is the same one enforcement exempts from
hard-blocking. A context-dependent prompt references the user's local
code/files/history/state — things a stateless routed model cannot see, so forcing
a route (blocking native tools) only traps the user behind a directive no cheap
model can satisfy.

Errs toward True: a false positive only costs a skipped draft (the agent still
answers from real context); a false negative is the exact failure mode we're
closing. Correctness outranks the token saving.
"""
from __future__ import annotations

import re

_CONTEXT_DEP_RE = re.compile(
    r"\b(this|that|these|those|the|our|my|your)\s+(\w+\s+){0,2}"
    r"(code\s?base|code|repo(sitory)?|project|file|module|package|library|"
    r"function|class|method|test|suite|spec|script|bug|error|stack\s?trace|"
    r"diff|pr|branch|commit|readme|config|directory|folder|swarm|agent|hook|"
    r"session|dashboard|app|server|service|component|feature|build|parser|"
    r"endpoint|route|api|database|db|schema|query|migration|deployment|pipeline|"
    r"workflow|setup|environment|env|dependency|dependencies|import|variable|"
    r"output|log|crash|failure|exception|stacktrace|codebase)s?\b"
    r"|\b(run|start|startup|launch|serve|deploy|install|build|compile|lint|"
    r"debug|fix|refactor|optimi[sz]e|rename|migrate|rerun|restart|reproduce|"
    r"profile|redeploy|rollback"
    r"|stop|kill|cancel|terminate|abort|halt|remove|delete|purge|prune|"
    r"resume|pause|retry|revert|undo|clean\s?up)\b"
    r"|previous\s+session|prior\s+(session|conversation|turn|reply|message)"
    r"|earlier\s+(you|we|i)\b|last\s+(reply|message|session|turn|answer)"
    r"|you\s+(said|mentioned|wrote)|we\s+(discussed|talked|were|built)"
    r"|as\s+(above|before|discussed)|continue\s+(the|from|with|where)"
    r"|\b(loophole|llm_router)\b"
    r"|[\w./-]+\.(py|js|ts|tsx|jsx|go|rs|md|json|toml|ya?ml|sh|txt|cfg|ini)\b"
    r"|(~|\./|\.\./|/Users/|/home/)[\w./-]+",
    re.IGNORECASE,
)

# Bare deictic pronouns in a short prompt ("run IT", "what does THIS do").
_DEICTIC_RE = re.compile(r"\b(it|this|that|these|those|here|them)\b", re.IGNORECASE)

# Definite anaphora pointing at a set from a prior turn ("the rest", "the others").
_ANAPHORA_RE = re.compile(r"\bthe\s+(rest|remaining|others?|ones?)\b", re.IGNORECASE)



# S3b. A RELATIVE `that`/`which` is not a deixis.
#
# `_DEICTIC_RE` treats a bare "that" as pointing at something in the user's
# local state ("fix THAT bug"). But "that" is also a relative pronoun
# introducing a clause — "a regex THAT validates emails" — where it points at
# nothing outside the sentence.
#
# Measured on a labelled corpus of 24 prompts, before this rule:
#
#     context-dependent correctly detected  11/12
#     FALSE POSITIVES on stateless prompts   5/12   (42%)
#
# and every false positive had the same shape: `a <noun> that <verb>`.
#
#     "write a regex that validates an email address"
#     "write a function that reverses a string"
#     "write a SQL query that counts rows by day"
#     "describe an algorithm that sorts in O(n log n)"
#     "name a language that has pattern matching"
#
# This mattered beyond classification. `auto-route.py` suppresses enforcement
# for a context-dependent prompt (it would otherwise hold a tool no routed
# model can use), so a false positive here silently turns routing OFF for a
# prompt a routed model could answer perfectly well.
#
# `enforce-route.py` removed its OWN call to this function for exactly this
# reason — "over-fired on incidental deictics" — which fixed one consumer and
# left the detector, and every other consumer, unchanged.
#
# The distinction is grammatical, not semantic: a relative `that` is followed
# by a VERB, a demonstrative one is not. Verb-like is approximated by an -s /
# -ed ending or a small auxiliary set — deliberately a heuristic, and a
# conservative one: it only ever REMOVES a deixis signal, so a miss leaves the
# previous (over-firing) behaviour rather than inventing a new one.
_RELATIVE_RE = re.compile(r"\b(that|which)\s+(\w+)\b", re.IGNORECASE)

_AUXILIARIES = frozenset({
    "is", "are", "was", "were", "has", "have", "had", "can", "could", "will",
    "would", "should", "must", "does", "do", "did", "may", "might",
})


def _looks_verbal(word: str) -> bool:
    """-s / -ed / auxiliary. Deliberately NOT -ing.

    "-ing" was in the first version and cost recall: "that THING works" has a
    noun ending in -ing, so the demonstrative was masked and a genuinely
    context-dependent prompt stopped being detected. The same trap holds for
    "string", "nothing", "something", "setting", "warning", "meeting".

    Measured on the 26-prompt corpus in
    `tests/test_s3b_relative_that_is_not_a_deixis.py`:

        -s -ed -ing   recall 11/13   false positives 0/13
        -s -ed        recall 12/13   false positives 0/13   <- chosen

    A relative clause in a prompt of this kind almost always takes -s ("that
    validates") or an auxiliary ("that has"); a bare "-ing" form needs an
    auxiliary before it anyway ("that IS validating"), which the auxiliary set
    already catches.
    """
    w = word.lower()
    return w in _AUXILIARIES or w.endswith(("s", "ed"))


def _mask_relative_pronouns(prompt: str) -> str:
    """Blank out `that`/`which` used as relative pronouns, keeping offsets."""
    out = prompt
    for m in _RELATIVE_RE.finditer(prompt):
        if _looks_verbal(m.group(2)):
            a, b = m.span(1)
            out = out[:a] + "_" * (b - a) + out[b:]
    return out


def is_context_dependent(prompt: str) -> bool:
    """True when the prompt references the user's local code/files/history/state."""
    p = prompt or ""
    if _CONTEXT_DEP_RE.search(p):
        return True
    words = p.split()
    # S3b: a relative `that`/`which` points inside the sentence, not at the
    # user's state. Masked before the deixis check; see _mask_relative_pronouns.
    masked = _mask_relative_pronouns(p)
    return len(words) <= 12 and bool(
        _DEICTIC_RE.search(masked) or _ANAPHORA_RE.search(masked)
    )
