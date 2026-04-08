#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]

PACKAGE_NAME = "claude-code-llm-router"
CHANGELOG_PATH = "CHANGELOG.md"
README_PATH = "README.md"

SYNC_TARGETS = (
    ("pyproject.toml", "package version", "pyproject"),
    ("uv.lock", "lockfile package version", "uv_lock"),
    (".claude-plugin/plugin.json", "Claude plugin version", "plugin"),
    (".claude-plugin/marketplace.json", "Claude marketplace version", "marketplace"),
    ("glama.json", "Glama registry version", "glama"),
    ("mcp-registry.json", "MCP registry version", "mcp_registry"),
)

CHANGELOG_REQUIRED_PATTERNS = (
    "src/llm_router/cli.py",
    "src/llm_router/config.py",
    "src/llm_router/install_hooks.py",
    "src/llm_router/repo_config.py",
    "src/llm_router/router.py",
    "src/llm_router/server.py",
    "src/llm_router/hooks/**",
    "src/llm_router/tools/**",
    ".claude-plugin/**",
    "glama.json",
    "mcp-registry.json",
    "smithery.yaml",
)

README_REQUIRED_PATTERNS = (
    "src/llm_router/cli.py",
    "src/llm_router/install_hooks.py",
    "src/llm_router/repo_config.py",
    "src/llm_router/server.py",
    "src/llm_router/tools/**",
    ".claude-plugin/**",
    "glama.json",
    "mcp-registry.json",
    "smithery.yaml",
)

CHECKLIST_LINES = (
    "`CHANGELOG.md` — always update for user-facing hooks, tools, install flow, config, or metadata changes.",
    "`README.md` — update when install, setup, configuration, tool surface, or roadmap status changes.",
    "Versioned release files — keep `pyproject.toml`, `uv.lock`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `glama.json`, and `mcp-registry.json` on the same released version.",
    "Release outputs — a tag should produce both a PyPI publish and a GitHub Release.",
)


@dataclass
class ChangelogSection:
    raw_name: str
    version: str | None
    title: str | None
    body: str


def normalize_version(value: str) -> str:
    return value[1:] if value.startswith("v") else value


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_pyproject_version(path: Path) -> str:
    data = tomllib.loads(read_text(path))
    return str(data["project"]["version"])


def read_uv_lock_version(path: Path) -> str:
    lines = read_text(path).splitlines()
    in_package_block = False
    current_name: str | None = None

    for line in lines:
        stripped = line.strip()
        if stripped == "[[package]]":
            in_package_block = True
            current_name = None
            continue
        if not in_package_block:
            continue
        if stripped.startswith("name = "):
            name_match = re.match(r'^name = "([^"]+)"$', stripped)
            current_name = name_match.group(1) if name_match else None
            continue
        if current_name != PACKAGE_NAME:
            continue
        version_match = re.match(r'^version = "([^"]+)"$', stripped)
        if version_match:
            return version_match.group(1)
    raise ValueError(f"Could not find {PACKAGE_NAME} in {path}")


def read_json_version(path: Path) -> str:
    data = json.loads(read_text(path))
    return str(data["version"])


def read_marketplace_version(path: Path) -> str:
    data = json.loads(read_text(path))
    plugins = data.get("plugins", [])
    if not plugins:
        raise ValueError(f"No plugins array found in {path}")
    return str(plugins[0]["version"])


def parse_changelog_sections(text: str) -> list[ChangelogSection]:
    heading_re = re.compile(
        r"^## (?P<name>Unreleased|v?(?P<version>\d+\.\d+\.\d+))(?: — (?P<title>.+))?$"
    )
    sections: list[ChangelogSection] = []
    current_name: str | None = None
    current_version: str | None = None
    current_title: str | None = None
    current_body: list[str] = []

    for line in text.splitlines():
        match = heading_re.match(line)
        if match:
            if current_name is not None:
                sections.append(
                    ChangelogSection(
                        raw_name=current_name,
                        version=current_version,
                        title=current_title,
                        body="\n".join(current_body).strip(),
                    )
                )
            current_name = match.group("name")
            current_version = normalize_version(match.group("version")) if match.group("version") else None
            current_title = match.group("title")
            current_body = []
            continue
        if current_name is not None:
            current_body.append(line)

    if current_name is not None:
        sections.append(
            ChangelogSection(
                raw_name=current_name,
                version=current_version,
                title=current_title,
                body="\n".join(current_body).strip(),
            )
        )
    return sections


def get_latest_released_section(text: str) -> ChangelogSection:
    sections = parse_changelog_sections(text)
    if not sections:
        raise ValueError("CHANGELOG.md has no level-2 sections")
    if sections[0].raw_name != "Unreleased":
        raise ValueError("CHANGELOG.md must start with `## Unreleased`")
    for section in sections[1:]:
        if section.version:
            return section
    raise ValueError("CHANGELOG.md does not contain a released version after `## Unreleased`")


