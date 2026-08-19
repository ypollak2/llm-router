"""Canonical 'is this prompt context-dependent?' signal.

Single source of truth shared by the UserPromptSubmit advisory (auto-route.py)
and the enforcement hook (enforce-route.py), so the two can't diverge: any prompt
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


def is_context_dependent(prompt: str) -> bool:
    """True when the prompt references the user's local code/files/history/state."""
    p = prompt or ""
    if _CONTEXT_DEP_RE.search(p):
        return True
    words = p.split()
    return len(words) <= 12 and bool(_DEICTIC_RE.search(p) or _ANAPHORA_RE.search(p))
