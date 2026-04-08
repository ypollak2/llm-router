from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "release_guard.py"

spec = importlib.util.spec_from_file_location("release_guard", MODULE_PATH)
release_guard = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = release_guard
spec.loader.exec_module(release_guard)


def _write_release_files(root: Path, *, version: str, marketplace_version: str | None = None) -> None:
    marketplace_version = marketplace_version or version

    (root / "pyproject.toml").write_text(
        "[project]\nname = \"claude-code-llm-router\"\nversion = "
        f"\"{version}\"\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        "[[package]]\nname = \"claude-code-llm-router\"\nversion = "
        f"\"{version}\"\n",
        encoding="utf-8",
    )
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        f'{{"name":"llm-router","version":"{version}"}}\n',
        encoding="utf-8",
    )
    (root / ".claude-plugin" / "marketplace.json").write_text(
        '{"plugins":[{"name":"llm-router","version":"'
        f'{marketplace_version}'
        '"}]}\n',
        encoding="utf-8",
    )
    (root / "glama.json").write_text(f'{{"version":"{version}"}}\n', encoding="utf-8")
    (root / "mcp-registry.json").write_text(f'{{"version":"{version}"}}\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "- Pending work.\n\n"
        f"## v{version} — Example release\n\n"
        "- Shipped change.\n",
        encoding="utf-8",
    )


def test_get_latest_released_section_requires_unreleased() -> None:
    text = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "- Pending.\n\n"
        "## v2.2.0 — Explainable Routing\n\n"
        "- Added release.\n"
    )

    section = release_guard.get_latest_released_section(text)

    assert section.version == "2.2.0"
    assert section.title == "Explainable Routing"


def test_check_changed_files_requires_changelog_and_readme() -> None:
    changed = ["src/llm_router/repo_config.py"]

    errors = release_guard.check_changed_files(changed)

    assert any("CHANGELOG.md" in error for error in errors)
    assert any("README.md" in error for error in errors)


def test_check_version_sync_accepts_synced_files(tmp_path: Path) -> None:
    _write_release_files(tmp_path, version="2.2.0")

    errors = release_guard.check_version_sync(tmp_path)

    assert errors == []


def test_check_version_sync_rejects_mismatched_marketplace_version(tmp_path: Path) -> None:
    _write_release_files(tmp_path, version="2.2.0", marketplace_version="1.3.0")

    errors = release_guard.check_version_sync(tmp_path)

    assert any("marketplace" in error.lower() for error in errors)


def test_release_notes_extraction_uses_matching_section() -> None:
    text = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "- Pending.\n\n"
        "## v2.2.0 — Explainable Routing\n\n"
        "### Added\n\n"
        "- One.\n\n"
        "## v2.1.0 — Prior release\n\n"
        "- Old.\n"
    )

    section = release_guard.get_release_section(text, "v2.2.0")

    assert "### Added" in section.body
    assert section.title == "Explainable Routing"
