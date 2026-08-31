#!/usr/bin/env python3
"""Generate the plugin bundle files from the single source of truth.

Tasks 01–04 of the First Forty. The plugin manifests were hand-maintained and
drifted badly: `.claude-plugin/plugin.json` declared no MCP server and no hooks
at all, and `.codex-plugin/plugin.json` pointed `mcpServers` at a `.mcp.json`
that did not exist in the repo. Installing the plugin therefore delivered four
skills and none of the product.

Generating them from `_HOOK_DEFS` — the same table the CLI installer uses — makes
that class of drift impossible: add a hook to the table and every host manifest
picks it up on the next build.

Run:  python scripts/build_plugin_bundle.py [--check]

``--check`` verifies the committed files match what would be generated and exits
non-zero if not, so CI fails on drift instead of shipping a broken plugin.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from llm_router.install_hooks import _HOOK_DEFS  # noqa: E402

# Claude Code and Codex use the same event names for the events we care about.
# Cursor differs and is handled by its own installer (tasks 23–26).
_CODEX_EVENT_ALIASES: dict[str, str] = {
    "SessionStart": "SessionStart",
    "UserPromptSubmit": "UserPromptSubmit",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "SubagentStart": "SubagentStart",
    "Stop": "Stop",
}


def _hooks_json(root_var: str) -> dict:
    """Build a hooks.json mapping event -> matcher groups -> handlers.

    ``root_var`` is the host's plugin-root placeholder, so the same generator
    serves both hosts without hardcoding an absolute path.
    """
    events: dict[str, dict[str, list]] = {}
    for src_name, installed_name, event, matcher in _HOOK_DEFS:
        events.setdefault(event, {}).setdefault(matcher, []).append(
            {
                "type": "command",
                "command": f"{root_var}/hooks/{src_name}",
            }
        )

    out: dict[str, list] = {}
    for event, by_matcher in events.items():
        groups = []
        for matcher, handlers in by_matcher.items():
            group: dict = {"hooks": handlers}
            if matcher:
                group["matcher"] = matcher
            groups.append(group)
        out[event] = groups
    return {"hooks": out}


def _mcp_json() -> dict:
    """The MCP server entry, resolved through the plugin root rather than a venv.

    The previous manifests assumed a `llm-router` binary already on PATH from a
    separate `pip install`. Declaring the module entry point keeps the plugin
    self-describing; task 38 replaces this with the bundled binary.
    """
    return {
        "mcpServers": {
            "llm_router": {
                "type": "stdio",
                "command": "llm-router",
                "args": [],
                "env": {},
            }
        }
    }


_COMMANDS: dict[str, str] = {
    "status": (
        "---\n"
        "description: Show today's routing savings, tier mix and quota pressure\n"
        "---\n\n"
        "Run `llm-router status` and summarise the result for me: how much was\n"
        "saved today, which tiers absorbed the work, and whether any quota is\n"
        "under pressure. Say plainly if the quota has not been measured yet.\n"
    ),
    "savings": (
        "---\n"
        "description: Report routing savings over a window\n"
        "---\n\n"
        "Run `llm-router status --window ${1:-7d}` and report savings versus the\n"
        "all-premium baseline, the per-provider split, and the top routes. If the\n"
        "figures are estimates rather than measured costs, say so.\n"
    ),
    "policy": (
        "---\n"
        "description: Show or change the routing policy\n"
        "---\n\n"
        "With no argument, run `llm-router policy show` and explain what the\n"
        "current policy routes away and what it keeps. With an argument, run\n"
        "`llm-router policy set $1` and report the expected savings change.\n"
    ),
    "doctor": (
        "---\n"
        "description: Diagnose a broken llm-router setup\n"
        "---\n\n"
        "Run `llm-router doctor` and walk me through anything it flags, in\n"
        "priority order. For each issue give the exact command that fixes it.\n"
        "Note explicitly what doctor does not check.\n"
    ),
}


def build() -> dict[Path, str]:
    """Return {path: content} for every generated file."""
    files: dict[Path, str] = {}

    # Task 01 — the MCP declaration the Codex manifest already referenced.
    #
    # It CANNOT live at the repo root as `.mcp.json`: .gitignore line 97 ignores
    # that name as "Local MCP config", which is why the file the manifest pointed
    # at was never committed and the published Codex plugin was broken. Those are
    # two different files that happened to share a name — a developer's machine-
    # specific config, and the plugin's shipped declaration. Keeping the plugin's
    # copy inside the plugin metadata directory lets both exist.
    mcp_body = json.dumps(_mcp_json(), indent=2) + "\n"
    files[REPO / ".claude-plugin" / "mcp.json"] = mcp_body
    files[REPO / ".codex-plugin" / "mcp.json"] = mcp_body

    # Task 03 — the hook scripts themselves must sit at the plugin root, because
    # a plugin is distributed as a repo clone or a zip and cannot reach into
    # src/llm_router/. Copying keeps the bundle self-contained; the --check mode
    # then fails CI whenever a source hook is edited without rebuilding.
    hooks_src = REPO / "src" / "llm_router" / "hooks"
    for src_name, _installed, _event, _matcher in _HOOK_DEFS:
        origin = hooks_src / src_name
        if origin.is_file():
            files[REPO / "hooks" / src_name] = origin.read_text()
    # The stdlib-only support module the hooks import by path.
    support = hooks_src.parent / "tool_surface.py"
    if support.is_file():
        files[REPO / "hooks" / "llm_router_tool_surface.py"] = support.read_text()

    # Task 03 — one script set, two hooks.json files.
    #
    # Both hosts treat the repo root as the plugin root, so the scripts are
    # shared and only the root placeholder differs. Copying the scripts twice
    # would just be two things to keep in sync.
    files[REPO / "hooks" / "hooks.json"] = (
        json.dumps(_hooks_json("${CLAUDE_PLUGIN_ROOT}"), indent=2) + "\n"
    )
    files[REPO / ".codex-plugin" / "hooks.json"] = (
        json.dumps(_hooks_json("${CODEX_PLUGIN_ROOT}"), indent=2) + "\n"
    )

    # Task 05 — slash commands.
    for name, body in _COMMANDS.items():
        files[REPO / "commands" / f"{name}.md"] = body

    # Tasks 02 & 04 — declare mcpServers and hooks in both manifests.
    for manifest_path in (
        REPO / ".claude-plugin" / "plugin.json",
        REPO / ".codex-plugin" / "plugin.json",
    ):
        manifest = json.loads(manifest_path.read_text())
        # Manifest paths resolve against the PLUGIN ROOT, not the metadata
        # directory — the pre-existing `"skills": "skills/"` entry, which
        # resolves to the repo root, is the proof.
        meta_dir = manifest_path.parent.name
        manifest["mcpServers"] = f"{meta_dir}/mcp.json"
        manifest["commands"] = "commands/"
        manifest["hooks"] = (
            "hooks/hooks.json" if meta_dir == ".claude-plugin" else f"{meta_dir}/hooks.json"
        )
        files[manifest_path] = json.dumps(manifest, indent=2) + "\n"

    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if files are out of date")
    args = ap.parse_args()

    files = build()
    drifted: list[Path] = []

    for path, content in files.items():
        if args.check:
            if not path.exists() or path.read_text() != content:
                drifted.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    if args.check:
        if drifted:
            print("plugin bundle is out of date; run scripts/build_plugin_bundle.py:")
            for p in drifted:
                print(f"  {p.relative_to(REPO)}")
            return 1
        print(f"plugin bundle up to date ({len(files)} files)")
        return 0

    print(f"wrote {len(files)} files:")
    for p in sorted(files):
        print(f"  {p.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
