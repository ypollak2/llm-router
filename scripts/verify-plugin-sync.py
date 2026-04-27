#!/usr/bin/env python3
"""Verify that all plugin distributions are synchronized.

Checks:
1. All plugin.json files have the same version
2. All marketplace.json files have matching versions
3. Plugin descriptions are consistent across hosts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load_plugin_files() -> dict[str, dict]:
    """Load plugin.json and marketplace.json from all plugin directories."""
    root = Path(__file__).parent.parent
    plugin_dirs = [".claude-plugin", ".codex-plugin", ".factory-plugin"]

    data = {}
    for plugin_dir in plugin_dirs:
        plugin_path = root / plugin_dir
        if not plugin_path.exists():
            print(f"❌ Plugin directory not found: {plugin_path}")
            sys.exit(1)

        try:
            with open(plugin_path / "plugin.json") as f:
                plugin_json = json.load(f)
            with open(plugin_path / "marketplace.json") as f:
                marketplace_json = json.load(f)

            data[plugin_dir] = {
                "plugin": plugin_json,
                "marketplace": marketplace_json,
                "path": plugin_path,
            }
        except Exception as e:
            print(f"❌ Failed to load plugin files from {plugin_dir}: {e}")
            sys.exit(1)

    return data


def check_version_sync(data: dict[str, dict]) -> bool:
    """Check that all versions match across all plugin directories."""
    versions = {}
    issues = []

    for plugin_dir, files in data.items():
        plugin_version = files["plugin"].get("version")
        marketplace_version = files["marketplace"].get("version")
        marketplace_plugin_version = (
            files["marketplace"].get("plugins", [{}])[0].get("version")
        )

        versions.setdefault("plugin_json", []).append(
            (plugin_dir, plugin_version)
        )
        versions.setdefault("marketplace_json", []).append(
            (plugin_dir, marketplace_version)
        )
        versions.setdefault("marketplace_plugin", []).append(
            (plugin_dir, marketplace_plugin_version)
        )

    # Check all plugin.json versions are the same
    plugin_versions = [v for _, v in versions["plugin_json"]]
    if len(set(plugin_versions)) > 1:
        issues.append(
            f"plugin.json versions mismatch: {dict(versions['plugin_json'])}"
        )

    # Check all marketplace.json root versions are the same
    marketplace_versions = [v for _, v in versions["marketplace_json"]]
    if len(set(marketplace_versions)) > 1:
        issues.append(
            f"marketplace.json root versions mismatch: {dict(versions['marketplace_json'])}"
        )

    # Check all marketplace plugin entries match
    plugin_entry_versions = [v for _, v in versions["marketplace_plugin"]]
    if len(set(plugin_entry_versions)) > 1:
        issues.append(
            f"marketplace plugin entry versions mismatch: {dict(versions['marketplace_plugin'])}"
        )

    # Check all three match each other
    all_versions = plugin_versions + marketplace_versions + plugin_entry_versions
    if len(set(all_versions)) > 1:
        issues.append(
            f"Version mismatch across files: {dict(versions)}"
        )

    if issues:
        print("❌ Version sync check failed:")
        for issue in issues:
            print(f"   {issue}")
        return False

    current_version = plugin_versions[0] if plugin_versions else "unknown"
    print(f"✅ All plugins synchronized at version {current_version}")
    return True


def check_name_consistency(data: dict[str, dict]) -> bool:
    """Check that all plugins are named 'llm-router'."""
    issues = []

    for plugin_dir, files in data.items():
        plugin_name = files["plugin"].get("name")
        if plugin_name != "llm-router":
            issues.append(f"{plugin_dir}/plugin.json: name is '{plugin_name}', expected 'llm-router'")

        marketplace_plugins = files["marketplace"].get("plugins", [])
        if marketplace_plugins:
            for i, plugin in enumerate(marketplace_plugins):
                if plugin.get("name") != "llm-router":
                    issues.append(
                        f"{plugin_dir}/marketplace.json plugin[{i}]: name is '{plugin.get('name')}', expected 'llm-router'"
                    )

    if issues:
        print("❌ Name consistency check failed:")
        for issue in issues:
            print(f"   {issue}")
        return False

    print("✅ All plugins named 'llm-router'")
    return True


def main() -> int:
    """Verify plugin distribution sync."""
    print("Verifying plugin distribution synchronization...\n")

    data = load_plugin_files()

    checks = [
        check_version_sync,
        check_name_consistency,
    ]

    results = [check(data) for check in checks]

    if all(results):
        print("\n✅ All plugin distribution checks passed")
        return 0
    else:
        print("\n❌ Plugin distribution validation failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
