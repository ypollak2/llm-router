"""Result cache — stores routed responses for BM25 retrieval on follow-ups.

Every successful route stores its Q&A pair in SQLite with FTS5 full-text
indexing. Follow-up questions query this cache via BM25 ranking to inject
relevant prior context into new prompts — giving cheap models memory of
what they've already answered.

Two cache levels:
- **User-level** (~/.llm-router/result_cache.db): knowledge Q&A, research
- **Project-level** (~/.llm-router/projects/<hash>/result_cache.db): code answers

Cache invalidation:
- TTL: 24h for code, 7d for research, 30d for general knowledge
- Dedup: SHA-256 of prompt prevents duplicate storage
- Manual: llm_cache_clear clears both levels
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from llm_router.token_budget import estimate_tokens

log = logging.getLogger("llm_router.result_cache")

_ROUTER_DIR = Path.home() / ".llm-router"

# TTL per task type (seconds)
_TTL: dict[str, int] = {
    "code": 86_400,        # 24 hours
    "analyze": 86_400 * 3, # 3 days
    "research": 86_400 * 7,  # 7 days
    "query": 86_400 * 30,   # 30 days
    "generate": 86_400 * 7,  # 7 days (creative content, less reusable)
}
_DEFAULT_TTL = 86_400 * 7  # 7 days

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    task_type TEXT NOT NULL,
    complexity TEXT NOT NULL,
    model_used TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    user_prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    project_dir TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_results_hash ON results(prompt_hash);
CREATE INDEX IF NOT EXISTS idx_results_time ON results(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_results_project ON results(project_dir, task_type);
"""

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS results_fts USING fts5(
    user_prompt, response,
    content='results',
    content_rowid='id'
);
"""

_CREATE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS results_ai AFTER INSERT ON results BEGIN
    INSERT INTO results_fts(rowid, user_prompt, response)
    VALUES (new.id, new.user_prompt, new.response);
END;

CREATE TRIGGER IF NOT EXISTS results_ad AFTER DELETE ON results BEGIN
    INSERT INTO results_fts(results_fts, rowid, user_prompt, response)
    VALUES ('delete', old.id, old.user_prompt, old.response);
END;
"""


@dataclass(frozen=True)
class CachedResult:
    """A single cached Q&A pair."""

    user_prompt: str
    response: str
    task_type: str
    model_used: str
    timestamp: float
    bm25_score: float = 0.0


def _prompt_hash(prompt: str) -> str:
    """SHA-256 hash of prompt for dedup."""
    return hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:16]


def _get_db_path(project_dir: str | None, task_type: str) -> Path:
    """Determine which cache DB to use based on task and project."""
    # Code tasks use project-level cache
    if task_type in ("code", "analyze") and project_dir:
        project_hash = hashlib.sha256(project_dir.encode()).hexdigest()[:12]
        path = _ROUTER_DIR / "projects" / project_hash / "result_cache.db"
    else:
        # Knowledge tasks use user-level cache
        path = _ROUTER_DIR / "result_cache.db"
    return path


