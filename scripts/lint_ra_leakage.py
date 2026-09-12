#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fail the build if a script that fits a shipped policy also reads RouterArena data.

The north star of Track E is a score we can defend line by line: every parameter that ships is
fit on the audited external corpus, and RouterArena data is only ever a thermometer. That is
easy to say and easy to violate by accident -- we now keep a complete 809x8 outcome matrix in
the repo precisely so policies can be *measured* offline, and it sits one import away from any
fitting script.

So the rule is mechanical rather than cultural: **no file may both write a policy artifact and
read RouterArena data.** Measurement tools may read RouterArena data (they write no artifact).
Fitting tools may write artifacts (they read no RouterArena data). A file that does both is the
leak, whatever its comments claim.

``data/policy/peek_log.jsonl`` is deliberately not a policy artifact -- it records that a read
happened, which is the opposite of a fitted parameter.

Usage::

    python scripts/lint_ra_leakage.py            # lint the repo
    python scripts/lint_ra_leakage.py --self-test  # prove the lint can actually fail
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("scripts/routerarena", "src/llm_router", "submissions/routerarena")

# Writing one of these means the file produces something a router's decisions depend on.
POLICY_ARTIFACT = re.compile(
    r"""data/policy/(?!peek_log)[\w.-]*\.json|routerarena_policy\.json|llm_router_policy\.json""",
)

# Reading any of these means the file has RouterArena's own questions, answers or outcomes.
RA_DATA = re.compile(
    r"""data/ra_eval|sub10_matrix|router_inference/predictions|dataset/routerarena
        |routerarena_10|full\.parquet|sub_10\.parquet|robustness\.parquet""",
    re.VERBOSE,
)

# A read that is unambiguously a *write* of the artifact, not a read of RA data, and vice
# versa, is not something the regexes can tell apart -- so the check is deliberately
# conservative: co-occurrence is the violation. An exemption must be declared in the file and
# is therefore visible in review.
EXEMPT = re.compile(r"#\s*lint:\s*ra-leakage-exempt(?:\s+(.*))?")


def scan(root: Path) -> list[tuple[Path, str]]:
    """Return (path, reason) for every file that both fits and reads."""
    violations: list[tuple[Path, str]] = []
    for rel in SCAN_DIRS:
        base = root / rel
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if EXEMPT.search(text):
                continue
            writes = POLICY_ARTIFACT.search(text)
            reads = RA_DATA.search(text)
            if writes and reads:
                violations.append(
                    (
                        path.relative_to(root),
                        f"writes policy artifact ({writes.group(0)!r}) "
                        f"and reads RouterArena data ({reads.group(0)!r})",
                    )
                )
    return violations


def self_test() -> int:
    """Plant a violation and confirm the lint catches it.

    A lint nobody has seen fail is a lint nobody knows works. The acceptance criterion for
    E0.4 asked for exactly this.
    """
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp)
        pkg = fake / "scripts" / "routerarena"
        pkg.mkdir(parents=True)
        (pkg / "clean.py").write_text(
            "OUT = 'data/policy/routerarena_policy.json'\nCORPUS = 'data/corpus'\n"
        )
        if scan(fake):
            print("SELF-TEST FAIL: clean file was flagged", file=sys.stderr)
            return 1
        (pkg / "leaky.py").write_text(
            "OUT = 'data/policy/routerarena_policy.json'\n"
            "M = 'data/ra_eval/sub10_matrix.json'  # fitting on the benchmark\n"
        )
        found = scan(fake)
        if not found:
            print("SELF-TEST FAIL: planted violation was not caught", file=sys.stderr)
            return 1
        (pkg / "leaky.py").write_text(
            "# lint: ra-leakage-exempt measurement tool, writes no parameters\n"
            "OUT = 'data/policy/routerarena_policy.json'\n"
            "M = 'data/ra_eval/sub10_matrix.json'\n"
        )
        if scan(fake):
            print("SELF-TEST FAIL: exemption was not honoured", file=sys.stderr)
            return 1
    print("self-test OK: lint catches a planted violation, passes clean files, "
          "and honours a declared exemption")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    violations = scan(REPO)
    if violations:
        print("RouterArena leakage lint FAILED:", file=sys.stderr)
        for path, reason in violations:
            print(f"  {path}: {reason}", file=sys.stderr)
        print(
            "\nA file may fit a policy or read RouterArena data, not both. If this file "
            "genuinely only measures, declare '# lint: ra-leakage-exempt <reason>'.",
            file=sys.stderr,
        )
        return 1
    print(f"RouterArena leakage lint OK ({', '.join(SCAN_DIRS)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
