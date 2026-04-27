#!/usr/bin/env python3
"""Automatically sync version across all project files.

Uses pyproject.toml as the source of truth and updates:
- .claude-plugin/plugin.json
- .claude-plugin/marketplace.json
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


def sync_plugin_json(project_root: Path, version: str) -> bool:
    """Update version in plugin.json."""
    plugin_path = project_root / ".claude-plugin" / "plugin.json"
    if not plugin_path.exists():
        print(f"⚠️  plugin.json not found at {plugin_path}, skipping")
        return False

    with open(plugin_path) as f:
        data = json.load(f)

    if data["version"] == version:
        print(f"✅ plugin.json already at {version}")
        return False

    data["version"] = version
    with open(plugin_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Updated plugin.json to {version}")
    return True


def sync_marketplace_json(project_root: Path, version: str) -> bool:
    """Update version in marketplace.json."""
    marketplace_path = project_root / ".claude-plugin" / "marketplace.json"
    if not marketplace_path.exists():
        print(f"⚠️  marketplace.json not found at {marketplace_path}, skipping")
        return False

    with open(marketplace_path) as f:
        data = json.load(f)

    changed = False

    # Update plugin version
    if data["plugins"][0]["version"] != version:
        data["plugins"][0]["version"] = version
        changed = True

    # Update root version
    if data["version"] != version:
        data["version"] = version
        changed = True

    if not changed:
        print(f"✅ marketplace.json already at {version}")
        return False

    with open(marketplace_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Updated marketplace.json to {version}")
    return True


def main():
    """Sync versions across all files."""
    project_root = Path(__file__).parent.parent

    try:
        # Read authoritative version
        version = read_pyproject_version(project_root)
        print(f"📋 Syncing all versions to {version}")
        print("=" * 50)

        # Update all files
        changes = []
        changes.append(sync_plugin_json(project_root, version))
        changes.append(sync_marketplace_json(project_root, version))

        print("=" * 50)

        if any(changes):
            print(f"✅ Version sync complete!")
            return 0
        else:
            print(f"✅ All versions already in sync!")
            return 0

    except Exception as e:
        print(f"❌ Error syncing versions: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
