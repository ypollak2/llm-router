"""Tests for install_hooks update logic."""

import os

from llm_router.install_hooks import (
    _register_hook,
    _rules_version,
    check_and_update_hooks,
    check_and_update_rules,
)


class TestRulesVersion:
    def test_no_header_returns_zero(self, tmp_path):
        f = tmp_path / "rules.md"
        f.write_text("# Some Rules\n\nContent here.\n")
        assert _rules_version(f) == 0

    def test_missing_file_returns_zero(self, tmp_path):
        assert _rules_version(tmp_path / "nonexistent.md") == 0

    def test_parses_version_header(self, tmp_path):
        f = tmp_path / "rules.md"
        f.write_text("<!-- llm-router-rules-version: 5 -->\n# Rules\n")
        assert _rules_version(f) == 5

    def test_version_with_extra_spaces(self, tmp_path):
        f = tmp_path / "rules.md"
        f.write_text("<!--  llm-router-rules-version:  3  -->\n# Rules\n")
        assert _rules_version(f) == 3

    def test_version_not_on_first_line_ignored(self, tmp_path):
        f = tmp_path / "rules.md"
        f.write_text("# Rules\n<!-- llm-router-rules-version: 7 -->\n")
        assert _rules_version(f) == 0

    def test_bundled_rules_have_version(self):
        """The bundled source rules file must have a version header."""
        from llm_router.install_hooks import _RULES_SRC
        bundled = _RULES_SRC / "llm-router.md"
        assert bundled.exists(), "Bundled rules file is missing"
        assert _rules_version(bundled) > 0, "Bundled rules must have a version header"


