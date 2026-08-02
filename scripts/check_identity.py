#!/usr/bin/env python3
"""Identity gate — fail CI if the "chuzom" brand leaks outside allowlisted spots.

llm-router ported several capabilities from Chuzom (see the migration plan).
All *runtime* and *public* surfaces must stay branded as "llm-router" — the
word "chuzom" may only appear in a narrow set of provenance/documentation
locations: porting-header comments, the README's dedicated "Meet Chuzom"
enterprise-upsell section, brand-leak regression tests (which must literally
reference the string to assert its absence elsewhere), the changelog, and
this gate's own source.

This script scans every git-tracked file (via `git ls-files`, so `.git`
internals are never touched) case-insensitively for "chuzom" and fails (exit
code 1) on any hit that isn't covered by the allowlist below. Runtime code
hits are never allowlistable — if one is found, it must be fixed, not
excused.

Usage:
    python scripts/check_identity.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_CHUZOM_RE = re.compile(r"chuzom", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

# Whole files where "chuzom" may appear anywhere, no matter the line.
#
# - loophole.json: local tool-run metadata, not a runtime/public surface.
# - README.md: the *entire* file is not allowlisted — only the dedicated
#   "Meet Chuzom" enterprise section is (see _readme_section_lines below).
# - tests/observability/test_surface_status.py,
#   tests/observability/test_summary.py: brand-leak regression tests that
#   must literally assert `"chuzom" not in ...` somewhere in their body.
# - CHANGELOG.md: historical release notes describing past porting work.
# - scripts/check_identity.py, tests/test_identity_gate.py: this gate and
#   its test, which necessarily discuss "chuzom" as a string to detect.
# - run_port_tests.sh: pre-existing sandbox runner for the Chuzom->llm-router
#   port itself (tooling comment, not a runtime/public surface). Extended
#   into the allowlist during WS0 after `git grep -il chuzom` turned it up
#   as a pre-existing legitimate hit.
# - tests/subscription_local/test_subscription_local.py: pre-existing test
#   docstring citing "Audit (Chuzom review)" as the provenance of a specific
#   regression case. Extended into the allowlist during WS0 for the same
#   reason as run_port_tests.sh.
# - tests/test_retrospective.py, tests/test_team.py,
#   tests/commands/test_team.py: WS7 brand-leak regression tests for the
#   retrospective loop (C7) and team-report enrichment (C8) — neither module
#   is a literal Chuzom port (retrospective.py predates the migration
#   program; team.py's WS2/WS3 enrichment is new design), so no "ported from
#   chuzom" header applies. Each file must literally assert
#   `"chuzom" not in ...` to guard against future brand leakage.
# - tests/test_contracts.py: `TestModuleHygiene.test_no_chuzom_imports`
#   (WS8) is a brand-leak regression test guarding `contracts.py` against
#   ever importing a chuzom module — the test's name, comment, and
#   assertion necessarily name the string it forbids in order to check for
#   it. `contracts.py` itself is not a documented Chuzom port (no "ported
#   from chuzom" header applies), so the same rationale as the WS7 entries
#   above governs: this is provenance-of-a-guard, not brand leakage.
ALLOW_FILES: frozenset[str] = frozenset(
    {
        "loophole.json",
        "tests/observability/test_surface_status.py",
        "tests/observability/test_summary.py",
        "CHANGELOG.md",
        "scripts/check_identity.py",
        "tests/test_identity_gate.py",
        "run_port_tests.sh",
        "tests/subscription_local/test_subscription_local.py",
        "tests/test_retrospective.py",
        "tests/test_team.py",
        "tests/commands/test_team.py",
        "tests/test_contracts.py",
    }
)

# Path prefixes where a documented porting header excuses the *whole* file's
# chuzom mentions (docstrings that explain the port, e.g. "no chuzom deps"),
# provided the file contains at least one line matching _PORTED_HEADER_RE.
_PORTED_HEADER_PREFIXES: tuple[str, ...] = ("src/llm_router/", "tests/")
_PORTED_HEADER_RE = re.compile(r"ported from chuzom", re.IGNORECASE)

# The README.md heading that opens the enterprise-upsell section, and the
# heading level that closes it (the next "## " heading, or EOF).
_README_SECTION_HEADING = re.compile(r"^##\s+Need Enterprise-Grade Routing\? Meet Chuzom\s*$")
_README_NEXT_HEADING = re.compile(r"^##\s+")
# The README table-of-contents also links to that section by anchor text —
# allow that single reference line too (it's not inside the section body).
_README_TOC_RE = re.compile(r"Meet Chuzom", re.IGNORECASE)


def _git_ls_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode("utf-8", errors="surrogateescape").split("\0") if p]


def _read_lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (UnicodeDecodeError, OSError):
        return None


def _readme_allowed_line_numbers(lines: list[str]) -> set[int]:
    """1-indexed line numbers inside README.md that are allowed to mention chuzom."""
    allowed: set[int] = set()
    in_section = False
    for i, line in enumerate(lines, start=1):
        if _README_SECTION_HEADING.match(line):
            in_section = True
            allowed.add(i)
            continue
        if in_section:
            if _README_NEXT_HEADING.match(line):
                in_section = False
            else:
                allowed.add(i)
                continue
        if _README_TOC_RE.search(line):
            allowed.add(i)
    return allowed


def find_violations() -> list[tuple[str, int, str]]:
    """Return (path, line_no, line_text) for every non-allowlisted chuzom hit."""
    violations: list[tuple[str, int, str]] = []

    for rel_path in _git_ls_files():
        path = REPO_ROOT / rel_path
        lines = _read_lines(path)
        if lines is None:
            continue

        hit_lines = [i for i, line in enumerate(lines, start=1) if _CHUZOM_RE.search(line)]
        if not hit_lines:
            continue

        if rel_path in ALLOW_FILES:
            continue

        if rel_path == "README.md":
            allowed = _readme_allowed_line_numbers(lines)
            for i in hit_lines:
                if i not in allowed:
                    violations.append((rel_path, i, lines[i - 1]))
            continue

        if rel_path.startswith(_PORTED_HEADER_PREFIXES) and any(
            _PORTED_HEADER_RE.search(lines[i - 1]) for i in hit_lines
        ):
            # Documented port: the whole file's chuzom mentions are provenance
            # commentary (e.g. "no chuzom deps"), not brand leakage.
            continue

        for i in hit_lines:
            violations.append((rel_path, i, lines[i - 1]))

    return violations


def main() -> int:
    violations = find_violations()
    if not violations:
        print("check_identity: OK — no non-allowlisted \"chuzom\" hits found.")
        return 0

    print("check_identity: FAILED — found non-allowlisted \"chuzom\" hits:\n")
    for path, line_no, text in violations:
        print(f"  {path}:{line_no}: {text.strip()}")
    print(
        "\nIf this is genuinely a pre-existing, legitimate provenance/docs "
        "reference, extend the allowlist in scripts/check_identity.py. "
        "Runtime-code hits must be fixed, not allowlisted."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
