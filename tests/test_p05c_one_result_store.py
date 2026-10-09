"""P0.5-c (PLAN v16 PG3, D-R8-5): the semantic cache is the ONLY result store.

The legacy ``result_cache`` keyed rows on ``sha256(prompt)`` alone and
``context_prep`` injected the BM25 neighbours as "[Relevant prior answers]" into
whatever model/system/context came next. A prior answer produced under one
model, system prompt and context therefore surfaced in a different one.
"""
from __future__ import annotations

import ast
import pathlib
import sqlite3

import pytest

from llm_router.context_prep import prepare_prompt
from llm_router.types import Complexity, LLMResponse, TaskType

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
    monkeypatch.setenv("HOME", str(tmp_path))


def _resp(text: str) -> LLMResponse:
    return LLMResponse(
        content=text, model="openai/gpt-4o", input_tokens=5, output_tokens=5,
        cost_usd=0.0, latency_ms=1.0, provider="openai",
    )


def test_a_prior_answer_is_never_injected_into_a_different_context():
    """Red on main: the answer stored for one request surfaced in another."""
    from llm_router.tools import text

    secret = "ANSWER-FROM-CONTEXT-A-7731"
    prompt = "yes, do it now with the migration plan"
    if hasattr(text, "_cache_result"):  # main: the legacy writer
        text._cache_result(prompt, _resp(secret), "query", "simple")

    for system in (None, "You are a different assistant with other rules."):
        prepared = prepare_prompt(
            prompt, TaskType.QUERY, Complexity.SIMPLE, "ollama/gemma4:latest",
            existing_system_prompt=system,
        )
        assert secret not in prepared.full_system
        assert prepared.context == ""
        assert prepared.context_source == "none"


def test_a_legacy_result_cache_db_on_disk_is_neither_read_nor_modified(tmp_path):
    """Users' old ~/.llm-router/result_cache.db is left alone and unread."""
    home = tmp_path / ".llm-router"
    home.mkdir()
    db = home / "result_cache.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE results (id INTEGER PRIMARY KEY, user_prompt TEXT, response TEXT)")
    conn.execute("INSERT INTO results (user_prompt, response) VALUES ('hello world', 'LEGACY-ROW')")
    conn.commit()
    conn.close()
    before = (db.read_bytes(), db.stat().st_mtime_ns)

    prepared = prepare_prompt("hello world", TaskType.QUERY, Complexity.SIMPLE, "ollama/gemma4:latest")
    assert "LEGACY-ROW" not in prepared.full_system
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_nothing_imports_result_cache():
    """AST scan: no module in src/, hooks/ or scripts/ imports result_cache."""
    root = SRC.parents[1]
    offenders = []
    for base in (SRC, root / "hooks", root / "scripts"):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [a.name for a in node.names]
                if any(n.split(".")[-1] == "result_cache" or n.endswith(".result_cache") for n in names):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, offenders
    assert not (SRC / "result_cache.py").exists()


def test_semantic_cache_still_serves_an_identical_request():
    """The one store keeps its hit for the identical request and only that."""
    import asyncio

    from llm_router import semantic_cache

    async def go():
        prompt = "what is the capital of france"
        ka = semantic_cache.make_key(prompt, context_text="ctx-A")
        kb = semantic_cache.make_key(prompt, context_text="ctx-B")
        await semantic_cache.store(prompt, TaskType.QUERY, _resp("Paris"), key=ka)
        hit = await semantic_cache.check(prompt, TaskType.QUERY, key=ka)
        miss = await semantic_cache.check(prompt, TaskType.QUERY, key=kb)
        return None, hit, miss

    _, hit, miss = asyncio.run(go())
    assert hit is not None and "Paris" in hit.content
    assert miss is None
