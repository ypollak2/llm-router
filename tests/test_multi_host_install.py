"""Tests for multi-host install functions (v3.5.0).

Validates that each _install_*_files() function writes the correct files
to a temporary home directory without touching the real filesystem.
"""

from __future__ import annotations

import json
import pathlib

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _patch_home(monkeypatch, tmp_path):
    """Redirect Path.home() to tmp_path so no real files are touched."""
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers used by multiple install functions
# ─────────────────────────────────────────────────────────────────────────────

class TestMergeJsonMcpBlock:
    def test_creates_new_file(self, tmp_path):
        from llm_router.cli import _merge_json_mcp_block

        config = tmp_path / "config.json"
        actions = _merge_json_mcp_block(config, "llm-router", {"command": "uvx"})
        assert config.exists()
        data = json.loads(config.read_text())
        assert data["mcpServers"]["llm-router"] == {"command": "uvx"}
        assert any("Added" in a for a in actions)

    def test_merges_into_existing_file(self, tmp_path):
        from llm_router.cli import _merge_json_mcp_block

        config = tmp_path / "config.json"
        config.write_text(json.dumps({"mcpServers": {"other": {}}}))
        _merge_json_mcp_block(config, "llm-router", {"command": "uvx"})
        data = json.loads(config.read_text())
        assert "other" in data["mcpServers"]
        assert "llm-router" in data["mcpServers"]

    def test_idempotent_second_call(self, tmp_path):
        from llm_router.cli import _merge_json_mcp_block

        config = tmp_path / "config.json"
        _merge_json_mcp_block(config, "llm-router", {"command": "uvx"})
        actions2 = _merge_json_mcp_block(config, "llm-router", {"command": "uvx"})
        assert any("skipped" in a for a in actions2)


class TestAppendRoutingRules:
    def test_creates_new_file(self, tmp_path):
        from llm_router.cli import _append_routing_rules

        dest = tmp_path / "instructions.md"
        actions = _append_routing_rules(dest, "opencode-rules.md")
        assert dest.exists()
        assert "llm-router" in dest.read_text().lower()
        assert any("Created" in a for a in actions)

    def test_appends_to_existing_file(self, tmp_path):
        from llm_router.cli import _append_routing_rules

        dest = tmp_path / "instructions.md"
        dest.write_text("# Existing instructions\n")
        _append_routing_rules(dest, "opencode-rules.md")
        content = dest.read_text()
        assert "# Existing instructions" in content
        assert "llm-router" in content.lower()

    def test_idempotent_second_call(self, tmp_path):
        from llm_router.cli import _append_routing_rules

        dest = tmp_path / "instructions.md"
        _append_routing_rules(dest, "opencode-rules.md")
        actions2 = _append_routing_rules(dest, "opencode-rules.md")
        assert any("skipped" in a for a in actions2)


# ─────────────────────────────────────────────────────────────────────────────
# OpenCode
# ─────────────────────────────────────────────────────────────────────────────

