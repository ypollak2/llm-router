#!/usr/bin/env python3
"""Verify version consistency across all project files.

This script ensures that version is in sync across:
- pyproject.toml (source of truth)
- .claude-plugin/plugin.json
- .claude-plugin/marketplace.json

Fails with exit code 1 if versions don't match, enabling use in CI.
"""

import json
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib


def read_pyproject_version(project_root: Path) -> str:
    """Read version from pyproject.toml."""
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found at {pyproject_path}")

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    return data["project"]["version"]


def read_plugin_json_version(project_root: Path) -> str:
    """Read version from .claude-plugin/plugin.json."""
    plugin_path = project_root / ".claude-plugin" / "plugin.json"
    if not plugin_path.exists():
        raise FileNotFoundError(f"plugin.json not found at {plugin_path}")

    with open(plugin_path) as f:
        data = json.load(f)

    return data["version"]


def read_marketplace_json_versions(project_root: Path) -> tuple[str, str]:
    """Read versions from .claude-plugin/marketplace.json.

    Returns (plugin_version, marketplace_version).
    """
    marketplace_path = project_root / ".claude-plugin" / "marketplace.json"
    if not marketplace_path.exists():
        raise FileNotFoundError(f"marketplace.json not found at {marketplace_path}")

    with open(marketplace_path) as f:
        data = json.load(f)

    # marketplace.json has version in both the plugin object and at root level
    plugin_version = data["plugins"][0]["version"]
    marketplace_version = data["version"]

    return plugin_version, marketplace_version


def main():
    """Verify all versions are in sync."""
    project_root = Path(__file__).parent.parent

    try:
        # Read all versions
        pyproject_version = read_pyproject_version(project_root)
        plugin_version = read_plugin_json_version(project_root)
        marketplace_plugin_version, marketplace_version = read_marketplace_json_versions(project_root)

        # Check for mismatches
        versions = {
            "pyproject.toml": pyproject_version,
            ".claude-plugin/plugin.json": plugin_version,
            ".claude-plugin/marketplace.json (plugin)": marketplace_plugin_version,
            ".claude-plugin/marketplace.json (root)": marketplace_version,
        }

        print("📋 Version Sync Check")
        print("=" * 50)

        all_match = True
        for source, version in versions.items():
            status = "✅" if version == pyproject_version else "❌"
            print(f"{status} {source:<45} {version}")
            if version != pyproject_version:
                all_match = False

        print("=" * 50)

        if all_match:
            print(f"✅ All versions in sync: {pyproject_version}")
            return 0
        else:
            print(f"❌ Version mismatch detected!")
            print(f"   Expected (from pyproject.toml): {pyproject_version}")
            print(f"\n   Run 'python scripts/sync-versions.py' to fix automatically")
            return 1

    except Exception as e:
        print(f"❌ Error checking versions: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
