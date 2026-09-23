#!/usr/bin/env python3
"""Flag `x or 0` / `d.get(k, 0)` whose result reaches a comparison or a mean.

S9 (audit 2026-09-22) and the class behind it. `retrospective.py` read a NULL
`classifier_confidence` as::

    conf = d.get("classifier_confidence", 0) or 0
    if conf < 0.70:
        gap_flags.append("LOW_CONFIDENCE")

On the live ledger that column is NULL for 213 of the 214 rows with trusted
provenance, so every one of them was flagged, then promoted to a
``CLASSIFIER_ERROR`` root cause at confidence "High" with the evidence string
*"Classifier confidence 0%"*. A certain finding, manufactured from a column
nobody ever wrote.

`grep -rnE '\\bor 0(\\.0)?\\b' src/llm_router` returns ~217 matches, and most
of them are fine: `or 0` inside a sum of token counts loses nothing that
matters. **The coercion is not the defect.** The defect is the coercion
reaching a place where "absent" and "zero" mean different things:

* **a comparison** — absent falls below every threshold, so it becomes a
  finding (S9's `analyze_gaps`), or above one, so it becomes an all-clear.
* **a mean** — absent is averaged in as a real observation, so the statistic
  silently becomes "the share of rows that recorded anything" wearing the name
  of a measurement (S9's `analyze_facts`).

So this lints for the *flow*, not the pattern. Three shapes are detected:

1. **Direct** — the coercion is an operand of a `Compare`.
2. **One hop** — the coercion is assigned to a local name, and that name is
   later an operand of a `Compare` in the same function.
3. **Mean** — the coercion is the element of a comprehension, or appended in a
   loop, and the resulting list is both `sum()`-ed and divided by its `len()`.

Shapes 2 and 3 are exactly the two S9 sites, which is not a coincidence: the
detector was built against them and is proven against them in
`tests/test_lint_unknown_as_number.py`, on a synthetic copy of the pre-fix
source rather than by requiring this repo to keep the disease.

Usage::

    python scripts/lint_unknown_as_number.py           # lint src/llm_router
    python scripts/lint_unknown_as_number.py PATH ...  # lint specific files

Exit code 1 if any unallowlisted finding remains.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = REPO_ROOT / "src" / "llm_router"

#: Sites that coerce and then compare or average, but where zero is the
#: genuinely correct reading of absence. Each entry needs a reason — an
#: allowlist without one is how the `_column_exists` table in cost.py drifted
#: until three migrated tables were missing from it (S6).
#:
#: Keyed by ``"<path relative to repo root>:<function>"``.
ALLOWLIST: dict[str, str] = {}

_ZERO_LITERALS = (0, 0.0)


def _is_zero(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in _ZERO_LITERALS and (
        not isinstance(node.value, bool)
    )


def _is_coercion(node: ast.AST) -> bool:
    """`x or 0`, or `d.get(k, 0)` — an absent value rendered as a number."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return any(_is_zero(v) for v in node.values)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) == 2
        and _is_zero(node.args[1])
    ):
        return True
    return False


def _contains_coercion(node: ast.AST) -> bool:
    return any(_is_coercion(n) for n in ast.walk(node))


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    func: str
    shape: str
    detail: str

    @property
    def key(self) -> str:
        rel = self.path.relative_to(REPO_ROOT) if self.path.is_absolute() else self.path
        return f"{rel}:{self.func}"

    def render(self) -> str:
        rel = self.path.relative_to(REPO_ROOT) if self.path.is_absolute() else self.path
        return f"{rel}:{self.line}: [{self.shape}] in {self.func}(): {self.detail}"


def _names_compared(func: ast.AST) -> set[str]:
    """Local names that appear as an operand of a comparison."""
    out: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if isinstance(operand, ast.Name):
                    out.add(operand.id)
    return out


def _mean_denominators(func: ast.AST) -> set[str]:
    """Names X where the function computes both `sum(X)` and `len(X)`.

    That pair is what makes a mean, and it is what turns a coerced absent value
    into an observation. Matching `sum` alone would flag every token tally.
    """
    summed: set[str] = set()
    lengthed: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if not node.args:
                continue
            arg = node.args[0]
            target = arg.id if isinstance(arg, ast.Name) else None
            if node.func.id == "sum" and target:
                summed.add(target)
            elif node.func.id == "len" and target:
                lengthed.add(target)
    return summed & lengthed


def _scan_function(path: Path, func: ast.AST, qualname: str) -> list[Finding]:
    findings: list[Finding] = []
    compared = _names_compared(func)
    means = _mean_denominators(func)

    # Shape 1 — the coercion sits directly inside a comparison.
    for node in ast.walk(func):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if _contains_coercion(operand):
                    findings.append(Finding(
                        path, node.lineno, qualname, "compared-directly",
                        f"`{ast.unparse(operand)}` is compared; absent reads as 0",
                    ))

    for node in ast.walk(func):
        # Shape 2 — assigned to a name that is compared later on.
        if isinstance(node, ast.Assign) and _contains_coercion(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in compared:
                    findings.append(Finding(
                        path, node.lineno, qualname, "compared-via-name",
                        f"`{target.id} = {ast.unparse(node.value)}` and "
                        f"`{target.id}` is later compared; absent reads as 0",
                    ))

        # Shape 3 — collected into something that is summed AND divided by len.
        if isinstance(node, ast.Assign) and isinstance(
            node.value, (ast.ListComp, ast.GeneratorExp, ast.SetComp)
        ):
            if _contains_coercion(node.value.elt):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in means:
                        findings.append(Finding(
                            path, node.lineno, qualname, "averaged",
                            f"`{target.id}` is averaged (sum/len) and its "
                            f"elements coerce absent to 0",
                        ))
    return findings


def scan_source(source: str, path: Path) -> list[Finding]:
    tree = ast.parse(source)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings.extend(_scan_function(path, node, node.name))
    return findings


def scan_path(path: Path) -> list[Finding]:
    try:
        return scan_source(path.read_text(encoding="utf-8"), path)
    except (SyntaxError, UnicodeDecodeError):
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", type=Path)
    args = ap.parse_args(argv)

    targets = args.paths or [DEFAULT_TARGET]
    files: list[Path] = []
    for t in targets:
        files.extend(sorted(t.rglob("*.py")) if t.is_dir() else [t])

    if not files:
        print("ERROR: no Python files to scan — an empty set passes everything")
        return 2

    findings = [f for path in files for f in scan_path(path)]
    kept = [f for f in findings if f.key not in ALLOWLIST]
    allowed = [f for f in findings if f.key in ALLOWLIST]

    print(f"scanned {len(files)} file(s)")
    for f in allowed:
        print(f"  ALLOWED {f.render()}\n          reason: {ALLOWLIST[f.key]}")

    if not kept:
        print("✅ no unknown-as-number findings")
        return 0

    print(f"\n❌ {len(kept)} finding(s):\n")
    for f in kept:
        print(f"  {f.render()}")
    print(
        "\nAn absent value is not a zero. Read the column as None and give "
        "'unmeasured' its own branch, or add an allowlist entry in "
        f"{Path(__file__).name} saying why zero is the right reading here."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
