#!/usr/bin/env python3
"""PostToolUse hook — version sync guard.

Fires after Edit/Write/MultiEdit. If pyproject.toml, plugin.json, and
marketplace.json exist and their versions differ, blocks the next action
with a clear error message.
"""
import json
import os
import sys

ROOT = "/Users/yali.pollak/projects/llm-router"

try:
    import tomllib
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        v_pyproject = tomllib.load(f)["project"]["version"]
    with open(os.path.join(ROOT, ".claude-plugin/plugin.json")) as f:
        v_plugin = json.load(f)["version"]
    with open(os.path.join(ROOT, ".claude-plugin/marketplace.json")) as f:
        v_marketplace = json.load(f)["version"]
except FileNotFoundError:
    sys.exit(0)  # Files don't exist yet — skip check
except Exception:
    sys.exit(0)  # Never crash on a version check

if v_pyproject == v_plugin == v_marketplace:
    sys.exit(0)  # All good

print(json.dumps({
    "decision": "block",
    "reason": (
        f"⚠️  VERSION MISMATCH detected:\n"
        f"  pyproject.toml   → {v_pyproject}\n"
        f"  plugin.json      → {v_plugin}\n"
        f"  marketplace.json → {v_marketplace}\n\n"
        f"Sync all three to the same version before continuing.\n"
        f"Run: python3 -c \"import tomllib,json; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])\""
    )
}))
