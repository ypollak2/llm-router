#!/usr/bin/env python3
"""Does the version bump match what actually changed in the public surface?

N14. `CHANGELOG.md` records, in its own words, that 13.3.1 added `llm_local_task`
— new API surface — and shipped as a PATCH "at the maintainer's explicit
instruction". Writing the violation down is better than hiding it, but a note
cannot stop the next one, and nothing in the release path ever compared the
surface to the number.

This does. It diffs the public surface between a git ref and the working tree and
says what the bump must be at minimum:

    a removed or renamed public name        MAJOR
    a new public name                       MINOR
    neither                                 PATCH

"Public surface" is the MCP tool names, the CLI subcommands, and the top-level
exports of `llm_router` — the three things a user's own scripts can break against.
Internals are deliberately out of scope: this is a promise about what callers see,
not a change detector.

    python3 scripts/release/semver_gate.py                # compare against the last tag
    python3 scripts/release/semver_gate.py --base v13.3.0
    python3 scripts/release/semver_gate.py --check        # exit 1 if the bump is too small

Overriding it is legitimate — but then the override is the decision, not the
default.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
# The MCP tool modules belong here, and their absence was the gate's own first
# failure: built to catch 13.3.1 shipping `llm_local_task` as a patch, the first
# version scanned only server.py/cli.py/__init__.py and reported "surface
# unchanged" for exactly that release.
SURFACE_FILES = [
    "src/llm_router/server.py",
    "src/llm_router/cli.py",
    "src/llm_router/__init__.py",
    "src/llm_router/tools/consolidated.py",
    "src/llm_router/tools/local_task.py",
    "src/llm_router/tools/agents.py",
    "src/llm_router/tools/routing.py",
    "src/llm_router/tools/media.py",
    "src/llm_router/tools/agentic.py",
    "src/llm_router/tools/codex.py",
    "src/llm_router/tools/subscription.py",
]
_TOOL = re.compile(r'@mcp\.tool\(|name\s*=\s*["\']([a-z_]+)["\']')


def _at_ref(ref: str, rel: str) -> str:
    try:
        r = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=ROOT,
                           capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except Exception:                                        # noqa: BLE001
        return ""


def _public_names(source: str) -> set[str]:
    """Top-level defs/classes not starting with _, plus any __all__ entries."""
    out: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                out.add(node.name)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    try:
                        out |= {str(v) for v in ast.literal_eval(node.value)}
                    except Exception:                        # noqa: BLE001
                        pass
    return out


def surface(ref: str | None) -> set[str]:
    names: set[str] = set()
    for rel in SURFACE_FILES:
        src = _at_ref(ref, rel) if ref else (ROOT / rel).read_text(errors="replace")
        if src:
            names |= {f"{Path(rel).stem}.{n}" for n in _public_names(src)}
    return names


def last_tag() -> str | None:
    try:
        r = subprocess.run(["git", "describe", "--tags", "--abbrev=0"], cwd=ROOT,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:                                        # noqa: BLE001
        return None


def current_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (ROOT / "pyproject.toml").read_text(), re.M)
    return m.group(1) if m else "0.0.0"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=None, help="ref to compare against (default: last tag)")
    ap.add_argument("--check", action="store_true", help="exit 1 when the bump is too small")
    ns = ap.parse_args()

    base = ns.base or last_tag()
    if not base:
        print("no tag to compare against; nothing to check")
        return 0

    before, after = surface(base), surface(None)
    added, removed = sorted(after - before), sorted(before - after)

    required = "MAJOR" if removed else ("MINOR" if added else "PATCH")
    old_v = re.search(r"(\d+)\.(\d+)\.(\d+)", base)
    new_v = re.search(r"(\d+)\.(\d+)\.(\d+)", current_version())
    actual = "NONE"
    if old_v and new_v:
        o, n = [int(x) for x in old_v.groups()], [int(x) for x in new_v.groups()]
        actual = ("MAJOR" if n[0] > o[0] else "MINOR" if n[1] > o[1]
                  else "PATCH" if n[2] > o[2] else "NONE")

    print(f"base {base} -> {current_version()}")
    if removed:
        print(f"  REMOVED ({len(removed)}): {', '.join(removed[:6])}")
    if added:
        print(f"  ADDED   ({len(added)}): {', '.join(added[:6])}")
    if not added and not removed:
        print("  public surface unchanged")
    print(f"  required: {required}    actual: {actual}")

    rank = {"NONE": 0, "PATCH": 1, "MINOR": 2, "MAJOR": 3}
    ok = rank[actual] >= rank[required]
    if not ok:
        why = ("lost public names" if removed
               else "gained public names" if added
               else "changed at all")
        print(f"\n  the bump is too small: {required} required, {actual} found.")
        print(f"  Reason: the public surface {why}.")
        print("  Bump the version, or record the override deliberately in CHANGELOG.md.")
    return 0 if ok or not ns.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
