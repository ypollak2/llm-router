"""Regression: RED2-5-01/02 — uninstall must remove everything install created.

Previously `uninstall()` left `llm_router-statusline.sh` on disk AND its `statusLine`
registration in settings.json (Claude Code kept executing it forever), and both
`uninstall()` and `uninstall_claw_code()` left the `_SIDECAR_SCRIPTS` orphaned;
`uninstall_claw_code()` also left the `LLM_ROUTER_CLAW_CODE=true` marker in
`~/.claw-code/.env` so the host still believed llm_router was active.
"""
from __future__ import annotations

import json

import llm_router.install_hooks as ih


def test_uninstall_removes_statusline_and_sidecars(tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    rules = tmp_path / "rules"
    hooks.mkdir(parents=True)
    rules.mkdir(parents=True)
    settings_path = tmp_path / "settings.json"

    monkeypatch.setattr(ih, "_CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(ih, "_HOOKS_DST", hooks)
    monkeypatch.setattr(ih, "_RULES_DST", rules)
    monkeypatch.setattr(ih, "_SETTINGS_PATH", settings_path)
    # No Claude Desktop / CLI side effects in this hermetic test.
    monkeypatch.setattr(ih, "_uninstall_claude_desktop", lambda: [])
    monkeypatch.setattr(ih, "_uninstall_claude_code_cli", lambda: [])

    # Plant what install() would have created.
    statusline = hooks / "llm_router-statusline.sh"
    statusline.write_text("#!/bin/bash\necho llm_router")
    for name in ih._SIDECAR_SCRIPTS:
        (hooks / name).write_text("#!/bin/bash\n")
    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": f"bash {statusline}"},
    }))

    actions = ih.uninstall()

    assert not statusline.exists(), "RED2-5-01: statusline script not removed"
    for name in ih._SIDECAR_SCRIPTS:
        assert not (hooks / name).exists(), f"RED2-5-02: sidecar {name} not removed"
    settings_after = json.loads(settings_path.read_text())
    assert "statusLine" not in settings_after, "RED2-5-01: statusLine key not removed"
    assert any("statusLine" in a for a in actions)


def test_uninstall_preserves_a_foreign_statusline(tmp_path, monkeypatch):
    """A statusLine the user set themselves (not llm_router's) must be left alone."""
    hooks = tmp_path / "hooks"
    hooks.mkdir(parents=True)
    (tmp_path / "rules").mkdir(parents=True)
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(ih, "_CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(ih, "_HOOKS_DST", hooks)
    monkeypatch.setattr(ih, "_RULES_DST", tmp_path / "rules")
    monkeypatch.setattr(ih, "_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(ih, "_uninstall_claude_desktop", lambda: [])
    monkeypatch.setattr(ih, "_uninstall_claude_code_cli", lambda: [])

    settings_path.write_text(json.dumps({
        "statusLine": {"type": "command", "command": "bash /usr/local/bin/my-own.sh"},
    }))
    ih.uninstall()
    after = json.loads(settings_path.read_text())
    assert after.get("statusLine", {}).get("command") == "bash /usr/local/bin/my-own.sh"


def test_uninstall_claw_code_removes_sidecars_and_flag(tmp_path, monkeypatch):
    cc = tmp_path / ".claw-code"
    hooks = cc / "hooks"
    hooks.mkdir(parents=True)
    monkeypatch.setattr(ih, "_claw_code_dir", lambda: cc)

    for name in ih._SIDECAR_SCRIPTS:
        (hooks / name).write_text("#!/bin/bash\n")
    env_path = cc / ".env"
    env_path.write_text("SOME_OTHER=1\nLLM_ROUTER_CLAW_CODE=true\nKEEP=yes\n")
    (cc / "settings.json").write_text(json.dumps({"mcpServers": {}}))

    ih.uninstall_claw_code()

    for name in ih._SIDECAR_SCRIPTS:
        assert not (hooks / name).exists(), f"RED2-5-02: claw-code sidecar {name} not removed"
    env_after = env_path.read_text()
    assert "LLM_ROUTER_CLAW_CODE" not in env_after, "RED2-5-02: flag not removed"
    assert "SOME_OTHER=1" in env_after and "KEEP=yes" in env_after, "unrelated env lines dropped"
