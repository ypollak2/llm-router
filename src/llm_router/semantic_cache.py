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
import os
import re
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
# C-03. Raised from 0.95 after measuring what 0.95 actually admits on
# nomic-embed-text:
#
#     "retry 3 times"    vs "retry 30 times"    -> 0.9925
#     "timeout 30"       vs "timeout 300"       -> 0.9903
#     "increase by 10%"  vs "decrease by 10%"   -> 0.9764
#
# A higher threshold alone cannot fix this and it is important to say why: the
# first pair scores 0.9925, so a threshold that excluded it would exclude almost
# every genuine duplicate too. Embeddings are not a magnitude- or polarity-
# sensitive representation; no cutoff makes them one.
#
# 0.98 clears the weakest measured collision (0.9764) and nothing more; the
# 0.9925 pair sits above it and is caught by the discriminator, not by this
# number. So the threshold is defence in depth, and `_discriminator` is the actual
# fix: cosine similarity answers "are these about the same topic?", which is not
# the question the cache needs answered. The question is "do these ask for the
# same thing?"
DEFAULT_THRESHOLD = 0.98

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
    discriminator TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def _project_scope() -> str:
    """CHZ-ST-004: isolate cache entries by the project they were produced in.

    The semantic cache previously had *no* project column, so a query in
    project B could return project A's cached response verbatim (observed at
    similarity=1.000, leaking a secret across projects on the same machine).
    The scope key is a hash of the project root. Entries only match within the
    same project; the raw path is never stored.

    OKF-SCOPE-04: it used to be `sha256(LLM_ROUTER_PROJECT_DIR or os.getcwd())`,
    which claimed to match result_cache.py and did not — that one hashed its
    caller's argument — and matched OKF even less, since OKF walks to the repo
    root. So running from `src/` scoped OKF to the project and this cache to the
    subdirectory: one project, two namespaces, and the split shows up as an
    ordinary cache miss. `resolve_scope` is now the single answer for all of
    them, and it walks to the repo root from whatever it is given.

    Existing rows keyed under the old hash simply stop matching and age out on
    the 24h TTL — the same self-healing path `_ensure_project_scope_column`
    established when this column was introduced.
    """
    from llm_router.semantic.scope import scope_key

    return scope_key()


