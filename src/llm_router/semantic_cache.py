"""Semantic dedup cache — skip identical-intent LLM calls.

Uses Ollama embeddings to detect when a new prompt is semantically equivalent
to a recently cached one (cosine similarity ≥ 0.95). When a hit is found,
returns the cached response without making an LLM call.

Design:
- Embedding model: ``nomic-embed-text`` via Ollama (768-dim, fast, free).
  Falls back gracefully to no-op when Ollama is unavailable.
- Storage: ``semantic_cache`` table in the existing usage SQLite DB.
- TTL: 24 hours — cached responses are considered stale after one day.
- Threshold: 0.95 cosine similarity (configurable via ``SEMANTIC_CACHE_THRESHOLD``).
- Scope: per task_type — a code prompt never matches a research prompt even
  if the text is identical (different expected response shapes).
- Thread safety: ``aiosqlite`` handles concurrent access via WAL mode.

Only active when ``ollama_base_url`` is set — zero overhead otherwise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_router.types import LLMResponse, TaskType

log = logging.getLogger("llm_router.semantic_cache")

# Default similarity threshold — prompts with cosine similarity ≥ this value
# are considered duplicates and return the cached response.
DEFAULT_THRESHOLD = 0.95

# Cache TTL in seconds — entries older than this are ignored (not deleted).
_TTL_SECONDS = 86_400  # 24 hours

# Maximum entries to compare per lookup (most recent first). Limits scan cost
# while still catching recent duplicates which are the most common case.
_MAX_SCAN = 200

CREATE_SEMANTIC_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS semantic_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    project_scope TEXT NOT NULL DEFAULT '',
    embedding TEXT NOT NULL,
    response_content TEXT NOT NULL,
    response_model TEXT NOT NULL,
    response_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def _project_scope() -> str:
    """CHZ-ST-004: isolate cache entries by the project they were produced in.

    The semantic cache previously had *no* project column, so a query in
    project B could return project A's cached response verbatim (observed at
    similarity=1.000, leaking a secret across projects on the same machine).
    The scope key is a hash of the project root (cwd, or LLM_ROUTER_PROJECT_DIR),
    matching how result_cache.py derives its project hash. Entries only match
    within the same project; the raw path is never stored.
    """
    root = os.environ.get("LLM_ROUTER_PROJECT_DIR") or os.getcwd()
    return hashlib.sha256(root.encode()).hexdigest()[:16]


async def _ensure_project_scope_column(db) -> None:
    """Idempotently add project_scope to a pre-existing (unscoped) table.

    Old DBs created before CHZ-ST-004 lack the column; ``CREATE TABLE IF NOT
    EXISTS`` won't add it. Legacy rows keep project_scope='' and therefore never
    match a real (non-empty) project scope — they simply age out via TTL.
    """
    try:
        cur = await db.execute("PRAGMA table_info(semantic_cache)")
        cols = {row[1] for row in await cur.fetchall()}
        if "project_scope" not in cols:
            await db.execute(
                "ALTER TABLE semantic_cache ADD COLUMN project_scope TEXT NOT NULL DEFAULT ''"
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — migration failure must not break routing
        log.debug("semantic_cache project_scope migration skipped: %s", exc)

CREATE_SEMANTIC_CACHE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_semantic_cache_type_time
ON semantic_cache(task_type, created_at DESC)
"""


def _repair_shared_db_perms(db_path: "Path | None") -> None:
    """D-02: repair unsafe perms on the shared usage.db at open time.

    ``cost.py``'s ``_get_db()`` (off-limits — owned by another cluster)
    already creates the file with mode 0600 on first creation, but does
    not repair an already-existing file with looser perms. This is the
    local, out-of-band repair pass for semantic_cache's use of that same
    file — safe to run repeatedly, no-ops when perms are already correct.

    ``db_path`` may be ``None`` (e.g. a test double for ``get_config()``
    that doesn't define ``llm_router_db_path``) — no-op in that case rather
    than raising, since callers pass ``getattr(config, "llm_router_db_path",
    None)`` precisely to stay safe against minimal config stand-ins.
    """
    if db_path is None:
        return
    try:
        if db_path.exists() and stat.S_IMODE(db_path.stat().st_mode) != 0o600:
            os.chmod(db_path, 0o600)
    except OSError:
        pass


