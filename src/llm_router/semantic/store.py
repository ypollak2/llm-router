"""The derived structural index: SQLite, per project, rebuildable.

DERIVED is the load-bearing word. Nothing here is a source of truth — delete
the file and it rebuilds from the repository. That is what licenses the
aggression in `replace_file`: an entry whose bytes no longer hash to what was
parsed is not evidence, it is a memory of evidence, and presenting the second
as the first is the failure this layer exists to avoid.

WHERE IT LIVES

`okf.project_knowledge_dir(root, base) / "semantic" / "index.sqlite"`, derived
from the existing helper rather than a new path constant. Two reasons: the
per-project slug is already solved there, and every OKF test already isolates
itself by passing `base`, so this inherits that isolation instead of needing
its own sandbox story.

CONVENTIONS

WAL, `CREATE TABLE IF NOT EXISTS`, and a `meta` table holding schema and
extractor versions rather than a second manifest file — `cost.py:636` set that
pattern and there is no reason for a second one.

One deviation, stated: `cost.py` is `aiosqlite`. Indexing is a bulk
transactional job on the CLI side, and an async connection buys nothing for a
loop that is CPU-bound in `ast.parse`. This is sync `sqlite3`; async callers
reach it through `run_in_executor`, which is what `okf.py` already does for its
writes. One connection style in this module, not two.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from llm_router.sqlite_wal import enable_wal

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per indexed file. `content_hash` is the hash of the exact bytes that
-- were parsed, which is what makes staleness detectable rather than assumed.
CREATE TABLE IF NOT EXISTS file (
    relative_path TEXT PRIMARY KEY,
    language      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    parse_status  TEXT NOT NULL,
    indexed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path  TEXT NOT NULL,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    signature      TEXT NOT NULL DEFAULT '',
    source_hash    TEXT NOT NULL,
    FOREIGN KEY (relative_path) REFERENCES file(relative_path)
);
CREATE INDEX IF NOT EXISTS entity_by_name ON entity(name);
CREATE INDEX IF NOT EXISTS entity_by_path ON entity(relative_path);

-- `target` may be unresolved: a name this extractor could not bind to an
-- entity. Recorded as a candidate rather than dropped, because "we saw a
-- reference we could not resolve" and "there is no reference" are different
-- answers and only one of them is honest about aliases and dynamic dispatch.
CREATE TABLE IF NOT EXISTS relation (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path     TEXT NOT NULL,
    type              TEXT NOT NULL,
    source_name       TEXT NOT NULL,
    target_name       TEXT NOT NULL,
    resolution_status TEXT NOT NULL,
    line              INTEGER NOT NULL,
    FOREIGN KEY (relative_path) REFERENCES file(relative_path)
);
CREATE INDEX IF NOT EXISTS relation_by_target ON relation(target_name);
CREATE INDEX IF NOT EXISTS relation_by_path ON relation(relative_path);
"""


@dataclass(frozen=True)
class Entity:
    relative_path: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str
    source_hash: str


@dataclass(frozen=True)
class Relation:
    relative_path: str
    type: str
    source_name: str
    target_name: str
    resolution_status: str
    line: int


def index_path(root: Path | str | None = None, base: Path | None = None) -> Path:
    from llm_router import okf

    kwargs = {"root": root}
    if base is not None:
        kwargs["base"] = base
    return okf.project_knowledge_dir(**kwargs) / "semantic" / "index.sqlite"