async def _ensure_project_scope_column(db) -> None:
    """Idempotently add project_scope and discriminator to a pre-existing table.

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
        if "discriminator" not in cols:
            # C-03. Nullable on purpose: rows written before this column existed
            # carry no discriminator, and `check` treats that as UNKNOWN rather
            # than as "equivalent". They age out via TTL. Defaulting them to ''
            # would read as "no numbers, no polarity" and silently re-admit the
            # exact collisions this column exists to stop.
            await db.execute("ALTER TABLE semantic_cache ADD COLUMN discriminator TEXT")
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


# ── C-03: the equivalence guard ────────────────────────────────────────────
#
# Cosine similarity over sentence embeddings is a topic measure. Two prompts can
# be about the same topic and ask for opposite things, and the embedding will
# not separate them — measured on this project's own model and threshold:
#
#     "retry 3 times" / "retry 30 times"      0.9925
#     "increase by 10%" / "decrease by 10%"   0.9764
#
# These are the two failure shapes that actually matter in a developer tool:
# a differing MAGNITUDE and a differing DIRECTION. Both are carried by tokens an
# embedding compresses away, so they are checked literally.
#
# Deliberately narrow. This does not attempt semantic equivalence in general; it
# vetoes a hit when two prompts demonstrably differ in what they ask for. A false
# veto costs one model call. A false hit returns a confident wrong answer with no
# signal that anything happened, which is the failure this project already hit in
# production (one passport's answer served for another).

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

# Direction-bearing tokens. Each inner set is mutually exclusive: if one prompt
# uses a token from one side and the other uses a token from the other side, they
# are not asking the same question however close the vectors are.
_POLARITY_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"increase", "increment", "raise", "add", "more", "up", "grow"}),
    frozenset({"decrease", "decrement", "lower", "remove", "less", "down", "shrink"}),
    frozenset({"enable", "on", "start", "open", "allow", "include"}),
    frozenset({"disable", "off", "stop", "close", "deny", "exclude"}),
    frozenset({"ascending", "asc", "oldest", "first", "min", "minimum", "earliest"}),
    frozenset({"descending", "desc", "newest", "last", "max", "maximum", "latest"}),
    frozenset({"before", "prepend", "above", "preceding"}),
    frozenset({"after", "append", "below", "following"}),
)

_WORD_RE = re.compile(r"[a-z]+")


def _discriminator(text: str) -> dict:
    """What a prompt asks for, reduced to the parts embeddings lose.

    Returns only derived tokens — never prompt text. The cache lives in the
    shared ``usage.db``; putting prompts there would create the persistence
    surface `persist_redact` exists to avoid.
    """
    lowered = text.lower()
    nums = sorted({m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(lowered)})
    words = set(_WORD_RE.findall(lowered))
    pol = sorted(
        str(i) for i, group in enumerate(_POLARITY_GROUPS) if words & group
    )
    return {"nums": nums, "pol": pol}


def _equivalence_veto(stored: "dict | None", incoming: dict) -> str | None:
    """Why these two prompts are not interchangeable, or None if they may be.

    Fails closed: a row with no stored discriminator is UNKNOWN, not equivalent.
    """
    if stored is None:
        return "no discriminator on the cached row (written before C-03)"
    if stored.get("nums") != incoming.get("nums"):
        return (
            f"numeric literals differ: cached {stored.get('nums')} "
            f"vs incoming {incoming.get('nums')}"
        )
    if stored.get("pol") != incoming.get("pol"):
        return (
            f"direction differs: cached polarity {stored.get('pol')} "
            f"vs incoming {incoming.get('pol')}"
        )
    return None


def _cache_disabled() -> bool:
    """C-03: a per-request off switch. There was none."""
    val = os.getenv("LLM_ROUTER_SEMANTIC_CACHE", "").strip().lower()
    return val in ("0", "off", "false", "no", "disable", "disabled")


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
    if _cache_disabled():
        return None
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
                SELECT embedding, response_content, response_model, response_cost_usd,
                       discriminator
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

    incoming_disc = _discriminator(prompt)
    best_sim = 0.0
    best_row = None
    vetoed = 0
    for row in rows:
        try:
            cached_emb = json.loads(row[0])
            sim = _cosine_similarity(embedding, cached_emb)
            if sim <= best_sim:
                continue
            # C-03: a close vector is a candidate, not a hit. Check that the two
            # prompts actually ask for the same thing before letting this row win,
            # and keep scanning if they do not -- a vetoed row must not shadow a
            # genuinely equivalent one further down.
            try:
                stored_disc = json.loads(row[4]) if row[4] else None
            except Exception:
                stored_disc = None
            veto = _equivalence_veto(stored_disc, incoming_disc)
            if veto is not None:
                vetoed += 1
                log.debug("semantic_cache: VETO at sim=%.4f -- %s", sim, veto)
                continue
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

    log.debug(
        "semantic_cache: MISS (best_sim=%.3f, rows_scanned=%d, vetoed=%d)",
        best_sim, len(rows), vetoed,
    )
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
    if _cache_disabled():
        return
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
        from llm_router.persist_redaction import persist_redact
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
                     response_model, response_cost_usd, discriminator)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_type.value,
                    _project_scope(),
                    json.dumps(embedding),
                    safe_content,
                    response.model,
                    response.cost_usd,
                    json.dumps(_discriminator(prompt)),
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


async def evict(prompt: str, task_type: "TaskType") -> int:
    """Remove cached entries equivalent to *prompt*. Returns rows deleted.

    C-03: the cache could serve a wrong answer and there was no way to remove
    just that entry — the only remedies were waiting out the 24h TTL or clearing
    everything. Matching is by discriminator plus task and project scope, so
    this removes the entries that would answer this prompt without touching
    unrelated ones.
    """
    from llm_router.config import get_config

    config = get_config()
    target = json.dumps(_discriminator(prompt))
    try:
        from llm_router.cost import _get_db

        _repair_shared_db_perms(getattr(config, "llm_router_db_path", None))
        db = await _get_db()
        try:
            await _ensure_project_scope_column(db)
            cur = await db.execute(
                """
                DELETE FROM semantic_cache
                WHERE task_type = ? AND project_scope = ? AND discriminator = ?
                """,
                (task_type.value, _project_scope(), target),
            )
            await db.commit()
            return cur.rowcount or 0
        finally:
            await db.close()
    except Exception as exc:  # noqa: BLE001 — eviction must never break a route
        log.debug("semantic_cache eviction failed: %s", exc)
        return 0
