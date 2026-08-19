"""Regression: CHZ-ST-004 — cross-project leak via the unscoped semantic cache.

The `semantic_cache` table had no project column, so a query in project B could
return project A's cached response verbatim — observed at similarity=1.000,
leaking a secret across projects on the same machine.

These tests drive the real `check`/`store` with a *deterministic* embedding (so
similarity is exactly 1.0 — the worst case) and prove:
  1. a byte-identical prompt in a DIFFERENT project scope does NOT hit A's entry;
  2. the same prompt in the SAME project scope still hits (cache still works).
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from llm_router import semantic_cache
from llm_router.semantic_cache import CREATE_SEMANTIC_CACHE_TABLE
from llm_router.types import LLMResponse, TaskType

SECRET = "PROJECT_A_SECRET_sk_7f3ac91d4e"
FIXED_EMBEDDING = [0.1, 0.2, 0.3, 0.4]  # identical for every prompt → similarity 1.0


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    db_file = tmp_path / "usage.db"

    async def _init():
        conn = await aiosqlite.connect(str(db_file))
        await conn.executescript(CREATE_SEMANTIC_CACHE_TABLE)
        await conn.commit()
        await conn.close()

    asyncio.run(_init())

    # Deterministic embedding → cosine similarity is exactly 1.0 (worst case).
    monkeypatch.setattr(semantic_cache, "_get_embedding", lambda text, url: list(FIXED_EMBEDDING))

    class _Cfg:
        ollama_base_url = "http://localhost:11434"

    monkeypatch.setattr("llm_router.config.get_config", lambda: _Cfg())

    async def _fresh_db():
        return await aiosqlite.connect(str(db_file))

    monkeypatch.setattr("llm_router.cost._get_db", _fresh_db)
    return db_file


def _set_project(monkeypatch, path: str) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROJECT_DIR", path)


async def _store_secret():
    await semantic_cache.store(
        "what is the deploy key",
        TaskType.QUERY,
        LLMResponse(
            content=SECRET, model="ollama/x", input_tokens=0, output_tokens=0,
            cost_usd=0.0, latency_ms=0.0, provider="ollama",
        ),
    )


async def _check():
    return await semantic_cache.check("what is the deploy key", TaskType.QUERY)


def test_no_cross_project_hit(wired, monkeypatch) -> None:
    # Project A stores the secret.
    _set_project(monkeypatch, "/work/project-A")
    asyncio.run(_store_secret())

    # Project B asks the identical question (embedding identical → sim 1.0).
    _set_project(monkeypatch, "/work/project-B")
    hit = asyncio.run(_check())
    assert hit is None, (
        "CHZ-ST-004 regression: project B received project A's cached secret "
        "(cross-project semantic cache leak)"
    )


def test_same_project_still_hits(wired, monkeypatch) -> None:
    _set_project(monkeypatch, "/work/project-A")
    asyncio.run(_store_secret())
    hit = asyncio.run(_check())
    assert hit is not None and hit.content == SECRET, (
        "within-project semantic cache must still hit — scoping over-corrected"
    )
