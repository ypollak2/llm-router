"""Regression: GH#41 root cause — `doctor` reported 0 issues on a dead server.

The reporter's pipx install of 13.0.2 had `llm_router` registered in
mcpServers with a `uv run --directory <site-packages>` command that could
never start, and `claude mcp list` showed CONNECTION_CLOSED. `doctor` said
everything was fine, because every MCP check asked only:

    "llm_router" in settings.get("mcpServers", {})

— presence of a KEY. It never read the command back and never asked whether
that command could run. Fixing the lookup (GH#41) removes this instance; this
check is what turns the NEXT registration bug into a doctor failure instead of
a user bug report.
"""
from __future__ import annotations

import llm_router.commands.doctor as doc


def test_accepts_a_real_executable():
    import shutil
    real = shutil.which("sh")
    assert not doc._mcp_command_problems({"command": real, "args": []}, "test")


def test_accepts_a_bare_name_that_is_on_path():
    assert not doc._mcp_command_problems({"command": "sh", "args": []}, "test")


def test_flags_the_bare_literal_that_never_resolves():
    """GH#41 wrote the literal string "llm_router" into Claude Desktop."""
    problems = doc._mcp_command_problems({"command": "llm_router", "args": []}, "Claude Desktop")
    assert problems, "a command not on PATH must be reported"
    assert any("llm_router" in p for p in problems)


def test_flags_uv_run_pointed_at_a_non_project_directory(tmp_path):
    """The exact pipx failure: --directory <site-packages>, which has no pyproject.toml."""
    import shutil
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    entry = {
        "command": shutil.which("sh") or "sh",
        "args": ["run", "--directory", str(site_packages), "llm-router"],
    }
    problems = doc._mcp_command_problems(entry, "Claude Code")
    assert problems, "uv run against a directory with no pyproject.toml must be reported"
    assert any("pyproject.toml" in p for p in problems)


def test_accepts_uv_run_against_a_real_checkout(tmp_path):
    import shutil
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    entry = {
        "command": shutil.which("sh") or "sh",
        "args": ["run", "--directory", str(tmp_path), "llm-router"],
    }
    assert not doc._mcp_command_problems(entry, "Claude Code")


def test_flags_a_malformed_entry():
    assert doc._mcp_command_problems({}, "test")
    assert doc._mcp_command_problems("not-a-dict", "test")


def test_flags_a_command_path_that_does_not_exist(tmp_path):
    missing = tmp_path / "venvs" / "llm-routing" / "bin" / "llm-router"
    problems = doc._mcp_command_problems({"command": str(missing), "args": []}, "Claude Desktop")
    assert problems
    assert any(str(missing) in p for p in problems)
