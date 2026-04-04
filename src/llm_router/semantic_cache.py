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

import json
import logging
import math
import urllib.error
import urllib.request
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
    embedding TEXT NOT NULL,
    response_content TEXT NOT NULL,
    response_model TEXT NOT NULL,
    response_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_SEMANTIC_CACHE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_semantic_cache_type_time
ON semantic_cache(task_type, created_at DESC)
"""


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


async def check(
    prompt: str,
    task_type: "TaskType",
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> "LLMResponse | None":
    """Check the semantic cache for a recent equivalent prompt.

    Embeds ``prompt`` via Ollama, then scans recent cache entries for the same
    ``task_type`` and returns the cached response if similarity ≥ threshold.

    Args:
        prompt: The user's prompt text.
        task_type: Task type used to scope the cache (code hits never match research hits).
        threshold: Cosine similarity threshold (0–1). Default 0.95.

    Returns:
        A cached ``LLMResponse`` on hit, or ``None`` on miss / Ollama unavailable.
    """
    from llm_router.config import get_config
    config = get_config()
    if not config.ollama_base_url:
        return None

    embedding = _get_embedding(prompt, config.ollama_base_url)
    if embedding is None:
        return None

    try:
        from llm_router.cost import _get_db
        db = await _get_db()
        try:
            # Fetch the most recent entries within TTL for this task type
            cursor = await db.execute(
                """
                SELECT embedding, response_content, response_model, response_cost_usd
                FROM semantic_cache
                WHERE task_type = ?
                  AND created_at >= datetime('now', ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (task_type.value, f"-{_TTL_SECONDS} seconds", _MAX_SCAN),
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

    try:
        from llm_router.cost import _get_db
        db = await _get_db()
        try:
            await db.execute(
                """
                INSERT INTO semantic_cache
                    (task_type, embedding, response_content, response_model, response_cost_usd)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_type.value,
                    json.dumps(embedding),
                    response.content,
                    response.model,
                    response.cost_usd,
                ),
            )
            await db.commit()
            log.debug("semantic_cache: stored entry for %s", task_type.value)
        finally:
            await db.close()
    except Exception as exc:
        log.debug("Semantic cache write failed: %s", exc)
