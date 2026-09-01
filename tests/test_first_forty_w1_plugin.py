"""The plugin bundle must actually deliver the product (First Forty, W1).

Before these tests the published plugin was a shell:

  * `.claude-plugin/plugin.json` declared `skills/` and nothing else — no MCP
    server, no hooks, so installing it gave four skills and no routing.
  * `.codex-plugin/plugin.json` declared `"mcpServers": ".mcp.json"` while that
    file did not exist anywhere in the repo.
  * There was no `hooks/` directory at all, so no host could receive auto-routing
    through the plugin even in principle.

The marketplace is the only channel where a stranger finds llm-router without
already knowing its name. These assertions are what makes that channel honest.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

MANIFESTS = [
    REPO / ".claude-plugin" / "plugin.json",
    REPO / ".codex-plugin" / "plugin.json",
]


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_declares_the_engine(manifest_path):
    """A manifest that ships skills but no server or hooks is not the product."""
    manifest = json.loads(manifest_path.read_text())

    for key in ("mcpServers", "hooks", "commands"):
        assert key in manifest, (
            f"{manifest_path.parent.name}/plugin.json does not declare {key!r} — "
            "installing this plugin would not deliver routing"
        )


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.parent.name)
def test_every_manifest_reference_resolves(manifest_path):
    """Every path a manifest points at must exist.

    `.codex-plugin/plugin.json` shipped pointing at a `.mcp.json` that was never
    committed. Nothing caught it because nothing checked.
    """
    manifest = json.loads(manifest_path.read_text())
    # Manifest paths resolve against the PLUGIN ROOT — the repo root — not the
    # metadata directory. The pre-existing `"skills": "skills/"` entry is the
    # proof, and accepting either root would let a broken path pass.
    for key in ("skills", "mcpServers", "hooks", "commands"):
        ref = manifest.get(key)
        if not isinstance(ref, str):
            continue
        assert (REPO / ref).exists(), (
            f"{manifest_path.parent.name}/plugin.json declares {key}={ref!r} "
            f"but {REPO / ref} does not exist"
        )


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_targets_are_not_gitignored(manifest_path):
    """A shipped path that git refuses to commit is the same bug in a new place.

    Root cause of the broken Codex plugin: the manifest pointed at `.mcp.json`,
    which .gitignore excludes as a developer's "Local MCP config". The file
    existed on the author's machine and was never in the repo, so the published
    plugin referenced nothing. Passing the reference-resolves test locally would
    NOT have caught that — only asking git does.
    """
    manifest = json.loads(manifest_path.read_text())
    refs = [
        manifest[k]
        for k in ("skills", "mcpServers", "hooks", "commands")
        if isinstance(manifest.get(k), str)
    ]
    result = subprocess.run(
        ["git", "check-ignore", "-v", *refs],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    # check-ignore exits 0 when it matched something — i.e. when a shipped path
    # is ignored, which is the failure we are hunting.
    assert result.returncode != 0, (
        f"{manifest_path.parent.name}/plugin.json points at gitignored paths, so "
        f"they will be missing from a clone:\n{result.stdout}"
    )


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.parent.name)
def test_declared_interface_assets_exist(manifest_path):
    """`interface.logo` is a declared path like any other, and it was dangling.

    The first version of test_every_manifest_reference_resolves checked
    `skills`, `mcpServers`, `hooks` and `commands` — and missed `interface`,
    so `assets/logo.png` was declared by both manifests while existing nowhere
    in the repo. Exactly the `.mcp.json` failure, one nesting level deeper,
    surviving the test written to catch that class of bug.
    """
    manifest = json.loads(manifest_path.read_text())
    interface = manifest.get("interface") or {}

    for key in ("logo", "icon", "banner"):
        ref = interface.get(key)
        if not isinstance(ref, str) or ref.startswith(("http://", "https://")):
            continue
        target = REPO / ref
        assert target.is_file(), (
            f"{manifest_path.parent.name}/plugin.json declares interface.{key}="
            f"{ref!r}, but {target} does not exist"
        )
        assert target.stat().st_size > 0, f"{ref} is an empty file"


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.parent.name)
def test_every_skill_has_frontmatter(manifest_path):
    """A skill without frontmatter has no name or description for a host to show."""
    manifest = json.loads(manifest_path.read_text())
    skills_ref = manifest.get("skills")
    if not isinstance(skills_ref, str):
        pytest.skip("manifest declares no skills directory")

    skill_files = sorted((REPO / skills_ref).glob("*/SKILL.md"))
    assert skill_files, f"no SKILL.md files under {skills_ref}"

    for path in skill_files:
        text = path.read_text()
        assert text.startswith("---\n"), (
            f"{path.relative_to(REPO)} has no frontmatter block, so a host has "
            "no name or description to display for it"
        )
        block = text.split("---", 2)[1]
        for field_name in ("name:", "description:"):
            assert field_name in block, (
                f"{path.relative_to(REPO)} frontmatter is missing {field_name!r}"
            )


def test_mcp_json_exists_and_names_the_server():
    mcp = REPO / ".claude-plugin" / "mcp.json"
    assert mcp.is_file(), "plugin mcp.json is missing — the manifests point at it"
    data = json.loads(mcp.read_text())
    assert "llm_router" in data.get("mcpServers", {}), (
        ".mcp.json must register the llm_router server under its canonical name; "
        "the hooks resolve tool names against it"
    )


def test_hooks_json_covers_the_routing_events():
    """The events that make routing automatic must all be wired."""
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]

    # UserPromptSubmit is the one that makes routing automatic rather than manual;
    # PreToolUse is what holds a tool until the route is satisfied.
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        assert event in hooks, f"hooks.json does not handle {event}"

    commands = [
        h["command"]
        for groups in hooks.values()
        for g in groups
        for h in g["hooks"]
    ]
    assert any("auto-route" in c for c in commands), "no auto-route hook registered"
    assert any("enforce-route" in c for c in commands), "no enforcement hook registered"


def test_every_hook_command_points_at_a_shipped_file():
    """A hooks.json entry naming a script the bundle does not contain is dead."""
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]

    for groups in hooks.values():
        for group in groups:
            for handler in group["hooks"]:
                cmd = handler["command"]
                assert cmd.startswith("${CLAUDE_PLUGIN_ROOT}/"), (
                    f"hook command {cmd!r} is not plugin-root relative, so it "
                    "breaks the moment the plugin is installed anywhere else"
                )
                rel = cmd.replace("${CLAUDE_PLUGIN_ROOT}/", "")
                assert (REPO / rel).is_file(), f"hooks.json points at missing file: {rel}"


def test_commands_are_present_and_have_frontmatter():
    cmd_dir = REPO / "commands"
    assert cmd_dir.is_dir(), "no commands/ directory"
    found = sorted(p.stem for p in cmd_dir.glob("*.md"))
    assert {"status", "savings", "policy", "doctor"} <= set(found), (
        f"expected the core slash commands, found {found}"
    )
    for path in cmd_dir.glob("*.md"):
        text = path.read_text()
        assert text.startswith("---\n"), f"{path.name} has no frontmatter block"
        assert "description:" in text.split("---")[1], f"{path.name} has no description"


def test_bundle_is_not_stale():
    """CI fails on manifest drift instead of shipping a broken plugin.

    This is the check that would have caught the missing .mcp.json: the bundle is
    generated from _HOOK_DEFS, so a hook added to the table and not rebuilt into
    the manifests is a failure here rather than a silent hole in the plugin.
    """
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "build_plugin_bundle.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, (
        "plugin bundle is out of date — run scripts/build_plugin_bundle.py\n"
        f"{result.stdout}{result.stderr}"
    )