def get_release_section(text: str, version: str) -> ChangelogSection:
    target = normalize_version(version)
    for section in parse_changelog_sections(text):
        if section.version == target:
            return section
    raise ValueError(f"Could not find CHANGELOG section for v{target}")


def load_versions(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    values["pyproject"] = read_pyproject_version(root / "pyproject.toml")
    values["uv_lock"] = read_uv_lock_version(root / "uv.lock")
    values["plugin"] = read_json_version(root / ".claude-plugin" / "plugin.json")
    values["marketplace"] = read_marketplace_version(root / ".claude-plugin" / "marketplace.json")
    values["glama"] = read_json_version(root / "glama.json")
    values["mcp_registry"] = read_json_version(root / "mcp-registry.json")
    return values


def check_version_sync(root: Path) -> list[str]:
    errors: list[str] = []
    values = load_versions(root)
    source_version = values["pyproject"]

    for path_label, human_label, key in SYNC_TARGETS[1:]:
        if values[key] != source_version:
            errors.append(
                f"{human_label} in {path_label} is {values[key]}, expected {source_version} from pyproject.toml."
            )

    latest_released = get_latest_released_section(read_text(root / CHANGELOG_PATH))
    if latest_released.version != source_version:
        errors.append(
            f"Latest released CHANGELOG section is v{latest_released.version}, expected v{source_version}."
        )

    return errors


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def collect_changed_files(root: Path, base_ref: str, head_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base_ref, head_ref],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_changed_files(changed_files: list[str]) -> list[str]:
    errors: list[str] = []
    changed_set = set(changed_files)

    if any(matches_any(path, CHANGELOG_REQUIRED_PATTERNS) for path in changed_files):
        if CHANGELOG_PATH not in changed_set:
            errors.append(
                "User-facing files changed but CHANGELOG.md was not updated. "
                "Add the change under `## Unreleased`."
            )

    if any(matches_any(path, README_REQUIRED_PATTERNS) for path in changed_files):
        if README_PATH not in changed_set:
            errors.append(
                "Install/config/public-surface files changed but README.md was not updated."
            )

    return errors


def print_checklist() -> None:
    print("Release checklist:")
    for line in CHECKLIST_LINES:
        print(f"- {line}")


def cmd_sync(args: argparse.Namespace) -> int:
    errors = check_version_sync(Path(args.root))
    if errors:
        print("Release sync check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print_checklist()
        return 1

    versions = load_versions(Path(args.root))
    print("Release sync OK")
    for path_label, human_label, key in SYNC_TARGETS:
        print(f"- {human_label}: {versions[key]}")
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    changed_files = collect_changed_files(Path(args.root), args.base, args.head)
    if not changed_files:
        print("No changed files detected for release hygiene check.")
        return 0

    errors = check_changed_files(changed_files)
    if errors:
        print("Release hygiene check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print("\nChanged files:", file=sys.stderr)
        for path in changed_files:
            print(f"- {path}", file=sys.stderr)
        print("", file=sys.stderr)
        print_checklist()
        return 1

    print("Release hygiene OK")
    print_checklist()
    return 0


def cmd_title(args: argparse.Namespace) -> int:
    section = get_release_section(read_text(Path(args.root) / CHANGELOG_PATH), args.version)
    heading = f"v{section.version}"
    if section.title:
        heading = f"{heading} — {section.title}"
    print(heading)
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    section = get_release_section(read_text(Path(args.root) / CHANGELOG_PATH), args.version)
    body = section.body.strip()
    if args.output:
        Path(args.output).write_text(body + "\n", encoding="utf-8")
    else:
        print(body)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release hygiene guard for llm-router.")
    parser.add_argument("--root", default=str(ROOT), help="Repository root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Verify release metadata stays in sync.")
    sync_parser.set_defaults(func=cmd_sync)

    changes_parser = subparsers.add_parser(
        "changes",
        help="Verify a diff updated the required changelog/readme surfaces.",
    )
    changes_parser.add_argument("--base", required=True, help="Git base ref or SHA")
    changes_parser.add_argument("--head", required=True, help="Git head ref or SHA")
    changes_parser.set_defaults(func=cmd_changes)

    title_parser = subparsers.add_parser("title", help="Print the GitHub release title for a version.")
    title_parser.add_argument("--version", required=True, help="Version string, with or without leading v")
    title_parser.set_defaults(func=cmd_title)

    notes_parser = subparsers.add_parser("notes", help="Write CHANGELOG notes for a version.")
    notes_parser.add_argument("--version", required=True, help="Version string, with or without leading v")
    notes_parser.add_argument("--output", help="Optional output file path")
    notes_parser.set_defaults(func=cmd_notes)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
