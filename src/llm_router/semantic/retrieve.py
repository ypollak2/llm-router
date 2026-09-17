"""Find the evidence this task needs, and stop.

Three properties, each of which exists because the obvious implementation gets
it wrong.

SEEDS BEFORE TRAVERSAL. "Where is `post_entry` defined" is an exact lookup.
Expanding the graph around it costs budget and buys nothing, so `hops_used`
stays 0 when the seeds already answer the question. Retrieval that always
traverses is retrieval that always pays.

HIGH-DEGREE NODES ARE PENALISED, NOT CAPPED. A logging helper with thirty
callers is reachable from almost everywhere, so a traversal ranked by
connectivity returns the logger for every query. A cap alone does not fix that
— it just returns a smaller pile of loggers. The penalty is proportional to
inbound degree, so a genuinely central node can still appear when it is the
subject, and cannot crowd out the answer when it is not.

EMPTY IS NOT BROKEN. `status` separates "nothing here" from "could not look",
because they are the same empty list and opposite facts: one means the
repository has nothing to say, the other means we failed to ask. A caller that
conflates them reports the first while experiencing the second.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from llm_router.semantic import store as sstore
from llm_router.semantic.scope import resolve_scope

# Above this many inbound references a node is a utility rather than an answer.
HIGH_DEGREE_THRESHOLD = 20
# And it may hold at most this share of a result set.
MAX_UTILITY_SHARE = 0.25

DEFAULT_LIMIT = 20
# Traversal is OPT-IN. "Where is post_entry defined" is answered by the seed
# lookup, and expanding around it spends budget on neighbours nobody asked
# about. A caller that wants the neighbourhood — an impact question, a
# cross-module failure — asks for it by passing max_hops.
DEFAULT_MAX_HOPS = 0
MAX_EXPANDED_NODES = 100

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PATHISH = re.compile(r"[\w./-]+\.(?:py|pyi|ts|js|go|rs|java)")

# Words that look like identifiers and never are. Without this, "the", "file"
# and "class" seed a traversal from every query.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "what", "where", "which",
    "does", "into", "from", "when", "why", "how", "are", "was", "were", "has",
    "have", "can", "should", "would", "could", "fix", "add", "make", "run",
    "file", "files", "code", "function", "class", "method", "test", "tests",
    "please", "explain", "show", "capital", "portugal",
})


@dataclass(frozen=True)
class RetrievalResult:
    entities: list[sstore.Entity] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    hops_used: int = 0
    # Two values, not three. Emptiness is `entities == []` and is a perfectly
    # good outcome; this field answers the different question of whether the
    # lookup could run at all. Folding "empty" in here was the first draft, and
    # it made an honest "nothing here" indistinguishable from a broken index at
    # exactly the call site that has to tell them apart.
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
    """(identifiers, paths) worth looking up. Order preserved, deduplicated."""
    paths = list(dict.fromkeys(_PATHISH.findall(query)))
    idents = []
    for match in _IDENT.finditer(query):
        word = match.group(0)
        if word.lower() in _STOPWORDS or word in paths:
            continue
        if word not in idents:
            idents.append(word)
    return idents, paths


def _inbound_degree(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM relation WHERE target_name = ? "
        "OR target_name LIKE ?", (name, "%." + name),
    ).fetchone()
    return int(row["n"]) if row else 0


def retrieve(
    query: str,
    root: Path | str | None = None,
    base: Path | None = None,
    limit: int = DEFAULT_LIMIT,
    max_hops: int = DEFAULT_MAX_HOPS,
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
        primary: list[sstore.Entity] = []
        for name in idents:
            try:
                for entity in sstore.find_definitions(name, conn=conn):
                    key = (entity.relative_path, entity.start_line)
                    if key not in seen:
                        seen.add(key)
                        primary.append(entity)
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
                    primary.append(entity)

        if not primary:
            return RetrievalResult(seeds=idents + paths, status="ok",
                                   note="no entity matched the seeds")

        hops = 0
        expanded: list[sstore.Entity] = []
        if max_hops > 0 and len(primary) < limit:
            hops = 1
            expanded = _expand(conn, primary, limit - len(primary))

        ranked = _rank(conn, primary, expanded, limit)
        return RetrievalResult(entities=ranked, seeds=idents + paths,
                               hops_used=hops if expanded else 0, status="ok")
    finally:
        conn.close()


def _expand(
    conn: sqlite3.Connection,
    primary: list[sstore.Entity],
    room: int,
) -> list[sstore.Entity]:
    """One hop: what the seed files import, and who calls the seed symbols."""
    if room <= 0:
        return []
    out: list[sstore.Entity] = []
    seen = {(e.relative_path, e.start_line) for e in primary}
    seed_paths = {e.relative_path for e in primary}

    neighbours = conn.execute(
        "SELECT DISTINCT relative_path, target_name FROM relation "
        "WHERE relative_path IN (%s) AND type IN ('imports','imports_name')"
        % ",".join("?" * len(seed_paths)),
        tuple(seed_paths),
    ).fetchall()[:MAX_EXPANDED_NODES]

    for row in neighbours:
        target = str(row["target_name"]).rsplit(".", 1)[-1]
        for entity in sstore.find_definitions(target, conn=conn):
            key = (entity.relative_path, entity.start_line)
            if key not in seen:
                seen.add(key)
                out.append(entity)

    callers = conn.execute(
        "SELECT DISTINCT relative_path FROM relation WHERE type = 'call_candidate' "
        "AND target_name IN (%s)" % ",".join("?" * len(primary)),
        tuple(e.name for e in primary),
    ).fetchall()[:MAX_EXPANDED_NODES]
    for row in callers:
        rel = row["relative_path"]
        if rel in seed_paths:
            continue
        rows = conn.execute(
            "SELECT * FROM entity WHERE relative_path = ? ORDER BY start_line "
            "LIMIT 3", (rel,),
        ).fetchall()
        for entity in sstore._rows_to_entities(rows):
            key = (entity.relative_path, entity.start_line)
            if key not in seen:
                seen.add(key)
                out.append(entity)
    return out


def _rank(
    conn: sqlite3.Connection,
    primary: list[sstore.Entity],
    expanded: list[sstore.Entity],
    limit: int,
) -> list[sstore.Entity]:
    """Seeds first, then neighbours, with utility nodes held to their share.

    The share cap is applied AFTER ranking rather than as a filter before it,
    so a query genuinely about the utility node still gets it: `log_it` is a
    seed there, and seeds are not subject to the cap.
    """
    degree = {}
    for entity in expanded:
        degree[entity.name] = _inbound_degree(conn, entity.name)

    ordered = list(primary) + sorted(
        expanded, key=lambda e: (degree.get(e.name, 0), e.relative_path))

    primary_keys = {(e.relative_path, e.start_line) for e in primary}
    out: list[sstore.Entity] = []
    utility_used = 0
    for entity in ordered:
        if len(out) >= limit:
            break
        is_seed = (entity.relative_path, entity.start_line) in primary_keys
        if not is_seed and degree.get(entity.name, 0) >= HIGH_DEGREE_THRESHOLD:
            # Against the ACTUAL result size, not against `limit`. Measuring
            # the share against the cap lets a 30-caller logger be half of a
            # two-entity answer while still satisfying "at most a quarter of
            # eight" — which is the flooding this rule exists to stop, passing
            # the rule written to stop it. A utility node earns its place only
            # once there is enough else in the set to keep it proportional.
            if (utility_used + 1) / (len(out) + 1) > MAX_UTILITY_SHARE:
                continue
            utility_used += 1
        out.append(entity)
    return out