def _persist_ttl_seconds() -> float:
    """Global physical-retention TTL (LLM_ROUTER_PERSIST_TTL_DAYS), in seconds.

    Independent of the semantic-similarity ``_TTL_SECONDS`` above (which
    only bounds how far back `check()` scans for a similarity match). This
    TTL governs unconditional PHYSICAL deletion of semantic_cache rows.
    0 disables purging.
    """
    try:
        from llm_router.config import get_config
        days = float(getattr(get_config(), "llm_router_persist_ttl_days", 30))
    except Exception:
        days = 30.0
    return max(days, 0.0) * 86_400


async def _purge_expired(db) -> int:
    """Physically delete TTL-expired rows from the ``semantic_cache`` table.

    Scoped strictly to ``semantic_cache`` — never touches other tables in
    the shared usage.db (which also holds spend/usage rows owned by
    ``cost.py``). Sets ``PRAGMA secure_delete=ON`` on this connection so
    freed page bytes are zeroed immediately, satisfying raw-byte-grep
    requirements without needing a VACUUM (which risks lock contention with
    concurrent ``cost.py`` writers on the shared file).
    """
    ttl_seconds = _persist_ttl_seconds()
    if ttl_seconds <= 0:
        return 0
    try:
        await db.execute("PRAGMA secure_delete=ON")
        cursor = await db.execute(
            "SELECT COUNT(*) FROM semantic_cache WHERE created_at < datetime('now', ?)",
            (f"-{int(ttl_seconds)} seconds",),
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        if not count:
            return 0
        await db.execute(
            "DELETE FROM semantic_cache WHERE created_at < datetime('now', ?)",
            (f"-{int(ttl_seconds)} seconds",),
        )
        await db.commit()
        log.debug("semantic_cache: purged %d expired row(s) (ttl=%.0fd)", count, ttl_seconds / 86_400)
        return count
    except Exception as exc:
        log.debug("semantic_cache: TTL purge failed: %s", exc)
        return 0


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length float vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _get_embedding(text: str, base_url: str) -> list[float] | None:
    """Fetch an embedding from Ollama's /api/embeddings endpoint.

    Uses the synchronous ``urllib`` (no extra deps) with a short timeout.
    Returns None on any error so callers can treat failure as a cache miss.

    Args:
        text: The text to embed.
        base_url: Ollama base URL, e.g. ``"http://localhost:11434"``.

    Returns:
        Flat list of floats, or None if Ollama is unreachable or returns an error.
    """
    try:
        payload = json.dumps({"model": "nomic-embed-text", "prompt": text}).encode()
        req = urllib.request.Request(
            f"{base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return data.get("embedding")
    except Exception as exc:
        log.debug("Ollama embedding failed: %s", exc)
        return None


def _get_threshold() -> float:
    """Get similarity threshold from env or default."""
    try:
        return float(os.getenv("LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD", str(DEFAULT_THRESHOLD)))
    except ValueError:
        return DEFAULT_THRESHOLD


async def check(
    prompt: str,
    task_type: "TaskType",
    *,
    threshold: float | None = None,
) -> "LLMResponse | None":
    """Check the semantic cache for a recent equivalent prompt.

    Embeds ``prompt`` via Ollama, then scans recent cache entries for the same
    ``task_type`` and returns the cached response if similarity ≥ threshold.

    Args:
        prompt: The user's prompt text.
        task_type: Task type used to scope the cache (code hits never match research hits).
        threshold: Cosine similarity threshold (0–1). Uses
            ``LLM_ROUTER_SEMANTIC_CACHE_THRESHOLD`` env var or 0.95 default.

    Returns:
        A cached ``LLMResponse`` on hit, or ``None`` on miss / Ollama unavailable.
    """
    if threshold is None:
        threshold = _get_threshold()
    from llm_router.config import get_config
    config = get_config()
    if not config.ollama_base_url:
        return None

    embedding = _get_embedding(prompt, config.ollama_base_url)
    if embedding is None:
        return None

    try:
        from llm_router.cost import _get_db
        _repair_shared_db_perms(getattr(config, "llm_router_db_path", None))
        db = await _get_db()
        try:
            await _ensure_project_scope_column(db)
            # Fetch the most recent entries within TTL for this task type AND
            # this project (CHZ-ST-004: never match another project's entries).
            cursor = await db.execute(
                """
                SELECT embedding, response_content, response_model, response_cost_usd
                FROM semantic_cache
                WHERE task_type = ?
                  AND project_scope = ?
                  AND created_at >= datetime('now', ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (task_type.value, _project_scope(), f"-{_TTL_SECONDS} seconds", _MAX_SCAN),
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()
    except Exception as exc:
        log.debug("Semantic cache read failed: %s", exc)
        return None

    best_sim = 0.0
    best_row = None
    for row in rows:
        try:
            cached_emb = json.loads(row[0])
            sim = _cosine_similarity(embedding, cached_emb)
            if sim > best_sim:
                best_sim = sim
                best_row = row
        except Exception:
            continue

    if best_sim >= threshold and best_row is not None:
        from llm_router.types import LLMResponse
        log.info(
            "semantic_cache: HIT (sim=%.3f ≥ %.2f, model=%s)",
            best_sim, threshold, best_row[2],
        )
        return LLMResponse(
            content=best_row[1],
            model=f"cache/{best_row[2]}",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,   # cached — no API cost
            latency_ms=0.0,
            provider="cache",
            cache_hit=True,
            cache_similarity=best_sim,
        )

    log.debug("semantic_cache: MISS (best_sim=%.3f, rows_scanned=%d)", best_sim, len(rows))
    return None


async def store(
    prompt: str,
    task_type: "TaskType",
    response: "LLMResponse",
) -> None:
    """Store a prompt+response pair in the semantic cache.

    Embeds the prompt and persists the embedding alongside the response
    content for future similarity lookups.

    Args:
        prompt: The original user prompt.
        task_type: The task type of this call.
        response: The LLMResponse to cache.
    """
    from llm_router.config import get_config
    config = get_config()
    if not config.ollama_base_url:
        return

    # Don't cache failed or empty responses
    if not response.content or response.provider == "cache":
        return

    embedding = _get_embedding(prompt, config.ollama_base_url)
    if embedding is None:
        return

    # D-01/D-04: redact BEFORE the row touches the shared usage.db. Wrapped
    # locally even though persist_redact() is already safe-failure, so an
    # import failure here can't fall through to persisting raw content.
    try:
        from llm_router.enterprise.redaction import persist_redact
        safe_content = persist_redact(response.content)
    except Exception as exc:
        log.debug("semantic_cache: redaction unavailable, withholding content: %s", exc)
        safe_content = "[REDACTION-FAILED: content withheld]"

    try:
        from llm_router.cost import _get_db
        _repair_shared_db_perms(getattr(config, "llm_router_db_path", None))
        db = await _get_db()
        try:
            await _ensure_project_scope_column(db)
            await db.execute(
                """
                INSERT INTO semantic_cache
                    (task_type, project_scope, embedding, response_content,
                     response_model, response_cost_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_type.value,
                    _project_scope(),
                    json.dumps(embedding),
                    safe_content,
                    response.model,
                    response.cost_usd,
                ),
            )
            await db.commit()
            log.debug("semantic_cache: stored entry for %s", task_type.value)
            # B-02/B-03: physically purge TTL-expired rows on every store,
            # scoped strictly to this table.
            await _purge_expired(db)
        finally:
            await db.close()
    except Exception as exc:
        log.debug("Semantic cache write failed: %s", exc)
