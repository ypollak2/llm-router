"""The workdir snapshot must not hash the world.

A5 of docs/ACTIONS_REMEDIATION_RUN.md. `_snapshot` walked `root.rglob("*")` with a
six-entry noise set, no file cap, no byte cap and no duration budget, so it
descended into `.venv`, `node_modules`, build output and anything vendored under
the root — twice per run, before and after.

Measured on this repository 2026-09-14:

    old: 10,276 files in 1.8s, of which 8,103 were .venv  (79% of the work)
    new:    944 files in 0.1s

The snapshot exists to say which files the run CHANGED. Hashing a virtualenv
cannot contribute to that answer.
"""
from __future__ import annotations

import time

import pytest

from llm_router.tools import local_task as lt


def _tree(root, spec):
    for path, content in spec.items():
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


@pytest.mark.parametrize("junk", [".venv", "node_modules", "dist", "build", ".tox"])
def test_dependency_and_build_directories_are_not_hashed(tmp_path, junk):
    _tree(tmp_path, {
        "src/app.py": "print('real')",
        f"{junk}/lib/thing.py": "x = 1",
        f"{junk}/deep/nested/other.py": "y = 2",
    })
    snap = lt._snapshot(tmp_path)
    assert "src/app.py" in snap
    assert not [k for k in snap if k.startswith(junk)], (
        f"{junk} was hashed; on a real project this is the bulk of the work and "
        "none of it can tell you what the run changed"
    )


def test_real_source_is_still_captured_and_changes_are_detected(tmp_path):
    _tree(tmp_path, {"src/a.py": "one", "docs/b.md": "two"})
    before = lt._snapshot(tmp_path)
    assert set(before) == {"src/a.py", "docs/b.md"}
    (tmp_path / "src/a.py").write_text("changed")
    after = lt._snapshot(tmp_path)
    assert lt._changed(before, after) == ["src/a.py"]


def test_the_file_count_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "_SNAPSHOT_MAX_FILES", 10)
    _tree(tmp_path, {f"src/f{i}.py": str(i) for i in range(50)})
    assert len(lt._snapshot(tmp_path)) <= 10


def test_the_byte_budget_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "_SNAPSHOT_MAX_BYTES", 500)
    _tree(tmp_path, {f"src/f{i}.py": "x" * 200 for i in range(20)})
    snap = lt._snapshot(tmp_path)
    assert len(snap) < 20, "the byte budget did not stop anything"


def test_a_big_tree_completes_quickly(tmp_path):
    _tree(tmp_path, {"src/app.py": "real"})
    _tree(tmp_path, {f".venv/lib/python3.12/site-packages/pkg{i}/mod.py": "x" * 2000
                     for i in range(600)})
    t0 = time.monotonic()
    snap = lt._snapshot(tmp_path)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"took {elapsed:.1f}s on a tree that is mostly a virtualenv"
    assert list(snap) == ["src/app.py"]
