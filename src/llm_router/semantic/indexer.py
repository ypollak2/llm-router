"""Bring the index up to date with the repository, and nothing else.

Two rules, and everything here follows from them.

HASH, THEN PARSE THE BYTES YOU HASHED. Not "hash, then open the file again":
between those two reads someone can save, and the result is an index entry
whose `source_hash` describes bytes that were never parsed. That entry then
passes every freshness check while being wrong, which is worse than being
obviously stale. One read, and the hash and the parse come from the same
`bytes` object.

A FILE THAT DOES NOT PARSE HAS NO STRUCTURE. Not "keep the last structure that
worked". The whole point of a derived index is that it can be discarded, so
discarding is cheap and a stale answer is the only expensive outcome.
`parse_status="parse_failed"` is reportable; silently answering from the
previous version is not.

SCOPE

`git ls-files`, like `okf.index_project`, so the index inherits the repo's own
`.gitignore` rather than burying real code under `node_modules`. Every path is
resolved and confirmed to be inside the root before it is read, because a
symlink is a path out of the project and the store is per-project for a reason.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from llm_router.semantic import store as sstore
from llm_router.semantic.extractors import python as py_extractor
from llm_router.semantic.scope import resolve_scope

# Python only, deliberately. A second language is a second adapter, added when
# something needs it — not a speculative abstraction over one implementation.
_LANGUAGES = {".py": ("python", py_extractor)}

MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class IndexResult:
    root: str
    files_parsed: int
    files_skipped: int
    files_failed: int
    files_forgotten: int
    entities: int

    @property
    def total_seen(self) -> int:
        return self.files_parsed + self.files_skipped + self.files_failed


def _tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _safe_path(root: Path, rel: str) -> Path | None:
    """The file, if it is genuinely inside the project.

    `git ls-files` lists the link, not its target, so a symlinked directory is
    a supported way to walk straight out of the root. Resolve and check.
    """
    try:
        target = (root / rel).resolve()
        target.relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    return target


def index(
    root: Path | str | None = None,
    base: Path | None = None,
    limit: int = 20_000,
) -> IndexResult:
    """Update the index for *root*. Cheap when nothing changed."""
    scope = resolve_scope(root)
    conn = sstore.connect(scope, base)
    try:
        previous = sstore.known_files(conn)
        seen: set[str] = set()
        parsed = skipped = failed = entity_count = 0

        for rel in _tracked_files(scope)[:limit]:
            suffix = Path(rel).suffix
            if suffix not in _LANGUAGES:
                continue
            language, extractor = _LANGUAGES[suffix]
            path = _safe_path(scope, rel)
            if path is None:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                # ONE read. The hash and the parse describe the same bytes.
                raw = path.read_bytes()
            except OSError:
                continue

            seen.add(rel)
            file_hash = sstore.content_hash(raw)
            if previous.get(rel) == file_hash:
                skipped += 1
                continue

            extracted = extractor.extract(
                raw.decode("utf-8", errors="replace"), rel, file_hash)
            if extracted is None:
                sstore.replace_file(conn, rel, language, file_hash,
                                    "parse_failed", [], [])
                failed += 1
                continue

            entities, relations = extracted
            sstore.replace_file(conn, rel, language, file_hash,
                                "parsed", entities, relations)
            parsed += 1
            entity_count += len(entities)

        # Anything the index remembers and the repository no longer lists is
        # gone: deleted, renamed, or newly ignored. All three mean the same
        # thing to a reader asking what the code looks like now.
        forgotten = 0
        for rel in previous:
            if rel not in seen:
                sstore.forget_file(conn, rel)
                forgotten += 1

        return IndexResult(
            root=str(scope), files_parsed=parsed, files_skipped=skipped,
            files_failed=failed, files_forgotten=forgotten, entities=entity_count,
        )
    finally:
        conn.close()


@dataclass(frozen=True)
class Link:
    """How much of one experience record's key still exists in the code.

    Three states have to stay distinguishable, and a bare list of entities
    collapses two of them into "empty":

        entities            the named symbols, found where the record said
        resolved_paths      files that exist but whose symbols were not named
                            or were not found
        missing_paths       files the record names that are no longer there

    A record citing a path with no symbol is perfectly well resolved — most
    decisions are about a module, not a function. A record whose file has been
    deleted is not, and a caller needs to say so out loud rather than showing
    an empty section that looks like "nothing to report".
    """

    record: object
    entities: list[sstore.Entity]
    resolved_paths: list[str]
    missing_paths: list[str]

    @property
    def is_resolved(self) -> bool:
        return bool(self.entities or self.resolved_paths)


def link_experience(
    store,
    root: Path | str | None = None,
    base: Path | None = None,
) -> list[Link]:
    """Resolve each experience record's (path, symbol) key against the index.

    Experience records key on paths and symbol names because they are written
    before any index exists — a foreign key into a table nobody has built is
    how two layers ship as two databases that never meet.

    Nothing is dropped. A record whose code is gone comes back with its paths
    in `missing_paths`, because a lesson about deleted code is frequently a
    lesson about why it was deleted, and "this refers to something that no
    longer exists" is exactly what the caller needs in order to say so.
    """
    scope = resolve_scope(root)
    conn = sstore.connect(scope, base)
    try:
        known = set(sstore.known_files(conn))
        out: list[Link] = []
        for record in store.all():
            wanted_paths = list(getattr(record, "affected_paths", []) or [])
            wanted_symbols = set(getattr(record, "affected_symbols", []) or [])
            if not wanted_paths and not wanted_symbols:
                continue

            matched: list[sstore.Entity] = []
            for symbol in sorted(wanted_symbols):
                for entity in sstore.find_definitions(symbol, conn=conn):
                    # A symbol only counts for this record if it is in a file
                    # the record actually names — otherwise a lesson about
                    # `reserve` in budget.py attaches to every `reserve` in the
                    # repository.
                    if not wanted_paths or entity.relative_path in wanted_paths:
                        matched.append(entity)

            # `known` holds indexed files only, so a path this indexer does not
            # cover (a .md, a shell script) is checked on disk rather than
            # reported missing for the wrong reason.
            resolved, missing = [], []
            for rel in wanted_paths:
                if rel in known or (scope / rel).exists():
                    resolved.append(rel)
                else:
                    missing.append(rel)
            out.append(Link(record=record, entities=matched,
                            resolved_paths=resolved, missing_paths=missing))
        return out
    finally:
        conn.close()
