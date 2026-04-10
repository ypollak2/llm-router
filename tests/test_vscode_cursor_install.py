"""Tests for VS Code + Cursor install support (v3.6.0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() and os.environ to a temp directory."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    if sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    return tmp_path


@pytest.fixture
def fake_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── VS Code install ───────────────────────────────────────────────────────────


class TestInstallVsCodeFiles:
    def test_creates_mcp_json_with_servers_key(self, fake_home):
        from llm_router.cli import _install_vscode_files

        actions = _install_vscode_files()

        # Find the mcp.json that was written
        if sys.platform == "darwin":
            mcp_json = fake_home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
        elif sys.platform == "win32":
            mcp_json = fake_home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
        else:
            mcp_json = fake_home / ".config" / "Code" / "User" / "mcp.json"

        assert mcp_json.exists(), f"mcp.json not created at {mcp_json}"
        data = json.loads(mcp_json.read_text())

        # VS Code uses "servers", NOT "mcpServers"
        assert "servers" in data, "VS Code mcp.json must use 'servers' root key"
        assert "mcpServers" not in data, "VS Code mcp.json must NOT use 'mcpServers'"
        assert "llm-router" in data["servers"]
        assert data["servers"]["llm-router"]["command"] == "uvx"

    def test_action_confirms_file_written(self, fake_home):
        from llm_router.cli import _install_vscode_files

        actions = _install_vscode_files()
        file_actions = [a for a in actions if "llm-router" in a or "mcp.json" in a.lower()]
        assert file_actions, f"Expected confirmation action, got: {actions}"

    def test_idempotent_no_duplicate_server(self, fake_home):
        from llm_router.cli import _install_vscode_files

        _install_vscode_files()
        _install_vscode_files()

        if sys.platform == "darwin":
            mcp_json = fake_home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
        elif sys.platform == "win32":
            mcp_json = fake_home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
        else:
            mcp_json = fake_home / ".config" / "Code" / "User" / "mcp.json"

        data = json.loads(mcp_json.read_text())
        assert len(data["servers"]) == 1  # not duplicated

    def test_copilot_instructions_appended(self, fake_home, fake_cwd):
        from llm_router.cli import _install_vscode_files

        # Create .github/copilot-instructions.md in cwd
        github_dir = fake_cwd / ".github"
        github_dir.mkdir()
        instructions = github_dir / "copilot-instructions.md"
        instructions.write_text("# Existing instructions\n")

        _install_vscode_files()

        content = instructions.read_text()
        assert "llm-router" in content

    def test_copilot_instructions_not_duplicated(self, fake_home, fake_cwd):
        from llm_router.cli import _install_vscode_files

        github_dir = fake_cwd / ".github"
        github_dir.mkdir()
        instructions = github_dir / "copilot-instructions.md"
        instructions.write_text("# llm-router already here\n")

        actions = _install_vscode_files()
        skip_actions = [a for a in actions if "skipped" in a]
        assert skip_actions, "Should skip copilot-instructions.md if llm-router already present"

    def test_merges_with_existing_servers(self, fake_home):
        from llm_router.cli import _install_vscode_files

        if sys.platform == "darwin":
            mcp_json = fake_home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
        elif sys.platform == "win32":
            mcp_json = fake_home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
        else:
            mcp_json = fake_home / ".config" / "Code" / "User" / "mcp.json"

        mcp_json.parent.mkdir(parents=True, exist_ok=True)
        mcp_json.write_text(json.dumps({"servers": {"other-tool": {"command": "other"}}}))

        _install_vscode_files()

        data = json.loads(mcp_json.read_text())
        assert "other-tool" in data["servers"]
        assert "llm-router" in data["servers"]


# ── Cursor install ────────────────────────────────────────────────────────────


class TestInstallCursorFiles:
    def test_creates_cursor_mcp_json(self, fake_home):
        from llm_router.cli import _install_cursor_files

        _install_cursor_files()

        mcp_json = fake_home / ".cursor" / "mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text())

        # Cursor uses "mcpServers"
        assert "mcpServers" in data, "Cursor mcp.json must use 'mcpServers' root key"
        assert "servers" not in data, "Cursor mcp.json must NOT use 'servers'"
        assert "llm-router" in data["mcpServers"]

    def test_action_confirms_file_written(self, fake_home):
        from llm_router.cli import _install_cursor_files

        actions = _install_cursor_files()
        assert any("llm-router" in a or ".cursor" in a for a in actions)

    def test_idempotent_no_duplicate_server(self, fake_home):
        from llm_router.cli import _install_cursor_files

        _install_cursor_files()
        _install_cursor_files()

        mcp_json = fake_home / ".cursor" / "mcp.json"
        data = json.loads(mcp_json.read_text())
        assert len(data["mcpServers"]) == 1

    def test_cursor_rules_written(self, fake_home):
        from llm_router.cli import _install_cursor_files

        _install_cursor_files()

        cursor_rules = fake_home / ".cursor" / "rules" / "llm-router.md"
        assert cursor_rules.exists(), f"Cursor rules not written to {cursor_rules}"
        content = cursor_rules.read_text()
        assert "llm-router" in content.lower()

    def test_cursor_rules_not_duplicated(self, fake_home):
        from llm_router.cli import _install_cursor_files

        _install_cursor_files()
        actions_second = _install_cursor_files()
        skip_actions = [a for a in actions_second if "skipped" in a]
        # mcp.json skipped + rules skipped
        assert len(skip_actions) >= 1

    def test_merges_with_existing_mcp_servers(self, fake_home):
        from llm_router.cli import _install_cursor_files

        mcp_json = fake_home / ".cursor" / "mcp.json"
        mcp_json.parent.mkdir(parents=True, exist_ok=True)
        mcp_json.write_text(json.dumps({"mcpServers": {"existing": {"command": "x"}}}))

        _install_cursor_files()

        data = json.loads(mcp_json.read_text())
        assert "existing" in data["mcpServers"]
        assert "llm-router" in data["mcpServers"]


# ── _install_host dispatch ────────────────────────────────────────────────────


class TestInstallHostDispatch:
    def test_install_host_vscode(self, capsys, fake_home):
        from llm_router.cli import _install_host

        _install_host("vscode")
        out = capsys.readouterr().out
        assert "VS Code" in out

    def test_install_host_cursor(self, capsys, fake_home):
        from llm_router.cli import _install_host

        _install_host("cursor")
        out = capsys.readouterr().out
        assert "Cursor" in out

    def test_install_host_all_includes_vscode_cursor(self, capsys, fake_home):
        from llm_router.cli import _install_host

        _install_host("all")
        out = capsys.readouterr().out
        assert "VS Code" in out
        assert "Cursor" in out


# ── Rules files exist ─────────────────────────────────────────────────────────


class TestRulesFilesExist:
    def test_vscode_rules_exists(self):
        from llm_router import __file__ as pkg_init

        rules = Path(pkg_init).parent / "rules" / "vscode-rules.md"
        assert rules.exists(), f"vscode-rules.md not found at {rules}"

    def test_cursor_rules_exists(self):
        from llm_router import __file__ as pkg_init

        rules = Path(pkg_init).parent / "rules" / "cursor-rules.md"
        assert rules.exists(), f"cursor-rules.md not found at {rules}"

    def test_vscode_rules_contain_routing_guidance(self):
        from llm_router import __file__ as pkg_init

        rules = Path(pkg_init).parent / "rules" / "vscode-rules.md"
        content = rules.read_text()
        assert "llm_auto" in content or "llm_research" in content

    def test_cursor_rules_contain_routing_guidance(self):
        from llm_router import __file__ as pkg_init

        rules = Path(pkg_init).parent / "rules" / "cursor-rules.md"
        content = rules.read_text()
        assert "llm_auto" in content or "llm_research" in content


# ── merge_json_mcp_block root_key ─────────────────────────────────────────────


class TestMergeJsonMcpBlockRootKey:
    def test_default_root_key_is_mcpServers(self, tmp_path):
        from llm_router.cli import _merge_json_mcp_block

        path = tmp_path / "test.json"
        _merge_json_mcp_block(path, "my-server", {"command": "uvx"})
        data = json.loads(path.read_text())
        assert "mcpServers" in data
        assert "servers" not in data

    def test_custom_root_key_servers(self, tmp_path):
        from llm_router.cli import _merge_json_mcp_block

        path = tmp_path / "test.json"
        _merge_json_mcp_block(path, "my-server", {"command": "uvx"}, root_key="servers")
        data = json.loads(path.read_text())
        assert "servers" in data
        assert "mcpServers" not in data

    def test_preserves_existing_keys_with_different_root(self, tmp_path):
        from llm_router.cli import _merge_json_mcp_block

        path = tmp_path / "test.json"
        path.write_text(json.dumps({"version": 1, "servers": {"existing": {}}}))
        _merge_json_mcp_block(path, "llm-router", {"command": "uvx"}, root_key="servers")
        data = json.loads(path.read_text())
        assert data["version"] == 1
        assert "existing" in data["servers"]
        assert "llm-router" in data["servers"]
