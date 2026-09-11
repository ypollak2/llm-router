"""Structural grounding — does a routed draft cite things that exist?

Lived in `hooks/auto-route.py` until 13.3.0, which made it unreachable to anything
else: the hook's filename is hyphenated, so `llm_router.hooks.auto-route` is not a
valid module path and even its own tests had to load it with
`spec_from_file_location`. The gateway could not import it at all, so the one
capability this project has that no competitor does was locked inside a file only
one caller could reach.

The checks are deliberately narrow, and stay that way. They do not judge whether a
draft is RIGHT — a judge that is wrong is worse than no judge. They settle the
claims that can be settled without asking another model: a cited file either exists
or it does not; a called function is either in the index, the context, the prompt,
or nowhere.

Both fail OPEN. A bug in a guard that silently stops all routing looks exactly like
the regression this project spent a week fixing.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# S2-6. Paths the draft asserts must be traceable to something real. Mirrors
# okf._FILE_PAT: the store restricts itself to verified structure for the same
# reason — a path is checkable, prose is not.
_DRAFT_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(\[])([\w./-]*[\w-]/[\w./-]*\w\.(?:py|ts|tsx|js|jsx|go|rs|java|md|json|toml|ya?ml|sh))\b"
)


def grounding_violations(draft: str, context: str, prompt: str = "") -> list[str]:
    """File paths the draft names that appear in neither its inputs nor the repo.

    Every other item in Stage 2 makes the routed model more willing to answer, and
    S2-5 flips the failure mode: a model with no context refuses, which is safe; a
    model with the WRONG context answers just as fluently about the wrong thing.
    This catches the mechanical version of that — the draft citing a file nobody
    mentioned and that does not exist. It is the shape of the 2026-09-06 failure,
    where routed reviews "listed tests that do not exist".

    Deliberately narrow. It does not judge whether the draft is RIGHT; a judge that
    is wrong is worse than no judge. It checks the one claim that can be settled
    without asking another model.
    """
    if not draft:
        return []
    haystack = f"{context or ''}\n{prompt or ''}"
    out: list[str] = []
    for m in _DRAFT_PATH_RE.finditer(draft):
        path = m.group(1)
        # `a/` and `b/` are git's diff prefixes, not directories. A draft quoting a
        # diff of a file that IS in context was being reported as citing two
        # invented paths, which would have rejected a correct answer.
        if path[:2] in ("a/", "b/"):
            path = path[2:]
        if path in haystack:
            continue
        try:
            # Existing on disk is evidence too: the model may have been shown the
            # file in an earlier turn that has since fallen out of the budget.
            if Path(path).exists():
                continue
        except OSError:
            pass
        if path not in out:
            out.append(path)
    return out


# S5. A CALL, not a word before a bracket. Two requirements, both learned by
# measuring against real model output rather than assumed:
#
#   * the paren must be adjacent. `\s*\(` matched ordinary English — "threads (or
#     processes)" and "keywords (from the prompt)" were both reported as invented
#     functions, a 2-in-3 false-positive rate on real answers.
#   * the name must look like an identifier: an underscore, or internal capitals.
#     A bare lowercase word before a paren is prose far more often than it is code,
#     and a guard that rejects correct answers costs routing silently — the
#     fallthrough is indistinguishable from a model that simply did not answer.
_DRAFT_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+|[a-z][A-Z][A-Za-z0-9_]*))\(")

# The second form, added after the call shape missed a live fabrication. Probing
# the real MCP tool produced: retrieval correct (`src/llm_router/okf.py` is right,
# and only OKF could have supplied it), then two invented callers —
# `write_concept` and `write_concept_from_mcp`, neither of which exists. Both were
# written as fenced prose rather than calls, so the paren requirement never saw
# them.
#
# A fenced identifier is an explicit code claim: nobody writes `write_concept` in
# backticks meaning an English word. Same identifier-shape requirement as the call
# form, plus two exclusions that matter in practice:
#
#   * anything containing "/" or "." is a path or a dotted attribute — the path
#     check owns those, and double-reporting one mistake as two is noise;
#   * ALL_CAPS is an env var or a module constant, which is the most common
#     backticked identifier-shaped token in this project's own writing. Flagging
#     `LLM_ROUTER_PROJECT_ROOT` would reject correct answers about configuration.
_DRAFT_FENCED_SYMBOL_RE = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+|[a-z][A-Z][A-Za-z0-9_]*))`"
)

# Words that appear with parens in ordinary writing and in shell, and would
# otherwise be read as invented functions. `test()`, `build()` and `run()` are
# English before they are identifiers.
_SYMBOL_NOISE = frozenset({
    "and", "build", "check", "def", "deploy", "elif", "for", "fix", "function",
    "get", "here", "http", "https", "if", "install", "int", "json", "list", "log",
    "not", "note", "open", "print", "return", "run", "set", "sudo", "test", "the",
    "this", "try", "update", "use", "using", "while", "with", "yaml",
})


def known_symbols() -> set[str]:
    """Every symbol name the OKF index knows for this project.

    Only meaningful once `okf index` has run. An empty set means "nothing is
    checkable", never "everything is invented" — see _symbol_violations.
    """
    out: set[str] = set()
    try:
        from llm_router import okf as _okf

        for concept in _okf._get_bundle():
            for sym in concept.extra.get("key_symbols") or []:
                out.add(str(sym))
    except Exception:  # noqa: BLE001
        return set()
    return out


def symbol_violations(draft: str, context: str, prompt: str = "") -> list[str]:
    """Functions/classes the draft calls that exist nowhere checkable.

    S2-6 validates paths; this validates the other half of the verified structure
    `okf index` now holds. A draft sounds specific when it cites
    `reconcile_invoice_totals()`, and specificity is exactly what makes a fabricated
    answer persuasive.

    Deliberately NOT a fix for the U7-class failure, which invented prose and named
    no symbols at all — nothing structural can catch that, which is why S2-5b fixed
    it at the gate. This covers the case in between.

    A symbol in the index, the context, or the prompt is grounded. Only a name in
    none of them is a violation, and only when the index is populated: unknown is
    not invented, and unknown must never reject.
    """
    if not draft:
        return []
    # Only meaningful when the answer is ABOUT the indexed project. The index knows
    # this repository's symbols and nothing else, so absence from it is not evidence
    # of non-existence — measured on real output, a mutex/semaphore answer naming
    # `pthread_mutex_lock` and `sem_init` was reported as inventing them, and a
    # question about configuration was rejected for naming `load_dotenv`. Those are
    # real functions; they are simply not in this repo.
    #
    # Retrieval having fired is the signal that the model was answering about this
    # codebase. With no injected knowledge, the draft is general-purpose and the
    # index has no standing to judge the names in it.
    if "<knowledge_context>" not in (context or ""):
        return []
    try:
        known = known_symbols()
    except Exception:  # noqa: BLE001
        return []
    if not known:
        return []  # nothing indexed → nothing checkable
    haystack = f"{context or ''}\n{prompt or ''}"
    out: list[str] = []
    names = [m.group(1) for m in _DRAFT_SYMBOL_RE.finditer(draft)]
    names += [
        m.group(1) for m in _DRAFT_FENCED_SYMBOL_RE.finditer(draft)
        if not m.group(1).isupper()   # env var / constant, not a function
    ]
    for name in names:
        if name.lower() in _SYMBOL_NOISE or name in known or name in haystack:
            continue
        if name not in out:
            out.append(name)
    return out


def draft_is_relayable(draft: str, context: str, prompt: str = "") -> bool:
    """Whether a DIRECT draft may be shown, or should fall through to Claude.

    Fail-open on any internal error: a bug in the guard must not silently stop all
    routing, which would look exactly like the regression this branch is fixing.
    `LLM_ROUTER_GROUNDING_CHECK=off` disables it.
    """
    if os.environ.get("LLM_ROUTER_GROUNDING_CHECK", "on").strip().lower() in (
        "0", "off", "false", "no"
    ):
        return True
    try:
        if grounding_violations(draft, context, prompt):
            return False
        # Symbol checking has its own switch: path checking is cheap and certain,
        # while this depends on the OKF index being present and reasonably fresh.
        if os.environ.get("LLM_ROUTER_SYMBOL_GROUNDING", "on").strip().lower() in (
            "0", "off", "false", "no"
        ):
            return True
        return not symbol_violations(draft, context, prompt)
    except Exception:  # noqa: BLE001
        return True

# S2-5. A gated prompt is routable when its reference can be RESOLVED, not merely
# when it names something. Minimums below are what separates "there is an exchange
# to resolve against" from "there is a scrap that invites a confident wrong guess";
# without context a model refuses, which is safe, so thin context is strictly worse
# than none.
