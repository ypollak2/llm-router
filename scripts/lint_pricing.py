#!/usr/bin/env python3
"""INV-COST-004 — model prices live in llm_router.pricing and nowhere else.

The audit found the same stale Opus rate ($15/$75 — the retired Opus 3 price) in
five independent tables, three of which fed user-visible savings. Git history
shows it fixed locally four separate times and returning every time, because
each fix touched one table and left the others alone.

A canonical module alone does not stop that. Nothing prevents the next person
from adding a sixth table, and the failure is silent — a wrong price produces a
plausible number, not an error. This lint is what turns the convention into a
structural guarantee, mirroring what scripts/lint_tool_surface.py did for tool
names (CHZ-SURF-01) after the same class of bug.

Two checks:

  1. STRUCTURAL — a module-level container whose name looks like a price table
     (PRICING / PRICES / RATES / _PER_M / _PER_MTOK / COST_PER) and which holds
     numeric literals. This is the shape every one of the five tables had.

  2. VALUE — the specific retired rates, wherever they appear together. Catches
     a price literal that dodged check 1 by living in a function body or under
     an innocuous name, which is exactly how dashboard_data.py held $15/$75.

Exit 0 clean, 1 on violations. Run from anywhere:

    python scripts/lint_pricing.py [--root SRC]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# The one module allowed to contain prices.
CANONICAL = "llm_router/pricing.py"

# Paths exempt from the structural check, each with a reason. Keep this list
# short and justified — every entry is a place the guarantee does not hold.
ALLOWED: dict[str, str] = {
    CANONICAL: "the canonical source of truth",
    "llm_router/benchmark/": "benchmark fixtures record historical prices as measured data, not live rates",
}

# Name fragments that mark a container as a price table.
_PRICE_NAME_HINTS = ("PRICING", "PRICES", "RATES", "_PER_M", "_PER_MTOK", "COST_PER", "PRICE_")

# Rates that must never reappear. Each entry is (input, output, why).
# Checked as a PAIR: a lone 15.0 is a timeout or a percentage; 15.0 beside 75.0
# in the same container is the Opus 3 rate.
_RETIRED_RATES: list[tuple[float, float, str]] = [
    (15.0, 75.0, "retired Opus 3 rate — current Opus is $5/$25 (3x overstatement)"),
    (0.80, 4.00, "stale Haiku rate — current Haiku 4.5 is $1.00/$5.00"),
    (0.25, 1.25, "stale Haiku rate — current Haiku 4.5 is $1.00/$5.00"),
]


class Violation:
    def __init__(self, path: Path, line: int, kind: str, detail: str) -> None:
        self.path, self.line, self.kind, self.detail = path, line, kind, detail

    def __str__(self) -> str:
        return f"  {self.path}:{self.line}  [{self.kind}]  {self.detail}"


def _is_allowed(rel: str) -> bool:
    return any(rel == a or rel.startswith(a) for a in ALLOWED)


def _numeric_literals(node: ast.AST) -> list[float]:
    """Every numeric constant under ``node``."""
    return [
        float(n.value)
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
    ]


def _looks_like_price_name(name: str) -> bool:
    upper = name.upper()
    return any(h in upper for h in _PRICE_NAME_HINTS)


def _check_file(path: Path, rel: str) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []  # not our problem; the compiler will say so

    out: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names or node.value is None:
            continue

        literals = _numeric_literals(node.value)
        if not literals:
            continue

        # 1. Structural — a price-shaped container holding numbers.
        if not _is_allowed(rel):
            for name in names:
                if _looks_like_price_name(name) and isinstance(
                    node.value, (ast.Dict, ast.List, ast.Tuple, ast.Set)
                ):
                    out.append(
                        Violation(
                            path,
                            node.lineno,
                            "price-table",
                            f"`{name}` looks like a price table with {len(literals)} literal(s). "
                            f"Import from llm_router.pricing instead.",
                        )
                    )

        # 2. Value — retired rates, even inside the canonical module.
        present = set(literals)
        for inp, outp, why in _RETIRED_RATES:
            if inp in present and outp in present:
                out.append(
                    Violation(
                        path,
                        node.lineno,
                        "retired-rate",
                        f"`{names[0]}` contains {inp}/{outp} — {why}",
                    )
                )

    # 2b. Retired rates in function bodies (how dashboard_data.py held $15/$75).
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        present = set(_numeric_literals(node))
        for inp, outp, why in _RETIRED_RATES:
            if inp in present and outp in present:
                out.append(
                    Violation(
                        path, node.lineno, "retired-rate",
                        f"function `{node.name}` contains {inp}/{outp} — {why}",
                    )
                )
    return out


BASELINE_PATH = Path(__file__).resolve().parent / "pricing_baseline.txt"


def _key(v: Violation, root: Path) -> str:
    """Stable identity for a violation: path + name + kind, NOT line number.

    Line numbers churn on every unrelated edit, which would make the baseline
    noisy enough that people regenerate it reflexively — and a baseline that is
    regenerated reflexively silences the ratchet it exists to enforce.
    """
    rel = v.path.relative_to(root).as_posix()
    detail = v.detail.split("—")[0].strip()
    return f"{rel}\t{v.kind}\t{detail}"


def _load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return {
        ln.strip()
        for ln in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None, help="source root (default: <repo>/src)")
    ap.add_argument(
        "--update-baseline",
        action="store_true",
        help="record current violations as accepted debt (shrink it, never grow it)",
    )
    ap.add_argument("--show-baseline", action="store_true", help="list accepted debt and exit")
    args = ap.parse_args()

    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent / "src"
    if not root.is_dir():
        print(f"lint-pricing: source root not found: {root}", file=sys.stderr)
        return 2

    files = sorted(root.rglob("*.py"))
    violations: list[Violation] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        violations.extend(_check_file(path, rel))

    keys = {_key(v, root): v for v in violations}

    if args.update_baseline:
        BASELINE_PATH.write_text(
            "# INV-COST-004 accepted debt — price tables not yet migrated to llm_router.pricing.\n"
            "# This list may SHRINK, never grow. A new entry means a new price table\n"
            "# was introduced, which is the defect this lint exists to prevent.\n"
            "# Regenerate ONLY after migrating a table, never to silence a new one.\n"
            + "".join(f"{k}\n" for k in sorted(keys)),
            encoding="utf-8",
        )
        print(f"INV-COST-004: baseline updated — {len(keys)} accepted violation(s)")
        return 0

    baseline = _load_baseline()

    if args.show_baseline:
        for k in sorted(baseline):
            print(f"  {k}")
        print(f"\n{len(baseline)} accepted violation(s)")
        return 0

    new = {k: v for k, v in keys.items() if k not in baseline}
    fixed = baseline - set(keys)

    if fixed:
        print(f"INV-COST-004: {len(fixed)} baselined violation(s) no longer present — nice.")
        print("  Run --update-baseline to lock the improvement in.\n")

    if not new:
        print(
            f"INV-COST-004: clean ({len(files)} files checked, "
            f"{len(baseline & set(keys))} accepted pre-existing)"
        )
        return 0

    print(f"INV-COST-004: FAILED — {len(new)} NEW pricing violation(s).\n")
    for v in new.values():
        print(v)
    print(
        "\nModel prices belong in src/llm_router/pricing.py. Import from there:\n"
        "    from llm_router import pricing\n"
        "    rate = pricing.input_rate('claude-opus-5')\n"
        "\nIf a literal here is genuinely not a model price, rename it so it does\n"
        "not look like a price table, or add the path to ALLOWED with a reason.\n"
        "Do NOT run --update-baseline to silence this: the baseline records debt\n"
        "that predates the lint, not new debt.",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
