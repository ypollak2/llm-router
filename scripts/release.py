#!/usr/bin/env python3
"""Automate llm-router releases."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = ROOT / "pyproject.toml"
INIT_PATH = ROOT / "src" / "llm_router" / "__init__.py"
PLUGIN_PATH = ROOT / ".codex-plugin" / "plugin.json"
MARKETPLACE_PATH = ROOT / ".codex-plugin" / "marketplace.json"
CHANGELOG_PATH = ROOT / "CHANGELOG.md"

_PYPROJECT_VERSION_RE = re.compile(r'(?m)^version = "([^"]+)"$')
_INIT_VERSION_RE = re.compile(r'(?m)^__version__ = "([^"]+)"$')


def run(
    cmd: Sequence[str],
    *,
    cwd: Path = ROOT,
    dry_run: bool = False,
    display_cmd: Sequence[str] | None = None,
) -> None:
    rendered = " ".join(display_cmd or cmd)
    print(f"$ {rendered}")
    if dry_run:
        return
    subprocess.run(list(cmd), cwd=cwd, check=True)


def _replace_single(pattern: re.Pattern[str], text: str, replacement: str) -> str:
    updated, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise ValueError(f"Expected exactly one match for {pattern.pattern!r}")
    return updated


def update_pyproject_text(text: str, version: str) -> str:
    return _replace_single(_PYPROJECT_VERSION_RE, text, f'version = "{version}"')


def update_init_version_text(text: str, version: str) -> str:
    return _replace_single(_INIT_VERSION_RE, text, f'__version__ = "{version}"')


def update_plugin_data(data: dict, version: str) -> dict:
    updated = dict(data)
    updated["version"] = version
    return updated


def update_marketplace_data(data: dict, version: str) -> dict:
    updated = dict(data)
    if "version" in updated:
        updated["version"] = version

    plugins = [dict(plugin) for plugin in updated.get("plugins", [])]
    for plugin in plugins:
        if plugin.get("name") == "llm-router":
            plugin["version"] = version
    updated["plugins"] = plugins
    return updated


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def bump_versions(version: str, *, root: Path = ROOT) -> None:
    pyproject_path = root / "pyproject.toml"
    init_path = root / "src" / "llm_router" / "__init__.py"
    plugin_path = root / ".codex-plugin" / "plugin.json"
    marketplace_path = root / ".codex-plugin" / "marketplace.json"

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    pyproject_path.write_text(update_pyproject_text(pyproject_text, version), encoding="utf-8")

    init_text = init_path.read_text(encoding="utf-8")
    init_path.write_text(update_init_version_text(init_text, version), encoding="utf-8")

    plugin_data = json.loads(plugin_path.read_text(encoding="utf-8"))
    _write_json(plugin_path, update_plugin_data(plugin_data, version))

    marketplace_data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    _write_json(marketplace_path, update_marketplace_data(marketplace_data, version))


def read_versions(*, root: Path = ROOT) -> dict[str, str]:
    pyproject_path = root / "pyproject.toml"
    init_path = root / "src" / "llm_router" / "__init__.py"
    plugin_path = root / ".codex-plugin" / "plugin.json"
    marketplace_path = root / ".codex-plugin" / "marketplace.json"

    pyproject_version = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]["version"]
    init_match = _INIT_VERSION_RE.search(init_path.read_text(encoding="utf-8"))
    if init_match is None:
        raise ValueError(f"Could not find __version__ in {init_path}")

    plugin_version = json.loads(plugin_path.read_text(encoding="utf-8"))["version"]
    marketplace_data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    marketplace_version = None
    for plugin in marketplace_data.get("plugins", []):
        if plugin.get("name") == "llm-router":
            marketplace_version = plugin.get("version")
            break
    if marketplace_version is None:
        raise ValueError("Could not find llm-router entry in marketplace.json")

    return {
        "pyproject": pyproject_version,
        "__init__": init_match.group(1),
        "plugin.json": plugin_version,
        "marketplace.json": marketplace_version,
    }


def verify_versions(version: str, *, root: Path = ROOT) -> dict[str, str]:
    versions = read_versions(root=root)
    mismatches = {name: value for name, value in versions.items() if value != version}
    if mismatches:
        rendered = ", ".join(f"{name}={value}" for name, value in mismatches.items())
        raise ValueError(f"Version mismatch for {version}: {rendered}")
    return versions


def extract_changelog_entry(version: str, *, changelog_path: Path = CHANGELOG_PATH) -> str:
    changelog = changelog_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(?ms)^## v{re.escape(version)}\b.*?(?=^## v|\Z)"
    )
    match = pattern.search(changelog)
    if match is None:
        raise ValueError(f"Could not find changelog entry for v{version}")
    return match.group(0).strip()


def get_pypi_token(*, env: dict[str, str] | None = None, pypirc_path: Path | None = None) -> str:
    env = env or os.environ
    token = env.get("PYPI_TOKEN")
    if token:
        return token

    pypirc_path = pypirc_path or Path.home() / ".pypirc"
    parser = configparser.ConfigParser()
    parser.read(pypirc_path)
    try:
        return parser["pypi"]["password"]
    except KeyError as exc:
        raise ValueError("PyPI token not found in PYPI_TOKEN or ~/.pypirc") from exc


def perform_release(
    version: str,
    *,
    dry_run: bool = False,
    skip_tests: bool = False,
    skip_publish: bool = False,
    skip_plugin_reinstall: bool = False,
) -> None:
    print(f"\n=== Releasing v{version} ===\n")

    if not skip_tests:
        print("1. Running tests and lint...")
        run(
            ["uv", "run", "pytest", "tests/", "-q", "--ignore=tests/test_agno_integration.py"],
            dry_run=dry_run,
        )
        run(["uv", "run", "ruff", "check", "src/", "tests/"], dry_run=dry_run)

    print("2. Bumping versions...")
    if dry_run:
        print("   Would update pyproject.toml, __init__.py, plugin.json, and marketplace.json")
    else:
        bump_versions(version)
        verified = verify_versions(version)
        print("   Versions:", ", ".join(f"{name}={value}" for name, value in verified.items()))

    print("3. Verifying changelog entry...")
    changelog_entry = extract_changelog_entry(version)
    print(f"   Found changelog section for v{version}")

    print("4. Building artifacts...")
    run(["rm", "-rf", "dist"], dry_run=dry_run)
    run(["uv", "build"], dry_run=dry_run)

    print("5. Staging release metadata...")
    run(
        [
            "git",
            "add",
            "pyproject.toml",
            "src/llm_router/__init__.py",
            ".codex-plugin/plugin.json",
            ".codex-plugin/marketplace.json",
            "CHANGELOG.md",
            "README.md",
            "CLAUDE.md",
            "uv.lock",
        ],
        dry_run=dry_run,
    )

    print("6. Committing and pushing...")
    run(["git", "commit", "-m", f"feat(v{version}): release"], dry_run=dry_run)
    run(["git", "push"], dry_run=dry_run)

    if not skip_publish:
        print("7. Publishing to PyPI...")
        token = get_pypi_token()
        publish_cmd = ["uv", "publish", "--token", token]
        run(
            publish_cmd,
            dry_run=dry_run,
            display_cmd=["uv", "publish", "--token", "***"],
        )

    print("8. Tagging and creating GitHub release...")
    run(["git", "tag", f"v{version}"], dry_run=dry_run)
    run(["git", "push", "origin", "--tags"], dry_run=dry_run)
    run(
        [
            "gh",
            "release",
            "create",
            f"v{version}",
            "--title",
            f"v{version}",
            "--latest",
            "--notes",
            changelog_entry,
        ],
        dry_run=dry_run,
    )

    if not skip_plugin_reinstall:
        print("9. Reinstalling Codex plugin...")
        run(["Codex", "plugin", "reinstall", "llm-router"], dry_run=dry_run)
        run(["Codex", "plugin", "list"], dry_run=dry_run)

    print(f"\nRelease flow for v{version} completed.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Version to release, for example 5.2.0")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest and ruff.")
    parser.add_argument("--skip-publish", action="store_true", help="Skip uv publish.")
    parser.add_argument(
        "--skip-plugin-reinstall",
        action="store_true",
        help="Skip Codex plugin reinstall verification.",
    )
    args = parser.parse_args(argv)

    perform_release(
        args.version,
        dry_run=args.dry_run,
        skip_tests=args.skip_tests,
        skip_publish=args.skip_publish,
        skip_plugin_reinstall=args.skip_plugin_reinstall,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
