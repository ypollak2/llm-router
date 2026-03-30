"""Tests for the llm_edit module (edit instruction generation and parsing)."""

from __future__ import annotations

import json

from llm_router.edit import (
    EditInstruction,
    build_edit_prompt,
    format_edit_result,
    parse_edit_response,
    read_file_for_edit,
)


# ── read_file_for_edit ────────────────────────────────────────────────────────


def test_read_file_success(tmp_path):
    """Reads a normal file and returns content without truncation."""
    f = tmp_path / "hello.py"
    f.write_text("def hello():\n    return 'world'\n")
    content, truncated = read_file_for_edit(str(f))
    assert "def hello():" in content
    assert not truncated


def test_read_file_truncation(tmp_path):
    """Files larger than max_bytes are truncated and truncated=True."""
    f = tmp_path / "big.py"
    f.write_bytes(b"x" * 100)
    content, truncated = read_file_for_edit(str(f), max_bytes=50)
    assert len(content) == 50
    assert truncated


def test_read_file_missing():
    """Non-existent file returns an error string and truncated=False."""
    content, truncated = read_file_for_edit("/does/not/exist.py")
    assert "Error" in content
    assert not truncated


# ── build_edit_prompt ─────────────────────────────────────────────────────────


def test_build_edit_prompt_contains_task():
    """The prompt includes the task description."""
    prompt = build_edit_prompt("Add type hints", {"src/foo.py": "def bar(): pass"})
    assert "Add type hints" in prompt


def test_build_edit_prompt_contains_file():
    """The prompt includes the file path and content."""
    prompt = build_edit_prompt("fix bug", {"myfile.py": "def oops(): None"})
    assert "myfile.py" in prompt
    assert "def oops():" in prompt


def test_build_edit_prompt_multiple_files():
    """Multiple files are all included in the prompt."""
    files = {
        "a.py": "x = 1",
        "b.py": "y = 2",
    }
    prompt = build_edit_prompt("rename vars", files)
    assert "a.py" in prompt
    assert "b.py" in prompt
    assert "x = 1" in prompt
    assert "y = 2" in prompt


# ── parse_edit_response ───────────────────────────────────────────────────────


def test_parse_valid_json_array():
    """Parses a clean JSON array response correctly."""
    raw = json.dumps([
        {
            "file": "src/main.py",
            "old_string": "pass",
            "new_string": "return 42",
            "description": "implement stub",
        }
    ])
    instructions, warnings = parse_edit_response(raw)
    assert len(instructions) == 1
    assert instructions[0].file == "src/main.py"
    assert instructions[0].old_string == "pass"
    assert instructions[0].new_string == "return 42"
    assert not warnings


def test_parse_json_in_fenced_block():
    """Parses JSON wrapped in a markdown code fence."""
    raw = (
        "Here are the edits:\n"
        "```json\n"
        '[{"file": "f.py", "old_string": "a", "new_string": "b"}]\n'
        "```\n"
    )
    instructions, warnings = parse_edit_response(raw)
    assert len(instructions) == 1
    assert instructions[0].file == "f.py"


def test_parse_empty_array():
    """Empty array means no edits and no warnings."""
    instructions, warnings = parse_edit_response("[]")
    assert instructions == []
    assert not warnings


def test_parse_missing_required_key():
    """Items missing required keys are skipped with a warning."""
    raw = json.dumps([{"file": "x.py", "old_string": "foo"}])  # missing new_string
    instructions, warnings = parse_edit_response(raw)
    assert instructions == []
    assert any("new_string" in w for w in warnings)


def test_parse_no_json():
    """Response with no JSON array returns empty instructions and a warning."""
    instructions, warnings = parse_edit_response("I don't know what to change.")
    assert instructions == []
    assert warnings


def test_parse_multiple_edits():
    """Multiple edit instructions are all parsed."""
    raw = json.dumps([
        {"file": "a.py", "old_string": "x = 1", "new_string": "x = 2", "description": ""},
        {"file": "b.py", "old_string": "y = 1", "new_string": "y = 3", "description": ""},
    ])
    instructions, warnings = parse_edit_response(raw)
    assert len(instructions) == 2
    assert not warnings


# ── format_edit_result ────────────────────────────────────────────────────────


def test_format_edit_result_no_edits():
    """No instructions → output says 'No edits to apply'."""
    result = format_edit_result([], [], "Model: gemini-flash | $0.001")
    assert "No edits to apply" in result


def test_format_edit_result_with_instructions():
    """Instructions are shown with file, old, and new strings."""
    instr = EditInstruction(
        file="src/foo.py",
        old_string="def old():\n    pass",
        new_string="def new():\n    return 1",
        description="rename function",
    )
    result = format_edit_result([instr], [], "Model: gemini-flash")
    assert "src/foo.py" in result
    assert "def old():" in result
    assert "def new():" in result
    assert "rename function" in result
    # Should include machine-readable JSON block
    assert '"file"' in result


def test_format_edit_result_warnings_shown():
    """Warnings are surfaced in the output."""
    result = format_edit_result([], ["File truncated: big.py"], "header")
    assert "big.py" in result
