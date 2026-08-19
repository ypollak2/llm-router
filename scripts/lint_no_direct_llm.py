#!/usr/bin/env python3
"""Enforcement lint — fail if agent code calls an LLM provider directly.

Part 4 of Docs/planning/PLAN.md. Scans Python files for direct provider calls (litellm /
openai / anthropic / google.generativeai) and FAILS unless they route through
LLM Router (the gateway URL / llm_router.route) or are explicitly allowlisted with a
trailing  ``# llm_router: direct-ok``  marker (for the router's own internals and
deliberate fallbacks).

Usage:  python scripts/lint_no_direct_llm.py <path> [<path> ...]
Exit 0 = clean, 1 = violations found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Direct provider call signatures we care about.
_PATTERNS = [
    re.compile(r"\blitellm\.(completion|acompletion)\s*\("),
    re.compile(r"\bopenai\.(ChatCompletion|chat)\b"),
    re.compile(r"\bgenai\.GenerativeModel\b"),
    re.compile(r"\banthropic\.(Anthropic|messages)\b"),
]
# A call is OK if the surrounding lines route through LLM Router.
_OK_CONTEXT = re.compile(
    r"llm_router|gateway|LLM_ROUTER_GATEWAY|OPENAI_BASE_URL|presets\.gateway", re.IGNORECASE)
_ALLOW_MARKER = "# llm_router: direct-ok"


def scan_file(path: Path) -> list[str]:
    violations = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return violations
    for i, line in enumerate(lines):
        if not any(p.search(line) for p in _PATTERNS):
            continue
        if _ALLOW_MARKER in line:
            continue
        # look at a small window for routing context
        window = "\n".join(lines[max(0, i - 4): i + 2])
        if _OK_CONTEXT.search(window):
            continue
        violations.append(f"{path}:{i + 1}: direct provider call not routed through LLM Router\n    {line.strip()}")
    return violations


def main(argv: list[str]) -> int:
    targets = argv or ["."]
    files: list[Path] = []
    for t in targets:
        p = Path(t)
        files.extend(p.rglob("*.py") if p.is_dir() else [p])
    # skip the lint itself, tests, and vendored dirs
    files = [f for f in files if "test" not in f.name
             and ".venv" not in f.parts and "node_modules" not in f.parts
             and f.name != "lint_no_direct_llm.py"]

    violations = [v for f in files for v in scan_file(f)]
    if violations:
        print(f"❌ {len(violations)} direct-provider call(s) bypassing LLM Router:\n")
        print("\n".join(violations))
        print(f"\nRoute via the gateway / llm_router.route, or add '{_ALLOW_MARKER}' if intentional.")
        return 1
    print(f"✅ no direct-provider calls bypass LLM Router ({len(files)} files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
