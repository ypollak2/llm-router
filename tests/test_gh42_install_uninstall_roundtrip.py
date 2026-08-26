"""Regression: GH#42 — uninstall must be a proper inverse of install.

Reported against a clean-slate pipx install of 13.0.2: after
`llm-router uninstall`, `~/.claude/settings.json` still carried a `statusLine`
pointing at the deleted `llm_router-statusline.sh`, and Claude Desktop still
carried the `mcpServers.llm-router` entry.

Both behaviours already had fixes that are ancestors of the v13.0.2 tag, and
the published sdist is byte-identical to the tag for install_hooks.py — so the
release pipeline is ruled out and the cause has to be a runtime condition.

These tests state the contract directly rather than re-testing the individual
fixes: run the real install(), run the real uninstall(), and require every
config file to come back byte-for-byte as it started.
"""
from __future__ import annotations

import json

import pytest

import llm_router.install_hooks as ih
import llm_router.install_manifest as im


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every install surface at a throwaway HOME."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(ih, "_HOOKS_DST", claude / "hooks")
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_SETTINGS_PATH", claude / "settings.json")
    monkeypatch.setattr(ih, "_CLAUDE_JSON_PATH", tmp_path / ".claude.json")

    desktop = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(ih, "claude_desktop_config_path", lambda: desktop)
    monkeypatch.setattr(im, "_manifest_path", lambda: tmp_path / ".llm-router" / "install-manifest.json")
    (tmp_path / ".llm-router").mkdir()

    # No shelling out to the real `claude` CLI, and a resolvable router binary.
    import shutil as _sh
    real_which = _sh.which
    monkeypatch.setattr(ih.shutil, "which", lambda n: None if n == "claude" else real_which(n))
    return tmp_path


def _snapshot(paths):
    return {p: (p.read_bytes() if p.exists() else None) for p in paths}


def test_uninstall_restores_every_config_byte_for_byte(sandbox):
    """The contract: install then uninstall is a no-op on every config file."""
    settings = sandbox / ".claude" / "settings.json"
    desktop = sandbox / "claude_desktop_config.json"
    claude_json = sandbox / ".claude.json"

    # A user who already had their own status line and their own MCP server.
    settings.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "~/bin/my-powerline.sh"},
        "mcpServers": {"someone-elses": {"command": "other"}},
    }, indent=2) + "\n")
    desktop.write_text(json.dumps({"mcpServers": {"unrelated": {"command": "x"}}}, indent=2) + "\n")

    before = _snapshot([settings, desktop, claude_json])

    ih.install(force=True)
    ih._install_claude_desktop()
    ih.uninstall()
    im.apply_uninstall()

    after = _snapshot([settings, desktop, claude_json])
    for path in before:
        assert after[path] == before[path], (
            f"{path.name} was not restored by uninstall.\n"
            f"before: {before[path]!r}\nafter:  {after[path]!r}"
        )


def test_uninstall_removes_statusline_when_user_had_none(sandbox):
    """Clean-slate case — the reporter's setup. No statusLine must be left behind."""
    settings = sandbox / ".claude" / "settings.json"
    settings.write_text(json.dumps({}, indent=2) + "\n")

    ih.install(force=True)
    assert "statusLine" in json.loads(settings.read_text()), "install did not set one — test is vacuous"

    ih.uninstall()
    im.apply_uninstall()
    assert "statusLine" not in json.loads(settings.read_text()), (
        "GH#42: statusLine left pointing at the deleted llm_router-statusline.sh"
    )


def test_uninstall_removes_claude_desktop_registration(sandbox):
    """GH#42: install registers in claude_desktop_config.json; uninstall must undo it."""
    desktop = sandbox / "claude_desktop_config.json"
    desktop.write_text(json.dumps({"mcpServers": {"unrelated": {"command": "x"}}}, indent=2) + "\n")

    ih._install_claude_desktop()
    assert "llm_router" in json.loads(desktop.read_text())["mcpServers"], "test is vacuous"

    ih.uninstall()
    servers = json.loads(desktop.read_text())["mcpServers"]
    assert "llm_router" not in servers, "GH#42: Claude Desktop registration left behind"
    assert "unrelated" in servers, "uninstall clobbered someone else's entry"


def test_statusline_removal_reports_failure_instead_of_swallowing_it(sandbox, monkeypatch):
    """A silent `except OSError: pass` is how this stayed invisible."""
    settings = sandbox / ".claude" / "settings.json"
    settings.write_text(json.dumps({}, indent=2) + "\n")
    ih.install(force=True)

    real_unlink = ih.Path.unlink

    def _boom(self, *a, **k):
        if self.name == "llm_router-statusline.sh":
            raise OSError("permission denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(ih.Path, "unlink", _boom)
    actions = ih.uninstall()
    assert any("statusline" in a.lower() and ("could not" in a.lower() or "warn" in a.lower())
               for a in actions), (
        f"a failed statusline removal must be reported, not swallowed: {actions}"
    )


def test_a_failed_unlink_does_not_abort_the_rest_of_uninstall(sandbox, monkeypatch):
    """GH#42's likely root cause: one unguarded unlink aborted everything after it.

    `dst.unlink()` in the hook-removal loop and `rules_dst.unlink()` were both
    unguarded. A single OSError raised out of uninstall(), so the statusLine
    restore and the Claude Desktop deregistration — both of which run later in
    the function — never executed. The user sees exactly the reported symptom:
    "uninstall left these two things behind", with no error saying why.
    """
    settings = sandbox / ".claude" / "settings.json"
    desktop = sandbox / "claude_desktop_config.json"
    settings.write_text(json.dumps({}, indent=2) + "\n")
    ih.install(force=True)
    ih._install_claude_desktop()

    real_unlink = ih.Path.unlink
    first_hook = ih._HOOK_DEFS[0][1]

    def _boom(self, *a, **k):
        if self.name == first_hook:
            raise OSError("device busy")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(ih.Path, "unlink", _boom)
    actions = ih.uninstall()  # must not raise

    assert any("WARN" in a and first_hook in a for a in actions), actions
    # The two things GH#42 reported, both of which live AFTER the failing unlink:
    assert "statusLine" not in json.loads(settings.read_text()), (
        "statusLine cleanup was skipped because an earlier unlink aborted uninstall"
    )
    assert "llm_router" not in json.loads(desktop.read_text()).get("mcpServers", {}), (
        "Claude Desktop deregistration was skipped for the same reason"
    )
