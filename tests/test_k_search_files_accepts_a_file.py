"""K: search_files scoped to a FILE must search that file.

Live 2026-09-24: a read-only draft asked "Which tools does READ_ONLY_TOOLS in
agent_loop.py allow?" read the file (truncated at the cap), then searched it
with path="src/llm_router/hooks/agent_loop.py" and got "(no matches)" —
`Path.rglob` on a file yields nothing. The model believed its tool and told
the user the name does not exist. A search that cannot fail loudly on a file
path turns every narrowed search into a false negative.
"""
from __future__ import annotations

from llm_router.hooks.agent_loop import execute_tool


def test_a_search_scoped_to_one_file_finds_what_is_in_it(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "loop.py").write_text("x = 1\nREAD_ONLY_TOOLS = ('read_file',)\n")
    whole = execute_tool("search_files", {"pattern": "READ_ONLY_TOOLS", "path": "."}, tmp_path)
    assert "loop.py:2" in whole, "premise: a directory search finds it"
    one = execute_tool("search_files", {"pattern": "READ_ONLY_TOOLS",
                                        "path": "pkg/loop.py"}, tmp_path)
    assert "pkg/loop.py:2: READ_ONLY_TOOLS" in one, one


def test_a_file_search_ignores_the_default_file_pattern(tmp_path):
    """file_pattern defaults to *.py; a named .md file must still be searched."""
    (tmp_path / "notes.md").write_text("the answer is 42\n")
    out = execute_tool("search_files", {"pattern": "answer", "path": "notes.md"}, tmp_path)
    assert "notes.md:1" in out, out
