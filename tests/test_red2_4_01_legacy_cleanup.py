"""Regression: RED2-4-01 — pre-rebrand llm-router artifacts must be cleaned up.

The orphaned ~/.claude/rules/llm-router.md (from LLM Router's pre-rebrand identity)
declares routing a HARD CONSTRAINT, contradicting the advise-mode llm_router.md, and
was never removed by install or uninstall. install() now migrates it away and
uninstall() removes it.
"""
from __future__ import annotations

import llm_router.install_hooks as ih


def test_migrate_removes_legacy_llm_router(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "_CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(ih, "_HOOKS_DST", tmp_path / "hooks")
    monkeypatch.setattr(ih, "_RULES_DST", tmp_path / "rules")
    (tmp_path / "rules").mkdir(parents=True)
    (tmp_path / "hooks").mkdir(parents=True)
    # Plant the pre-rebrand artifacts.
    legacy_rules = tmp_path / "rules" / "llm-router.md"
    legacy_rules.write_text("ROUTING HINT = HARD CONSTRAINT")
    legacy_hook = tmp_path / "hooks" / "llm-router-auto-route.py"
    legacy_hook.write_text("# old")
    keep = tmp_path / "rules" / "llm_router.md"
    keep.write_text("advise mode")

    actions = ih._migrate_remove_legacy_llm_router()

    assert not legacy_rules.exists(), "RED2-4-01: legacy llm-router.md not removed on install"
    assert not legacy_hook.exists(), "RED2-4-01: legacy llm-router hook not removed"
    assert keep.exists(), "migration must not touch the current llm_router.md"
    assert any("llm-router" in a for a in actions)


def test_legacy_paths_enumerated(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "_HOOKS_DST", tmp_path / "hooks")
    monkeypatch.setattr(ih, "_RULES_DST", tmp_path / "rules")
    (tmp_path / "hooks").mkdir(parents=True)
    (tmp_path / "hooks" / "llm-router-session-end.py").write_text("x")
    paths = ih._legacy_llm_router_paths()
    names = {p.name for p in paths}
    assert "llm-router.md" in names
    assert "llm-router-session-end.py" in names
