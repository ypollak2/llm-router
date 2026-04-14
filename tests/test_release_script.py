"""Tests for the release automation helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release.py"


def _load_release_script():
    spec = importlib.util.spec_from_file_location("release_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_update_version_helpers():
    module = _load_release_script()
    pyproject = '[project]\nversion = "1.0.0"\n'
    init_text = '__version__ = "1.0.0"\n'

    assert 'version = "2.0.0"' in module.update_pyproject_text(pyproject, "2.0.0")
    assert '__version__ = "2.0.0"' in module.update_init_version_text(init_text, "2.0.0")


def test_bump_versions_and_verify(tmp_path):
    module = _load_release_script()
    project = tmp_path
    (project / "src" / "llm_router").mkdir(parents=True)
    (project / ".codex-plugin").mkdir()

    (project / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    (project / "src" / "llm_router" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (project / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "llm-router", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (project / ".codex-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "llm-router", "version": "1.0.0"}]}),
        encoding="utf-8",
    )

    module.bump_versions("2.1.0", root=project)
    versions = module.verify_versions("2.1.0", root=project)

    assert versions["pyproject"] == "2.1.0"
    assert versions["__init__"] == "2.1.0"
    assert versions["plugin.json"] == "2.1.0"
    assert versions["marketplace.json"] == "2.1.0"


def test_extract_changelog_entry(tmp_path):
    module = _load_release_script()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## v2.0.0 — Example (2026-04-14)\n\n"
        "- Added thing\n\n"
        "## v1.9.0 — Previous (2026-04-13)\n\n"
        "- Older thing\n",
        encoding="utf-8",
    )

    entry = module.extract_changelog_entry("2.0.0", changelog_path=changelog)
    assert "Added thing" in entry
    assert "v1.9.0" not in entry


def test_get_pypi_token_prefers_env(monkeypatch):
    module = _load_release_script()
    monkeypatch.setenv("PYPI_TOKEN", "secret-token")
    assert module.get_pypi_token() == "secret-token"
