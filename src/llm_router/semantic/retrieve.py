"""Find the evidence this task needs, and stop.

SEEDS, AND NOTHING ELSE

Look up what the query names — the identifiers and the paths — and return it.
There is no graph traversal here, and there used to be.

WHY THE TRAVERSAL IS GONE

It was built, measured, and earned nothing. Arms C (seeds only) and D (seeds
plus two hops) were run over 60 questions on this repository and gave
**identical answers to every single one**, while D cost 0.2s more per query.
The research document set the graph's adoption gate at "beats the strongest
corrected non-graph baseline by at least 3 points" and said what to do
otherwise:

    If graph traversal cannot beat corrected hybrid retrieval under these
    controls, keep the simpler retrieval system. That is a useful outcome, not
    a reason to redesign the benchmark until the graph wins.

So it was deleted rather than left switched off, and this comment is the reason
it will not be rebuilt by accident. `docs/measurements/2026-09-18-semantic-arms.md`
has the run; `git log` has the code if a later task set ever justifies it.

Two things went with it and are worth knowing about if it comes back. A
high-degree penalty: a logging helper with thirty callers is reachable from
almost everywhere, so a traversal ranked by connectivity returns the logger for
every query, and a cap alone does not fix that — it returns a smaller pile of
loggers. And a proportional share rule, because measuring that cap against the
limit rather than the actual result size let one utility node be half of a
two-entity answer while still satisfying "at most a quarter of eight".

EMPTY IS NOT BROKEN

`status` answers whether the lookup could run, not whether it found anything.
Emptiness is `entities == []` and is a perfectly good outcome. Folding the two
together was the first draft and it made an honest "nothing here"
indistinguishable from a broken index at exactly the call site that has to tell
them apart.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from llm_router.semantic import store as sstore
from llm_router.semantic.scope import resolve_scope

DEFAULT_LIMIT = 20

_PATHISH = re.compile(r"[\w./-]+\.(?:py|pyi|ts|js|go|rs|java)")

# A seed has to LOOK like code. Prose does not.
#
# This used to be `[A-Za-z_][A-Za-z0-9_]{2,}` behind a stopword list, which
# meant any word of three or more characters seeded a symbol lookup. The
# `concept` stratum — docstring questions with the identifier removed — showed
# what that does: every question retrieved the same five irrelevant files,
# because this repository contains entities named `project`, `implements`,
# `routing` and `override`.
#
# Not a benchmark artifact. Source retrieval is on by default, so "how does the
# routing override work?" was retrieving whatever happened to be named
# `routing`, and handing it over with source spans and content hashes attached
# — the shape of evidence, holding a guess.
#
# A longer stopword list cannot fix this; English is too large and every word
# in it is somebody's variable name. So the test is shape, not membership:
#
#   `backticked`     the user said explicitly that this is code
#   snake_case       an underscore between word characters
#   CamelCase        two or more capitals, so `Ledger` is prose and
#                    `OKFConcept` is not
#   dotted.name      a qualified reference
#
# A bare lowercase word is prose until proven otherwise. Someone who means a
# symbol can always backtick it, and that is a smaller cost than retrieving
# confident nonsense for every sentence containing a common noun.
_QUOTED = re.compile(r"[`'\"]([A-Za-z_][A-Za-z0-9_.]*)[`'\"]")
_SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*\b")
_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")


@dataclass(frozen=True)
class RetrievalResult:
    entities: list[sstore.Entity] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    # Two values, not three. Emptiness is `entities == []`; this field answers
    # the different question of whether the lookup could run at all.
    status: str = "ok"          # ok | unavailable
    note: str = ""

    @property
    def paths(self) -> list[str]:
        seen, out = set(), []
        for e in self.entities:
            if e.relative_path not in seen:
                seen.add(e.relative_path)
                out.append(e.relative_path)
        return out


def seeds_from(query: str) -> tuple[list[str], list[str]]:
    """(identifiers, paths) worth looking up. Order preserved, deduplicated.

    Returning an empty identifier list is a normal, frequent and correct
    outcome: most sentences do not name any code. Retrieval then finds nothing,
    the pack renders as nothing, and the prompt goes out untouched — which is
    the right behaviour for a question that was never about a symbol.
    """
    paths = list(dict.fromkeys(_PATHISH.findall(query)))

    # Strip the paths before scanning, so `src/llm_router/okf.py` does not also
    # yield `okf.py` as a dotted identifier.
    scannable = query
    for path in paths:
        scannable = scannable.replace(path, " ")

    idents: list[str] = []
    for pattern in (_QUOTED, _DOTTED, _SNAKE, _CAMEL):
        for match in pattern.finditer(scannable):
            token = match.group(1) if pattern is _QUOTED else match.group(0)
            # A dotted reference is looked up by its last segment, which is what
            # the index stores as a name; the qualified form is kept too.
            for candidate in ({token, token.rsplit(".", 1)[-1]}
                              if "." in token else {token}):
                if candidate and candidate not in idents:
                    idents.append(candidate)
    return idents, paths


def retrieve(
    query: str,
    root: Path | str | None = None,
    base: Path | None = None,
    limit: int = DEFAULT_LIMIT,
) -> RetrievalResult:
    scope = resolve_scope(root)
    idents, paths = seeds_from(query)

    # Open the index even with no seeds. A corrupt or unreadable index has to
    # report itself whatever the query was: returning early here would answer
    # "nothing relevant" while the store was unreadable.
    try:
        conn = sstore.connect(scope, base)
        conn.execute("SELECT COUNT(*) FROM entity").fetchone()
    except sqlite3.Error as exc:
        return RetrievalResult(status="unavailable", note=str(exc))

    if not idents and not paths:
        conn.close()
        return RetrievalResult(seeds=[], status="ok",
                               note="no identifier or path in the query")

    try:
        seen: set[tuple[str, int]] = set()
        found: list[sstore.Entity] = []

        for name in idents:
            try:
                for entity in sstore.find_definitions(name, conn=conn):
                    key = (entity.relative_path, entity.start_line)
                    if key not in seen:
                        seen.add(key)
                        found.append(entity)
            except sqlite3.DatabaseError as exc:
                return RetrievalResult(status="unavailable", note=str(exc))

        # Everything defined in a file the query named outright.
        for rel in paths:
            try:
                rows = conn.execute(
                    "SELECT * FROM entity WHERE relative_path = ? "
                    "ORDER BY start_line", (rel,),
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                return RetrievalResult(status="unavailable", note=str(exc))
            for entity in sstore._rows_to_entities(rows):
                key = (entity.relative_path, entity.start_line)
                if key not in seen:
                    seen.add(key)
                    found.append(entity)

        if not found:
            return RetrievalResult(seeds=idents + paths, status="ok",
                                   note="no entity matched the seeds")

        # Named identifiers first, then the contents of named files. Both are
        # things the query actually asked for, so there is nothing to rank
        # against them and nothing to penalise.
        return RetrievalResult(entities=found[:limit], seeds=idents + paths,
                               status="ok")
    finally:
        conn.close()
