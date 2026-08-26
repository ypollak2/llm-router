"""Regression: settings.json.bak held POST-install state.

`_backup_before_overwrite(_SETTINGS_PATH)` was called from the statusLine
branch, which runs late in install() — after the hook registrations and the
mcpServers entry have already been written and saved. The resulting
`settings.json.bak` therefore contained llm_router's own hooks and MCP server,
with only the statusLine still at its original value.

A file named `settings.json.bak` promises the settings as they were. A user who
reaches for it after a bad install and copies it back would silently reinstate
every llm_router hook. The backup has to be taken before the first mutation to
mean what its name says.
"""
from __future__ import annotations

import json

import pytest

import llm_router.install_hooks as ih
import llm_router.install_manifest as im


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(ih, "_HOOKS_DST", claude / "hooks")
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_SETTINGS_PATH", claude / "settings.json")
    monkeypatch.setattr(ih, "_CLAUDE_JSON_PATH", tmp_path / ".claude.json")
    monkeypatch.setattr(ih, "claude_desktop_config_path", lambda: tmp_path / "desktop.json")
    monkeypatch.setattr(im, "_manifest_path", lambda: tmp_path / ".llm-router" / "m.json")
    (tmp_path / ".llm-router").mkdir()
    import shutil as _sh
    real_which = _sh.which
    monkeypatch.setattr(ih.shutil, "which", lambda n: None if n == "claude" else real_which(n))
    return tmp_path


def _original_settings() -> dict:
    return {
        "statusLine": {"type": "command", "command": "~/bin/my-powerline.sh"},
        "mcpServers": {"someone-elses": {"command": "other"}},
    }


def test_backup_is_a_true_pre_install_snapshot(sandbox):
    settings = sandbox / ".claude" / "settings.json"
    original = _original_settings()
    settings.write_text(json.dumps(original, indent=2) + "\n")

    ih.install(force=True)

    bak = sandbox / ".claude" / "settings.json.bak"
    assert bak.exists(), "install replaced settings.json but wrote no backup"
    restored = json.loads(bak.read_text())
    assert restored == original, (
        "settings.json.bak is not the pre-install state.\n"
        f"expected: {original}\ngot:      {restored}"
    )


def test_backup_contains_none_of_llm_routers_own_writes(sandbox):
    """The concrete failure: restoring the .bak reinstated llm_router's hooks."""
    settings = sandbox / ".claude" / "settings.json"
    settings.write_text(json.dumps(_original_settings(), indent=2) + "\n")

    ih.install(force=True)

    bak = json.loads((sandbox / ".claude" / "settings.json.bak").read_text())
    assert "hooks" not in bak, "backup carries llm_router's hook registrations"
    assert "llm_router" not in bak.get("mcpServers", {}), (
        "backup carries llm_router's MCP server entry"
    )
    assert bak["mcpServers"] == {"someone-elses": {"command": "other"}}


def test_restoring_the_backup_undoes_the_install(sandbox):
    """The property a user actually relies on."""
    settings = sandbox / ".claude" / "settings.json"
    original_bytes = (json.dumps(_original_settings(), indent=2) + "\n").encode()
    settings.write_bytes(original_bytes)

    ih.install(force=True)
    bak = sandbox / ".claude" / "settings.json.bak"
    settings.write_bytes(bak.read_bytes())

    assert json.loads(settings.read_text()) == _original_settings()