class TestCheckAndUpdateRules:
    def test_copies_when_installed_version_older(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        (src_dir / "llm-router.md").write_text(
            "<!-- llm-router-rules-version: 3 -->\n# New rules\n"
        )
        (dst_dir / "llm-router.md").write_text(
            "<!-- llm-router-rules-version: 1 -->\n# Old rules\n"
        )

        monkeypatch.setattr("llm_router.install_hooks._RULES_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._RULES_DST", dst_dir)

        msg = check_and_update_rules()
        assert msg is not None
        assert "1 → 3" in msg or "v1" in msg
        assert (dst_dir / "llm-router.md").read_text().startswith(
            "<!-- llm-router-rules-version: 3 -->"
        )

    def test_no_update_when_versions_equal(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        content = "<!-- llm-router-rules-version: 2 -->\n# Rules\n"
        (src_dir / "llm-router.md").write_text(content)
        (dst_dir / "llm-router.md").write_text(content)

        monkeypatch.setattr("llm_router.install_hooks._RULES_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._RULES_DST", dst_dir)

        assert check_and_update_rules() is None

    def test_no_update_when_installed_newer(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        (src_dir / "llm-router.md").write_text(
            "<!-- llm-router-rules-version: 1 -->\n# Old\n"
        )
        (dst_dir / "llm-router.md").write_text(
            "<!-- llm-router-rules-version: 5 -->\n# Newer\n"
        )

        monkeypatch.setattr("llm_router.install_hooks._RULES_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._RULES_DST", dst_dir)

        assert check_and_update_rules() is None

    def test_copies_when_installed_has_no_version(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        (src_dir / "llm-router.md").write_text(
            "<!-- llm-router-rules-version: 2 -->\n# New\n"
        )
        (dst_dir / "llm-router.md").write_text("# Old rules without version\n")

        monkeypatch.setattr("llm_router.install_hooks._RULES_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._RULES_DST", dst_dir)

        msg = check_and_update_rules()
        assert msg is not None

    def test_no_op_when_src_missing(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        monkeypatch.setattr("llm_router.install_hooks._RULES_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._RULES_DST", dst_dir)

        assert check_and_update_rules() is None


class TestCheckAndUpdateHooks:
    def test_restores_missing_managed_hook(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        hook_content = "#!/usr/bin/env python3\n# llm-router-hook-version: 7\nprint('ok')\n"
        (src_dir / "auto-route.py").write_text(hook_content)

        monkeypatch.setattr(
            "llm_router.install_hooks._HOOK_DEFS",
            [("auto-route.py", "llm-router-auto-route.py", "UserPromptSubmit", "")],
        )
        monkeypatch.setattr("llm_router.install_hooks._HOOKS_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._HOOKS_DST", dst_dir)

        updates = check_and_update_hooks()

        restored = dst_dir / "llm-router-auto-route.py"
        assert restored.exists()
        assert restored.read_text() == hook_content
        assert os.access(restored, os.X_OK)
        assert updates == ["Restored missing llm-router-auto-route.py v7"]

    def test_updates_managed_legacy_alias(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        src_content = "#!/usr/bin/env python3\n# llm-router-hook-version: 8\nprint('new')\n"
        old_alias = "#!/usr/bin/env python3\n# llm-router-hook-version: 7\nprint('old')\n"
        (src_dir / "auto-route.py").write_text(src_content)
        (dst_dir / "llm-router-auto-route.py").write_text(src_content)
        (dst_dir / "auto-route.py").write_text(old_alias)

        monkeypatch.setattr(
            "llm_router.install_hooks._HOOK_DEFS",
            [("auto-route.py", "llm-router-auto-route.py", "UserPromptSubmit", "")],
        )
        monkeypatch.setattr("llm_router.install_hooks._HOOKS_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._HOOKS_DST", dst_dir)
        monkeypatch.setattr("llm_router.install_hooks._load_settings", lambda: {})

        updates = check_and_update_hooks()

        assert (dst_dir / "auto-route.py").read_text() == src_content
        assert updates == ["Updated legacy alias auto-route.py v7 → v8"]

    def test_restores_legacy_alias_when_settings_reference_it(self, tmp_path, monkeypatch):
        src_dir = tmp_path / "src"
        dst_dir = tmp_path / "dst"
        src_dir.mkdir()
        dst_dir.mkdir()

        src_content = "#!/usr/bin/env python3\n# llm-router-hook-version: 8\nprint('new')\n"
        (src_dir / "auto-route.py").write_text(src_content)
        (dst_dir / "llm-router-auto-route.py").write_text(src_content)
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {dst_dir / 'auto-route.py'}",
                            }
                        ],
                    }
                ]
            }
        }

        monkeypatch.setattr(
            "llm_router.install_hooks._HOOK_DEFS",
            [("auto-route.py", "llm-router-auto-route.py", "UserPromptSubmit", "")],
        )
        monkeypatch.setattr("llm_router.install_hooks._HOOKS_SRC", src_dir)
        monkeypatch.setattr("llm_router.install_hooks._HOOKS_DST", dst_dir)
        monkeypatch.setattr("llm_router.install_hooks._load_settings", lambda: settings)

        updates = check_and_update_hooks()

        assert (dst_dir / "auto-route.py").read_text() == src_content
        assert updates == ["Restored legacy alias auto-route.py v8"]


class TestRegisterHook:
    def test_dedupes_same_script_with_different_python_paths(self):
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/tmp/venv/bin/python3 /Users/yali.pollak/.claude/hooks/llm-router-auto-route.py",
                            }
                        ],
                    },
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/tmp/venv/bin/python /Users/yali.pollak/.claude/hooks/llm-router-auto-route.py",
                            }
                        ],
                    },
                ]
            }
        }

        status = _register_hook(
            settings,
            "UserPromptSubmit",
            "",
            "/Users/yali.pollak/Projects/llm-router/.venv/bin/python /Users/yali.pollak/.claude/hooks/llm-router-auto-route.py",
        )

        assert status == "updated"
        hooks = settings["hooks"]["UserPromptSubmit"]
        assert len(hooks) == 1
        assert hooks[0]["hooks"][0]["command"] == (
            "/Users/yali.pollak/Projects/llm-router/.venv/bin/python "
            "/Users/yali.pollak/.claude/hooks/llm-router-auto-route.py"
        )