def _secure_perms(path: Path) -> None:
    """Ensure *path* is mode 0600, repairing any looser existing perms.

    D-02: sensitive DB/JSONL files must never be world/group-readable, on
    creation OR on an already-existing file opened by an older version.
    """
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_db(db_path: Path) -> sqlite3.Connection:
    """Open (and initialize if needed) the cache database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        db_path.touch(mode=0o600)
    else:
        _secure_perms(db_path)
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    # secure_delete: overwrite freed page bytes with zeros immediately on
    # DELETE/UPDATE, so TTL purges (below) physically remove secret bytes
    # from the file rather than leaving them in unallocated pages. This is
    # a per-connection pragma, must be re-set on every open.
    conn.execute("PRAGMA secure_delete=ON")
    conn.executescript(_CREATE_TABLES)
    conn.executescript(_CREATE_FTS)
    conn.executescript(_CREATE_TRIGGERS)
    # WAL/SHM sidecar files can carry sensitive bytes too — keep them 0600.
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            _secure_perms(sidecar)
    return conn


def _persist_ttl_seconds() -> float:
    """Global physical-retention TTL (LLM_ROUTER_PERSIST_TTL_DAYS), in seconds.

    Independent of the per-task-type `_TTL` dict above, which only governs
    read-time freshness filtering (dedup / BM25 search). This TTL governs
    unconditional PHYSICAL deletion regardless of task type. 0 disables
    purging.
    """
    try:
        from llm_router.config import get_config
        days = float(getattr(get_config(), "llm_router_persist_ttl_days", 30))
    except Exception:
        days = 30.0
    return max(days, 0.0) * 86_400


def _purge_expired_conn(conn: sqlite3.Connection) -> int:
    """Physically delete rows older than the persistence TTL on *conn*.

    Relies on the existing `results_ad` AFTER-DELETE trigger to keep
    `results_fts` in sync per deleted row (fires once per affected row even
    for a multi-row DELETE), and on `PRAGMA secure_delete=ON` (set in
    `_ensure_db`) to zero the freed page bytes immediately. Returns the
    number of rows purged.
    """
    ttl_seconds = _persist_ttl_seconds()
    if ttl_seconds <= 0:
        return 0
    cutoff = time.time() - ttl_seconds
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM results WHERE timestamp < ?", (cutoff,)
        ).fetchone()
        count = row[0] if row else 0
        if not count:
            return 0
        conn.execute("DELETE FROM results WHERE timestamp < ?", (cutoff,))
        conn.commit()
        try:
            # Defense-in-depth: fully rewrite the file so no freed page can
            # retain stale bytes even if secure_delete behaves differently
            # across SQLite builds.
            conn.execute("VACUUM")
        except Exception:
            pass
        log.debug("Purged %d expired result(s) (ttl=%.0fd)", count, ttl_seconds / 86_400)
        return count
    except Exception as e:
        log.debug("TTL purge failed: %s", e)
        return 0


def purge_expired(db_path: Path) -> int:
    """Public helper: physically delete TTL-expired rows from *db_path*.

    Opens (or no-ops if the DB doesn't exist yet) and runs the same purge
    `store_result` performs automatically, useful for tests and any
    external maintenance/compaction pass.
    """
    if not db_path.exists():
        return 0
    try:
        conn = _ensure_db(db_path)
        try:
            return _purge_expired_conn(conn)
        finally:
            conn.close()
    except Exception as e:
        log.debug("TTL purge failed: %s", e)
        return 0


def store_result(
    user_prompt: str,
    response: str,
    task_type: str,
    complexity: str,
    model_used: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    project_dir: str | None = None,
) -> None:
    """Store a routed result in the cache.

    Skips storage if:
    - Response is empty or too short (<20 chars)
    - Exact duplicate exists (same prompt_hash within TTL)
    """
    if not response or len(response.strip()) < 20:
        return

    phash = _prompt_hash(user_prompt)
    db_path = _get_db_path(project_dir, task_type)

    try:
        conn = _ensure_db(db_path)
        try:
            # Check for dedup (same hash within TTL)
            ttl = _TTL.get(task_type, _DEFAULT_TTL)
            cutoff = time.time() - ttl
            existing = conn.execute(
                "SELECT id FROM results WHERE prompt_hash = ? AND timestamp > ?",
                (phash, cutoff),
            ).fetchone()

            if existing:
                log.debug("Skipping store — duplicate within TTL (hash=%s)", phash[:8])
                return

            # D-01/D-04: redact BEFORE the row (and its FTS shadow entry)
            # ever touches disk. phash above is intentionally computed from
            # the ORIGINAL prompt for dedup stability — only the persisted
            # text is scrubbed. Safe-failure: persist_redact() never raises
            # and never returns raw content on internal error — but wrap it
            # anyway so an import/config failure here can't fall through to
            # persisting the untouched raw strings.
            try:
                from llm_router.persist_redaction import persist_redact
                safe_prompt = persist_redact(user_prompt)
                safe_response = persist_redact(response)
            except Exception as e:
                log.debug("Redaction unavailable, withholding raw content: %s", e)
                safe_prompt = "[REDACTION-FAILED: content withheld]"
                safe_response = "[REDACTION-FAILED: content withheld]"

            conn.execute(
                """INSERT INTO results
                   (timestamp, task_type, complexity, model_used, prompt_hash,
                    user_prompt, response, tokens_in, tokens_out, cost_usd, project_dir)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    time.time(), task_type, complexity, model_used, phash,
                    safe_prompt, safe_response, tokens_in, tokens_out, cost_usd,
                    project_dir or "",
                ),
            )
            conn.commit()
            log.debug("Stored result (task=%s, model=%s, hash=%s)", task_type, model_used, phash[:8])
            # B-02/B-03: physically purge TTL-expired rows on every store,
            # reusing the already-open connection.
            _purge_expired_conn(conn)
        finally:
            conn.close()
    except Exception as e:
        log.debug("Failed to store result: %s", e)


