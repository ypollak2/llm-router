"""TS/JS extractor (PLAN v16 P1.5 task 3, MUST P1.5-a "TS/JS fixture indexed").

Offline: fixtures in tests/fixtures/ts_js, no Node, no model.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_router.semantic import indexer as ix
from llm_router.semantic import store as sstore
from llm_router.semantic.extractors import typescript as ts

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "ts_js"


def _extract(name: str):
    out = ts.extract((FIX / name).read_text(encoding="utf-8"), name, "h")
    assert out is not None
    return out


def _by_name(entities):
    return {e.name: e for e in entities}


def test_definitions_found_with_kinds_and_lines():
    ents, _ = _extract("service.ts")
    got = _by_name(ents)
    for name, kind in {
        "loadConfig": "function", "helper": "function", "Router": "class",
        "DefaultThing": "class", "makeId": "function", "pick": "function",
        "legacy": "function", "Options": "type", "Handler": "type", "Mode": "type",
    }.items():
        assert got[name].kind == kind, (name, got[name])
    assert got["loadConfig"].start_line == 17
    # brace counting: helper spans its nested block, not just its first line
    assert (got["helper"].start_line, got["helper"].end_line) == (22, 27)
    assert "function loadConfig(file: string)" in got["loadConfig"].signature


def test_mentions_in_comments_and_strings_are_not_definitions():
    ents, _ = _extract("service.ts")
    names = {e.name for e in ents}
    assert "commentedOut" not in names
    assert "CommentedClass" not in names
    assert "inString" not in names


def test_imports_exports_and_requires():
    _, rels = _extract("service.ts")
    imports = {r.target_name for r in rels if r.type == "imports"}
    assert {"fs/promises", "path", "./config", "./side-effect"} <= imports
    names = {r.target_name for r in rels if r.type == "imports_name"}
    assert {"fs/promises.readFile", "path.default", "path.join"} <= names
    exported = {r.target_name for r in rels if r.type == "exports"}
    assert {"loadConfig", "Router", "makeId", "DefaultThing", "publicHelper", "pick"} <= exported
    assert "legacy" not in exported
    _, js_rels = _extract("legacy.js")
    assert {r.target_name for r in js_rels if r.type == "imports"} == {"fs", "path"}


def test_js_and_tsx_files():
    js, _ = _extract("legacy.js")
    assert {"readAll": "function", "Walker": "class"} == {e.name: e.kind for e in js}
    tsx, _ = _extract("view.tsx")
    assert {"Greeting", "Farewell"} == {e.name for e in tsx}


def test_language_table_covers_ts_and_js():
    assert {".py", ".ts", ".tsx", ".js", ".jsx"} <= set(ix._LANGUAGES)


@pytest.fixture
def repo(tmp_path: Path):
    r = tmp_path / "repo"
    r.mkdir()
    for f in FIX.iterdir():
        (r / f.name).write_bytes(f.read_bytes())
    (r / "mod.py").write_text("def py_fn():\n    return 1\n")
    subprocess.run(["git", "-C", str(r), "init", "-q"], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True, timeout=30)
    return r, tmp_path / "store"


def test_fixture_is_indexed_end_to_end(repo):
    r, base = repo
    result = ix.index(root=r, base=base)
    assert result.files_parsed == 4 and result.files_failed == 0, result
    conn = sstore.connect(r, base)
    try:
        defs = sstore.find_definitions("loadConfig", conn=conn)
        assert [(d.relative_path, d.kind) for d in defs] == [("service.ts", "function")]
        assert [d.relative_path for d in sstore.find_definitions("Walker", conn=conn)] == ["legacy.js"]
        assert [d.relative_path for d in sstore.find_definitions("py_fn", conn=conn)] == ["mod.py"]
    finally:
        conn.close()
    # unchanged second run: nothing re-parsed
    again = ix.index(root=r, base=base)
    assert again.files_parsed == 0 and again.files_skipped == 4, again
