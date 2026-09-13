"""The loop's tools must work when the project root contains a symlink.

`/tmp` is a symlink to `/private/tmp` on macOS, so every sandbox created under
it has a root that differs from its resolved form. `_resolve_path` correctly
compared against the RESOLVED root, but `list_files` and `search_files` then
reported paths relative to the UNRESOLVED one — `relative_to` raised, the outer
handler turned it into "Error executing list_files: … is not in the subpath of
…", and the model spent its entire iteration budget retrying a directory it was
entitled to read.

This went unnoticed because nothing tested the loop against a symlinked root,
and the loop had no execution trace — a broken tool and a confused model look
identical from outside. Both benchmark suites ran in /tmp, so every local-model
score was measured with two of its six tools erroring out.
"""
from __future__ import annotations

from llm_router.hooks.agent_loop import execute_tool


def _repo(root):
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# demo\n")
    return root


def test_list_files_works_through_a_symlinked_root(tmp_path):
    real = _repo(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    out = execute_tool("list_files", {"path": "."}, link)
    assert "Error" not in out, out
    assert "README.md" in out, out


def test_search_files_works_through_a_symlinked_root(tmp_path):
    real = _repo(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    out = execute_tool("search_files", {"pattern": "def add", "path": "."}, link)
    assert "Error" not in out, out
    assert "calc.py" in out, out


def test_paths_are_reported_relative_to_the_root(tmp_path):
    """Not absolute, and not relative to the resolved path the caller never saw."""
    real = _repo(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    out = execute_tool("list_files", {"path": "src"}, link)
    assert "src/calc.py" in out, out
    assert str(real) not in out, "leaked the resolved absolute path: " + out


def test_traversal_is_still_refused(tmp_path):
    """The fix must not widen the sandbox."""
    real = _repo(tmp_path / "real")
    (tmp_path / "secret.txt").write_text("nope")
    out = execute_tool("read_file", {"path": "../secret.txt"}, real)
    assert "outside project root" in out or "Error" in out, out