def search_results(
    query: str,
    task_type: str | None = None,
    project_dir: str | None = None,
    budget_tokens: int = 2000,
    limit: int = 5,
) -> list[CachedResult]:
    """Search cached results using BM25 full-text search.

    Returns relevant prior Q&A pairs within the token budget.

    Args:
        query: The current user prompt to find relevant context for.
        task_type: Filter to same task type (optional).
        project_dir: Filter to same project (for code tasks).
        budget_tokens: Maximum tokens to return across all results.
        limit: Maximum number of results to consider.

    Returns:
        List of CachedResult ordered by relevance (best first).
    """
    # Determine which DB to search
    effective_task = task_type or "query"
    db_path = _get_db_path(project_dir, effective_task)

    if not db_path.exists():
        return []

    try:
        conn = _ensure_db(db_path)
        try:
            # Build BM25 search query
            # Escape FTS5 special characters
            safe_query = _sanitize_fts_query(query)
            if not safe_query:
                return []

            # BM25 search with TTL filter
            ttl = _TTL.get(effective_task, _DEFAULT_TTL)
            cutoff = time.time() - ttl

            sql = """
                SELECT r.user_prompt, r.response, r.task_type, r.model_used,
                       r.timestamp, bm25(results_fts) as score
                FROM results_fts
                JOIN results r ON r.id = results_fts.rowid
                WHERE results_fts MATCH ?
                  AND r.timestamp > ?
            """
            params: list = [safe_query, cutoff]

            if task_type:
                sql += " AND r.task_type = ?"
                params.append(task_type)

            if project_dir and effective_task in ("code", "analyze"):
                sql += " AND r.project_dir = ?"
                params.append(project_dir)

            sql += " ORDER BY score LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()

            # Budget-aware assembly
            results = []
            tokens_used = 0
            for row in rows:
                result = CachedResult(
                    user_prompt=row[0],
                    response=row[1],
                    task_type=row[2],
                    model_used=row[3],
                    timestamp=row[4],
                    bm25_score=row[5],
                )
                # Estimate tokens for this result
                entry_tokens = estimate_tokens(result.user_prompt) + estimate_tokens(result.response)
                if tokens_used + entry_tokens > budget_tokens:
                    break
                results.append(result)
                tokens_used += entry_tokens

            return results
        finally:
            conn.close()
    except Exception as e:
        log.debug("BM25 search failed: %s", e)
        return []


def check_dedup(user_prompt: str, task_type: str, project_dir: str | None = None) -> CachedResult | None:
    """Check if an exact duplicate exists in cache (instant cache hit).

    Returns the cached result if the same prompt (by hash) was answered
    within the TTL window.
    """
    phash = _prompt_hash(user_prompt)
    db_path = _get_db_path(project_dir, task_type)

    if not db_path.exists():
        return None

    try:
        conn = _ensure_db(db_path)
        try:
            ttl = _TTL.get(task_type, _DEFAULT_TTL)
            cutoff = time.time() - ttl

            row = conn.execute(
                """SELECT user_prompt, response, task_type, model_used, timestamp
                   FROM results
                   WHERE prompt_hash = ? AND timestamp > ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (phash, cutoff),
            ).fetchone()

            if row:
                return CachedResult(
                    user_prompt=row[0],
                    response=row[1],
                    task_type=row[2],
                    model_used=row[3],
                    timestamp=row[4],
                )
            return None
        finally:
            conn.close()
    except Exception as e:
        log.debug("Dedup check failed: %s", e)
        return None


def format_context(results: list[CachedResult], max_tokens: int = 2000) -> str:
    """Format cached results as context for injection into a prompt.

    Returns a formatted string with prior Q&A pairs, respecting token budget.
    """
    if not results:
        return ""

    parts = []
    tokens_used = 0
    header = "[Relevant prior answers]\n"
    tokens_used += estimate_tokens(header)

    for r in results:
        entry = f"Q: {r.user_prompt}\nA: {r.response}\n\n"
        entry_tokens = estimate_tokens(entry)
        if tokens_used + entry_tokens > max_tokens:
            break
        parts.append(entry)
        tokens_used += entry_tokens

    if not parts:
        return ""

    return header + "".join(parts).rstrip()


def clear_cache(project_dir: str | None = None) -> int:
    """Clear all cached results. Returns number of entries deleted."""
    deleted = 0

    # Clear user-level cache
    user_db = _ROUTER_DIR / "result_cache.db"
    if user_db.exists():
        try:
            conn = sqlite3.connect(str(user_db), timeout=5)
            deleted += conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            conn.execute("DELETE FROM results")
            conn.execute("INSERT INTO results_fts(results_fts) VALUES('rebuild')")
            conn.commit()
            conn.close()
        except Exception:
            pass

    # Clear project-level cache if specified
    if project_dir:
        project_hash = hashlib.sha256(project_dir.encode()).hexdigest()[:12]
        proj_db = _ROUTER_DIR / "projects" / project_hash / "result_cache.db"
        if proj_db.exists():
            try:
                conn = sqlite3.connect(str(proj_db), timeout=5)
                deleted += conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
                conn.execute("DELETE FROM results")
                conn.execute("INSERT INTO results_fts(results_fts) VALUES('rebuild')")
                conn.commit()
                conn.close()
            except Exception:
                pass

    return deleted


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a user query for safe FTS5 MATCH usage.

    FTS5 has special syntax (AND, OR, NOT, quotes, etc.). We extract
    meaningful words and join with OR for broad matching.
    """
    # Remove special FTS5 characters
    cleaned = query.replace('"', "").replace("'", "")
    for char in "(){}[]<>*+-!@#$%^&=|\\/:;,.?":
        cleaned = cleaned.replace(char, " ")

    # Extract words (3+ chars for meaningful matching)
    words = [w for w in cleaned.split() if len(w) >= 3 and w.isalnum()]

    if not words:
        return ""

    # Use OR for broad matching, limit to 10 terms for performance
    return " OR ".join(words[:10])
