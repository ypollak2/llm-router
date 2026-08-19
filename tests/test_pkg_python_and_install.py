"""Regression: CHZ-PKG-007/008 + CHZ-PY-001/002 (packaging + Python floor)."""
import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_requires_python_is_3_11_and_no_stale_310():
    d = tomllib.load(open(ROOT / "pyproject.toml", "rb"))
    assert d["project"]["requires-python"] == ">=3.11"
    classifiers = d["project"]["classifiers"]
    assert not any("3.10" in c for c in classifiers), "stale 3.10 classifier"
    assert any("3.11" in c for c in classifiers)


def test_pkg008_malformed_settings_backed_up(tmp_path, monkeypatch):
    import llm_router.install_hooks as ih
    monkeypatch.setattr(ih, "_SETTINGS_PATH", tmp_path / "settings.json")
    ih._SETTINGS_PATH.write_text("{ not: valid json,,,")
    original = ih._SETTINGS_PATH.read_text()
    ih._save_settings({"hooks": {"llm_router": True}})
    assert json.loads(ih._SETTINGS_PATH.read_text()) == {"hooks": {"llm_router": True}}
    baks = list(tmp_path.glob("settings.json.corrupt.*.bak"))
    assert baks and baks[0].read_text() == original, "malformed file not backed up"


def test_pkg008_valid_settings_not_spammed_with_backups(tmp_path, monkeypatch):
    import llm_router.install_hooks as ih
    monkeypatch.setattr(ih, "_SETTINGS_PATH", tmp_path / "settings.json")
    ih._SETTINGS_PATH.write_text(json.dumps({"a": 1}))
    ih._save_settings({"a": 2})
    assert not list(tmp_path.glob("*.bak"))


def test_pkg007_install_help_is_inert(tmp_path, monkeypatch, capsys):
    # Point HOME at an empty dir; --help must make NO install changes.
    monkeypatch.setenv("HOME", str(tmp_path))
    from llm_router.commands.install import cmd_install
    rc = cmd_install(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage" in out and "install" in out
    # No install artifacts should have been created under the fake HOME.
    assert not (tmp_path / ".claude" / "hooks").exists(), "install --help performed a real install"
