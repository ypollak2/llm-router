"""Regression: GH#41, second front — every pull integration wrote a dead command.

The Claude-side fix covered install_hooks.py's three registration sites. But
`cli.py`, `commands/install.py` and the two IDE templates independently wrote
`{"command": "llm_router", "args": []}` into MCP configs for VS Code, Cursor,
Windsurf, Kimi, Gemini CLI, Copilot CLI, OpenCode, OpenClaw, Trae, Pi and
Codex — and three such files are committed in this repo.

`llm_router` is not a console script. `[project.scripts]` declares only
`llm-router`. So every one of those configs named a command that cannot
resolve on any install type: each pull integration was registered dead, in
exactly the way GH#41 reported for Claude Code.

These tests are the guard: no tracked config and no generated config may name
the underscore command again.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_DEAD = '"command": "llm_router"'


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, capture_output=True, text=True, timeout=30
    )
    return out.stdout.splitlines()


def test_project_scripts_really_has_no_underscore_entry_point():
    """Guards the guard: if an `llm_router` script is ever added, these tests are moot."""
    pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
    scripts = pyproject[pyproject.index("[project.scripts]"):]
    scripts = scripts[: scripts.index("\n[", 1)]
    assert "\nllm-router =" in scripts, "expected the hyphenated console script"
    assert "\nllm_router =" not in scripts, (
        "an underscore console script now exists — revisit these tests and GH#41"
    )


def test_no_tracked_file_names_the_dead_command():
    """Shipped code and configs only. Tests legitimately name the dead command —
    test_gh41_doctor_validates_mcp_command.py asserts that doctor FLAGS it."""
    offenders = []
    for rel in _tracked_files():
        p = _REPO / rel
        if rel.startswith("tests/") or rel.startswith("_quarantined_tests/"):
            continue
        if not p.is_file() or p.suffix not in {".json", ".py", ".toml", ".md", ".mdc"}:
            continue
        try:
            body = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _DEAD in body:
            offenders.append(rel)
    assert not offenders, (
        f"these tracked files name a command that cannot resolve — "
        f"`llm_router` is not a console script, only `llm-router` is: {sorted(offenders)}"
    )


@pytest.mark.parametrize("rel", [".vscode/mcp.json", ".windsurf/mcp.json", ".kimi/mcp.json"])
def test_committed_ide_configs_name_the_real_command(rel):
    path = _REPO / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("servers") or data.get("mcpServers") or {}
    entry = servers.get("llm_router")
    assert entry is not None, f"{rel} has no llm_router server block"
    assert entry["command"] == "llm-router", (
        f"{rel} must invoke the hyphenated console script, got {entry['command']!r}"
    )


def test_generated_ide_templates_name_the_real_command():
    """The templates in install_hooks.py are what `install --ide` writes."""
    import llm_router.install_hooks as ih

    for name, root_key in (("_VSCODE_MCP_CONTENT", "servers"),
                           ("_WINDSURF_MCP_CONTENT", "mcpServers")):
        data = json.loads(getattr(ih, name))
        entry = data[root_key]["llm_router"]
        assert entry["command"] == "llm-router", (
            f"{name} writes {entry['command']!r} — a command that cannot resolve"
        )


def test_generated_ide_templates_are_valid_json():
    """Found while fixing the command name: both templates were INVALID JSON.

    `localize()` rewrites the old tool names to the 1.0 surface — `llm_code`
    becomes `llm(task="code")` — and these templates run that rewrite over a
    JSON document. The replacement text contains double quotes, so it was
    injected raw into the `"description"` string literal and broke it. The
    templates are written to disk verbatim by install_ide_configs(), so
    `llm-router install --ide` produced .vscode/mcp.json and
    .windsurf/mcp.json that no IDE can parse.
    """
    import llm_router.install_hooks as ih

    for name in ("_VSCODE_MCP_CONTENT", "_WINDSURF_MCP_CONTENT"):
        content = getattr(ih, name)
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            raise AssertionError(f"{name} is not valid JSON: {e}") from None


def test_installed_ide_configs_are_valid_json(tmp_path):
    """End-to-end: what install_ide_configs actually writes must parse."""
    import llm_router.install_hooks as ih

    ih.install_ide_configs(tmp_path)
    for rel in (".vscode/mcp.json", ".windsurf/mcp.json"):
        written = tmp_path / rel
        assert written.exists(), f"{rel} was not written"
        data = json.loads(written.read_text(encoding="utf-8"))  # must not raise
        servers = data.get("servers") or data.get("mcpServers")
        assert servers["llm_router"]["command"] == "llm-router"
