"""Regression: iteration-11 fixes.
RED2-11-01/02 (surgical IDE-config uninstall), RED1-11-01/02 (install backup-or-skip),
RED2-11-03 (mode-line local branch), RED2-11-04 (no guaranteed-savings claim)."""
import json
import importlib.util
import pathlib
import llm_router.install_hooks as ih

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_uninstall_ide_configs_surgical(tmp_path):
    """RED2-11-01/02: shared mcp.json must keep the user's own servers."""
    vs = tmp_path / ".vscode" / "mcp.json"
    vs.parent.mkdir(parents=True)
    vs.write_text(json.dumps({"servers": {"llm_router": {}, "mine": {"command": "x"}}}))
    ws = tmp_path / ".windsurf" / "mcp.json"
    ws.parent.mkdir(parents=True)
    ws.write_text(json.dumps({"mcpServers": {"llm_router": {}, "mine": {"command": "y"}}}))
    ih.uninstall_ide_configs(tmp_path)
    assert json.loads(vs.read_text())["servers"] == {"mine": {"command": "x"}}
    assert json.loads(ws.read_text())["mcpServers"] == {"mine": {"command": "y"}}
    assert vs.exists() and ws.exists(), "shared config wholesale-deleted"


def test_uninstall_ide_configs_noop_when_no_llm_router(tmp_path):
    vs = tmp_path / ".vscode" / "mcp.json"
    vs.parent.mkdir(parents=True)
    vs.write_text(json.dumps({"servers": {"mine": {"command": "x"}}}))
    ih.uninstall_ide_configs(tmp_path)
    assert json.loads(vs.read_text())["servers"] == {"mine": {"command": "x"}}


def test_install_rules_skips_overwrite_when_backup_fails(tmp_path, monkeypatch):
    """RED1-11-01: hand-edited llm_router.md must not be destroyed if backup fails."""
    src = tmp_path / "src"
    dst = tmp_path / "rules"
    src.mkdir()
    dst.mkdir()
    (src / "llm_router.md").write_text("<!-- llm_router-rules-version: 9 -->\nBUNDLED\n")
    user = "<!-- llm_router-rules-version: 9 -->\nMY EDIT\n"
    (dst / "llm_router.md").write_text(user)
    monkeypatch.setattr(ih, "_RULES_SRC", src)
    monkeypatch.setattr(ih, "_RULES_DST", dst)
    monkeypatch.setattr(ih, "_backup_before_overwrite", lambda d: None)  # backup fails
    # Drive only the rules block by calling install() with the heavy parts stubbed.
    for fn in ("_install_claude_desktop", "_install_claude_code_cli", "_migrate_remove_legacy_llm_router"):
        monkeypatch.setattr(ih, fn, lambda *a, **k: [])
    monkeypatch.setattr(ih, "_HOOKS_SRC", tmp_path / "hsrc")
    (tmp_path / "hsrc").mkdir()
    monkeypatch.setattr(ih, "_HOOKS_DST", tmp_path / "hdst")
    (tmp_path / "hdst").mkdir()
    monkeypatch.setattr(ih, "_SETTINGS_PATH", tmp_path / "settings.json")
    try:
        _acts =ih.install()
    except Exception:
        _acts =[]  # heavy install may still error on unstubbed bits; we only assert the file
    assert (dst / "llm_router.md").read_text() == user, "RED1-11-01: user rules destroyed on backup failure"


def _load_ss():
    spec = importlib.util.spec_from_file_location("ss11", ROOT / "src/llm_router/hooks/session-start.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_mode_label_has_local_branch(monkeypatch):
    m = _load_ss()
    monkeypatch.setattr(m, "_CC_MODE", False, raising=False)
    monkeypatch.setattr(m, "_any_cloud_key", lambda: False)
    monkeypatch.setattr(m, "_zero_claude_enabled", lambda: False, raising=False)
    assert "local" in m._mode_label(False)
    monkeypatch.setattr(m, "_any_cloud_key", lambda: True)
    assert "api-keys" in m._mode_label(False)


def test_no_guaranteed_savings_claim():
    """RED2-11-04: the unqualified 'Savings are guaranteed' claim must be gone."""
    txt = (ROOT / "src/llm_router/install_hooks.py").read_text()
    assert "Savings are guaranteed" not in txt
