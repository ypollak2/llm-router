"""In-session incremental re-index (PLAN v16 P1.5 task 2, MUST P1.5-a "in-session re-index").

Offline. The detached child is replaced by a synchronous spawn; no process forks,
no model loads. The hook test feeds a PostToolUse payload to the real hook with
LLM_ROUTER_HOME pointed at a temp dir.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

from llm_router.semantic import indexer as ix
from llm_router.semantic import refresh
from llm_router.semantic import store as sstore

HOOK = Path(__file__).resolve().parent.parent.parent / "src/llm_router/hooks/context-capture.py"


@pytest.fixture
def proj(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def old_fn():\n    return 1\n")
    (repo / "b.ts").write_text("export function oldTs() { return 1; }\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, timeout=30)
    base = tmp_path / "store"
    ix.index(root=repo, base=base)
    return repo, base


def _defined(repo, base, name):
    conn = sstore.connect(repo, base)
    try:
        return [d.relative_path for d in sstore.find_definitions(name, conn=conn)]
    finally:
        conn.close()


def _sync_spawn(base):
    calls = []

    def spawn(scope):
        calls.append(scope)
        refresh.run(scope, base)
    return calls, spawn


def test_repeated_edits_to_one_file_do_not_reach_the_ten_file_threshold(proj):
    repo, base = proj
    calls, spawn = _sync_spawn(base)
    (repo / "a.py").write_text("def old_fn():\n    return 1\n\ndef brand_new_fn():\n    return 2\n")
    for i in range(9):
        assert refresh.note_edit(str(repo / "a.py"), repo, base, spawn=spawn)
    # 9 queued lines, one distinct path, but the threshold counts distinct files
    assert refresh.pending(repo, base) == ["a.py"]
    assert calls == [] and _defined(repo, base, "brand_new_fn") == []


def test_ten_distinct_files_trigger_one_reindex_within_the_session(proj):
    repo, base = proj
    calls, spawn = _sync_spawn(base)
    for i in range(9):
        (repo / f"n{i}.ts").write_text(f"export function fn{i}() {{ return {i}; }}\n")
        refresh.note_edit(str(repo / f"n{i}.ts"), repo, base, spawn=spawn)
    assert calls == []
    (repo / "a.py").write_text("def old_fn():\n    return 1\n\ndef brand_new_fn():\n    return 2\n")
    refresh.note_edit(str(repo / "a.py"), repo, base, spawn=spawn)
    assert len(calls) == 1
    # untracked, never committed: reached the index anyway
    assert _defined(repo, base, "brand_new_fn") == ["a.py"]
    assert _defined(repo, base, "fn3") == ["n3.ts"]
    assert refresh.pending(repo, base) == []


def test_session_start_drains_a_short_queue(proj):
    repo, base = proj
    calls, spawn = _sync_spawn(base)
    (repo / "b.ts").write_text("export function newTs() { return 1; }\n")
    refresh.note_edit(str(repo / "b.ts"), repo, base, spawn=spawn)
    assert calls == []
    assert refresh.maybe_spawn(repo, base, minimum=1, spawn=spawn)
    assert _defined(repo, base, "newTs") == ["b.ts"]
    assert _defined(repo, base, "oldTs") == []


def test_a_deleted_file_is_forgotten(proj):
    repo, base = proj
    (repo / "b.ts").unlink()
    refresh.mark_dirty(str(repo / "b.ts"), repo, base)
    refresh.run(repo, base)
    assert _defined(repo, base, "oldTs") == []


def test_declines_unindexed_projects_outside_paths_and_other_suffixes(proj, tmp_path, monkeypatch):
    repo, base = proj
    assert not refresh.mark_dirty(str(repo / "README.md"), repo, base)
    assert not refresh.mark_dirty(str(tmp_path / "elsewhere.py"), repo, base)
    other = tmp_path / "other"
    other.mkdir()
    assert not refresh.mark_dirty(str(other / "x.py"), other, tmp_path / "no-index")
    assert not (tmp_path / "no-index").exists()  # no store created for it
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_REFRESH", "0")
    assert not refresh.mark_dirty(str(repo / "a.py"), repo, base)


def test_spawn_cooldown_prevents_a_second_child(proj):
    repo, base = proj
    spawned = []
    for i in range(10):
        (repo / f"c{i}.py").write_text(f"def c{i}(): pass\n")
        refresh.mark_dirty(str(repo / f"c{i}.py"), repo, base)
    assert refresh.maybe_spawn(repo, base, spawn=spawned.append)
    assert not refresh.maybe_spawn(repo, base, spawn=spawned.append)
    assert len(spawned) == 1


def test_the_real_hook_queues_an_edit(proj, tmp_path, monkeypatch):
    """PostToolUse payload -> semantic_dirty.txt, through the shipped hook file."""
    repo, _ = proj
    home = tmp_path / "home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    # the index must live where the hook will look (default base under LLM_ROUTER_HOME)
    ix.index(root=repo)
    (repo / "a.py").write_text("def hooked_fn():\n    return 3\n")
    spec = importlib.util.spec_from_file_location("_cc_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    payload = {"tool_name": "Edit", "cwd": str(repo),
               "tool_input": {"file_path": str(repo / "a.py"), "old_string": "x", "new_string": "y"},
               "tool_response": "ok"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        mod.main()
    assert refresh.pending(repo) == ["a.py"]


def test_hook_copies_are_identical_and_versions_bumped():
    root = Path(__file__).resolve().parent.parent.parent
    for name in ("context-capture.py", "session-start.py"):
        assert (root / "hooks" / name).read_bytes() == (root / "src/llm_router/hooks" / name).read_bytes()