class TestInstallOpenCode:
    def test_writes_mcp_config(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_opencode_files
        _patch_home(monkeypatch, tmp_path)

        _install_opencode_files()

        config = tmp_path / ".config" / "opencode" / "config.json"
        assert config.exists()
        data = json.loads(config.read_text())
        assert "llm-router" in data["mcpServers"]

    def test_writes_routing_rules(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_opencode_files
        _patch_home(monkeypatch, tmp_path)

        _install_opencode_files()

        instructions = tmp_path / ".config" / "opencode" / "instructions.md"
        assert instructions.exists()
        assert "llm-router" in instructions.read_text().lower()

    def test_returns_action_list(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_opencode_files
        _patch_home(monkeypatch, tmp_path)

        actions = _install_opencode_files()
        assert isinstance(actions, list)
        assert len(actions) > 0

    def test_idempotent_second_run(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_opencode_files
        _patch_home(monkeypatch, tmp_path)

        _install_opencode_files()
        actions2 = _install_opencode_files()
        skipped = [a for a in actions2 if "skipped" in a]
        assert len(skipped) >= 2  # config + rules both skipped


# ─────────────────────────────────────────────────────────────────────────────
# Gemini CLI
# ─────────────────────────────────────────────────────────────────────────────

class TestInstallGeminiCli:
    def test_writes_settings_json(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_gemini_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_gemini_cli_files()

        settings = tmp_path / ".gemini" / "settings.json"
        assert settings.exists()
        data = json.loads(settings.read_text())
        assert "llm-router" in data["mcpServers"]

    def test_creates_extension_manifest(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_gemini_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_gemini_cli_files()

        manifest = tmp_path / ".gemini" / "extensions" / "llm-router" / "gemini-extension.json"
        assert manifest.exists()
        data = json.loads(manifest.read_text())
        assert data["name"] == "llm-router"

    def test_creates_hooks_json(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_gemini_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_gemini_cli_files()

        hooks = tmp_path / ".gemini" / "extensions" / "llm-router" / "hooks" / "hooks.json"
        assert hooks.exists()
        data = json.loads(hooks.read_text())
        assert "PostToolUse" in data["hooks"]

    def test_writes_routing_rules(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_gemini_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_gemini_cli_files()

        instructions = tmp_path / ".gemini" / "extensions" / "llm-router" / "INSTRUCTIONS.md"
        assert instructions.exists()
        assert "llm-router" in instructions.read_text().lower()

    def test_idempotent_second_run(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_gemini_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_gemini_cli_files()
        actions2 = _install_gemini_cli_files()
        skipped = [a for a in actions2 if "skipped" in a]
        assert len(skipped) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Copilot CLI
# ─────────────────────────────────────────────────────────────────────────────

class TestInstallCopilotCli:
    def test_writes_mcp_config(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_copilot_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_copilot_cli_files()

        mcp = tmp_path / ".config" / "gh" / "copilot" / "mcp.json"
        assert mcp.exists()
        data = json.loads(mcp.read_text())
        assert "llm-router" in data["mcpServers"]

    def test_writes_routing_rules(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_copilot_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_copilot_cli_files()

        instructions = tmp_path / ".config" / "gh" / "copilot" / "instructions.md"
        assert instructions.exists()

    def test_idempotent(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_copilot_cli_files
        _patch_home(monkeypatch, tmp_path)

        _install_copilot_cli_files()
        actions2 = _install_copilot_cli_files()
        skipped = [a for a in actions2 if "skipped" in a]
        assert len(skipped) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# OpenClaw
# ─────────────────────────────────────────────────────────────────────────────

class TestInstallOpenclaw:
    def test_writes_mcp_config(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_openclaw_files
        _patch_home(monkeypatch, tmp_path)

        _install_openclaw_files()

        mcp = tmp_path / ".openclaw" / "mcp.json"
        assert mcp.exists()
        data = json.loads(mcp.read_text())
        assert "llm-router" in data["mcpServers"]

    def test_writes_routing_rules(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_openclaw_files
        _patch_home(monkeypatch, tmp_path)

        _install_openclaw_files()

        instructions = tmp_path / ".openclaw" / "instructions.md"
        assert instructions.exists()

    def test_idempotent(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_openclaw_files
        _patch_home(monkeypatch, tmp_path)

        _install_openclaw_files()
        actions2 = _install_openclaw_files()
        assert any("skipped" in a for a in actions2)


# ─────────────────────────────────────────────────────────────────────────────
# Trae IDE
# ─────────────────────────────────────────────────────────────────────────────

class TestInstallTrae:
    def test_writes_mcp_config(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_trae_files
        _patch_home(monkeypatch, tmp_path)

        _install_trae_files()

        # Check that some mcp.json was written under the Trae dir
        mcp_files = list(tmp_path.rglob("mcp.json"))
        assert len(mcp_files) >= 1
        data = json.loads(mcp_files[0].read_text())
        assert "llm-router" in data["mcpServers"]

    def test_returns_action_list(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_trae_files
        _patch_home(monkeypatch, tmp_path)

        actions = _install_trae_files()
        assert isinstance(actions, list)
        assert len(actions) > 0

    def test_idempotent(self, monkeypatch, tmp_path):
        from llm_router.cli import _install_trae_files
        _patch_home(monkeypatch, tmp_path)

        _install_trae_files()
        actions2 = _install_trae_files()
        assert any("skipped" in a for a in actions2)


# ─────────────────────────────────────────────────────────────────────────────
# Factory Droid manifests
# ─────────────────────────────────────────────────────────────────────────────

class TestFactoryDroidManifest:
    def test_plugin_json_exists(self):
        import pathlib
        manifest = pathlib.Path(__file__).parent.parent / ".factory-plugin" / "plugin.json"
        assert manifest.exists(), ".factory-plugin/plugin.json must exist"

    def test_plugin_json_schema(self):
        import pathlib
        manifest = pathlib.Path(__file__).parent.parent / ".factory-plugin" / "plugin.json"
        data = json.loads(manifest.read_text())
        assert data["name"] == "llm-router"
        assert "version" in data
        assert "mcpServers" in data or "skills" in data

    def test_marketplace_json_exists(self):
        import pathlib
        mkt = pathlib.Path(__file__).parent.parent / ".factory-plugin" / "marketplace.json"
        assert mkt.exists(), ".factory-plugin/marketplace.json must exist"

    def test_marketplace_version_matches_plugin(self):
        import pathlib
        plugin = pathlib.Path(__file__).parent.parent / ".factory-plugin" / "plugin.json"
        mkt = pathlib.Path(__file__).parent.parent / ".factory-plugin" / "marketplace.json"
        plugin_version = json.loads(plugin.read_text())["version"]
        mkt_version = json.loads(mkt.read_text())["plugins"][0]["version"]
        assert plugin_version == mkt_version


# ─────────────────────────────────────────────────────────────────────────────
# Rules files content
# ─────────────────────────────────────────────────────────────────────────────

class TestRulesFileContent:
    @pytest.mark.parametrize("rules_file", [
        "opencode-rules.md",
        "gemini-cli-rules.md",
        "copilot-cli-rules.md",
        "openclaw-rules.md",
        "trae-rules.md",
    ])
    def test_rules_file_exists(self, rules_file):
        import pathlib
        p = pathlib.Path(__file__).parent.parent / "src" / "llm_router" / "rules" / rules_file
        assert p.exists(), f"{rules_file} must exist in src/llm_router/rules/"

    @pytest.mark.parametrize("rules_file", [
        "opencode-rules.md",
        "gemini-cli-rules.md",
        "copilot-cli-rules.md",
        "openclaw-rules.md",
        "trae-rules.md",
    ])
    def test_has_llm_auto_guidance(self, rules_file):
        import pathlib
        p = pathlib.Path(__file__).parent.parent / "src" / "llm_router" / "rules" / rules_file
        content = p.read_text()
        assert "llm_auto" in content, f"{rules_file} must mention llm_auto"

    @pytest.mark.parametrize("rules_file", [
        "opencode-rules.md",
        "gemini-cli-rules.md",
        "copilot-cli-rules.md",
        "openclaw-rules.md",
        "trae-rules.md",
        "codex-rules.md",
        "llm-router.md",
    ])
    def test_has_token_efficient_section(self, rules_file):
        import pathlib
        p = pathlib.Path(__file__).parent.parent / "src" / "llm_router" / "rules" / rules_file
        content = p.read_text()
        assert "Token-Efficient" in content or "preamble" in content.lower(), \
            f"{rules_file} must have token-efficient response guidance"