def connect(root: Path | str | None = None, base: Path | None = None) -> sqlite3.Connection:
    path = index_path(root, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # M-06 / the sqlite_wal adoption gap. `busy_timeout` governs how long the
    # journal_mode PRAGMA itself waits for its exclusive lock, so setting it
    # AFTER is the one ordering that leaves that statement on the 5s default.
    # The PRAGMA also reports failure by RETURNING the mode in effect rather
    # than raising -- lose the cold-start race and you silently proceed in
    # rollback-journal mode. `enable_wal` handles both and was adopted by only
    # 3 of 9 sites.
    enable_wal(conn, label="semantic_store")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


# ── writes ───────────────────────────────────────────────────────────────────

def replace_file(
    conn: sqlite3.Connection,
    relative_path: str,
    language: str,
    file_hash: str,
    parse_status: str,
    entities: list[Entity],
    relations: list[Relation],
) -> None:
    """Replace everything this file owns, in one transaction.

    Delete-then-insert rather than merge. A merge keeps whatever the previous
    parse believed and the current one did not mention, which is exactly a
    renamed or deleted symbol — the case that turns an index into a source of
    confidently wrong answers.

    A file with `parse_status != "parsed"` contributes no entities. Its row
    stays so the status is reportable: "this file is unavailable for this
    snapshot" is an answer, and silently falling back to the last version that
    parsed is not.
    """
    from datetime import datetime, timezone

    with conn:
        conn.execute("DELETE FROM entity WHERE relative_path = ?", (relative_path,))
        conn.execute("DELETE FROM relation WHERE relative_path = ?", (relative_path,))
        conn.execute(
            "INSERT INTO file(relative_path, language, content_hash, parse_status, "
            "indexed_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(relative_path) DO UPDATE SET language=excluded.language, "
            "content_hash=excluded.content_hash, parse_status=excluded.parse_status, "
            "indexed_at=excluded.indexed_at",
            (relative_path, language, file_hash, parse_status,
             datetime.now(timezone.utc).isoformat()),
        )
        if parse_status != "parsed":
            return
        conn.executemany(
            "INSERT INTO entity(relative_path, kind, name, qualified_name, "
            "start_line, end_line, signature, source_hash) VALUES(?,?,?,?,?,?,?,?)",
            [(e.relative_path, e.kind, e.name, e.qualified_name, e.start_line,
              e.end_line, e.signature, e.source_hash) for e in entities],
        )
        conn.executemany(
            "INSERT INTO relation(relative_path, type, source_name, target_name, "
            "resolution_status, line) VALUES(?,?,?,?,?,?)",
            [(r.relative_path, r.type, r.source_name, r.target_name,
              r.resolution_status, r.line) for r in relations],
        )


def forget_file(conn: sqlite3.Connection, relative_path: str) -> None:
    """A deleted file leaves nothing behind."""
    with conn:
        conn.execute("DELETE FROM entity WHERE relative_path = ?", (relative_path,))
        conn.execute("DELETE FROM relation WHERE relative_path = ?", (relative_path,))
        conn.execute("DELETE FROM file WHERE relative_path = ?", (relative_path,))


def known_files(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["relative_path"]: r["content_hash"]
            for r in conn.execute("SELECT relative_path, content_hash FROM file")}


# ── reads ────────────────────────────────────────────────────────────────────

def _rows_to_entities(rows) -> list[Entity]:
    return [Entity(
        relative_path=r["relative_path"], kind=r["kind"], name=r["name"],
        qualified_name=r["qualified_name"], start_line=r["start_line"],
        end_line=r["end_line"], signature=r["signature"],
        source_hash=r["source_hash"],
    ) for r in rows]


def find_definitions(
    name: str,
    root: Path | str | None = None,
    base: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Entity]:
    owned = conn is None
    conn = conn or connect(root, base)
    try:
        rows = conn.execute(
            "SELECT * FROM entity WHERE name = ? OR qualified_name = ? "
            "ORDER BY relative_path, start_line", (name, name),
        ).fetchall()
        return _rows_to_entities(rows)
    finally:
        if owned:
            conn.close()


def find_importers(
    module: str,
    root: Path | str | None = None,
    base: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Relation]:
    owned = conn is None
    conn = conn or connect(root, base)
    try:
        rows = conn.execute(
            "SELECT * FROM relation WHERE type = 'imports' AND "
            "(target_name = ? OR target_name LIKE ? ) ORDER BY relative_path",
            (module, module + ".%"),
        ).fetchall()
        return [Relation(
            relative_path=r["relative_path"], type=r["type"],
            source_name=r["source_name"], target_name=r["target_name"],
            resolution_status=r["resolution_status"], line=r["line"],
        ) for r in rows]
    finally:
        if owned:
            conn.close()


def file_status(
    relative_path: str,
    root: Path | str | None = None,
    base: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """"parsed", "parse_failed", or "unknown" for a file never indexed."""
    owned = conn is None
    conn = conn or connect(root, base)
    try:
        row = conn.execute(
            "SELECT parse_status FROM file WHERE relative_path = ?",
            (relative_path,),
        ).fetchone()
        return row["parse_status"] if row else "unknown"
    finally:
        if owned:
            conn.close()


def evidence_is_current(
    entity: Entity,
    root: Path | str | None = None,
    base: Path | None = None,
) -> bool:
    """Do the bytes on disk still hash to what this entity was parsed from.

    The question a caller has to be able to ask before quoting an index entry
    as fact. Answering it from the index alone would be circular, so it reads
    the file.
    """
    from llm_router.semantic.scope import resolve_scope

    try:
        path = resolve_scope(root) / entity.relative_path
        return content_hash(path.read_bytes()) == entity.source_hash
    except OSError:
        return False
