"""Show what a surviving mutant actually CHANGED, per function.

WHY THIS EXISTS
---------------
"51 mutants survive in _aggregate" is a count, not a diagnosis. Writing tests against a
count means guessing. mutmut wrote both the original and every mutant into the working
copy as sibling functions (x_f__mutmut_orig, x_f__mutmut_1, ...), so the exact changed
line is recoverable and can be read instead of inferred.

Methodology (d) on this codebase: bisect/measure went 3/3, reasoning from source went 0/5.
This is the measuring end of that — the mutation is read out of the artefact that ran, not
reconstructed from what a mutation tool "would" do.
"""

from __future__ import annotations

import ast
import difflib
import pathlib
import sys

REPO = pathlib.Path.home() / "Projects/LLM Router"
MUT = REPO / "mutants/src/llm_router"


def bodies(module: str) -> dict[str, str]:
    """name -> source text, for every top-level def in the mutated module."""
    path = MUT / f"{module}.py"
    src = path.read_text()
    tree = ast.parse(src)
    lines = src.splitlines()
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = "\n".join(lines[node.lineno - 1: node.end_lineno])
    return out


def diff_for(module: str, fn: str, n: int, srcs: dict[str, str]) -> list[str]:
    orig = srcs.get(f"x_{fn}__mutmut_orig")
    mut = srcs.get(f"x_{fn}__mutmut_{n}")
    if orig is None or mut is None:
        return [f"    <bodies not found for x_{fn}__mutmut_{n}>"]
    a = [ln for ln in orig.splitlines()]
    b = [ln for ln in mut.splitlines()]
    out = []
    for ln in difflib.unified_diff(a, b, lineterm="", n=0):
        if ln.startswith(("---", "+++", "@@")):
            continue
        out.append("    " + ln.strip())
    return out or ["    <no textual difference — candidate EQUIVALENT mutant>"]


def main() -> int:
    import json
    data = json.loads((pathlib.Path(__file__).parent / "gf_classes.json").read_text())
    rows = data["train_survivors"]

    targets = sys.argv[1:]
    if not targets:
        print("usage: gf_diff.py module.function [module.function ...]  (or ALL16)")
        return 2

    if targets == ["ALL16"]:
        targets = [k for k, v in data["by_function"].items()
                   if v["survivors"] == v["total"] and v["total"] >= 3]

    cache: dict[str, dict[str, str]] = {}
    for target in targets:
        module, fn = target.split(".", 1)
        srcs = cache.setdefault(module, bodies(module))
        mine = [r for r in rows if r["module"] == module and r["function"] == fn]
        by_outcome: dict[str, int] = {}
        for r in mine:
            by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        print(f"\n{'=' * 78}\n{target}  —  {len(mine)} surviving  "
              f"({', '.join(f'{k}={v}' for k, v in sorted(by_outcome.items()))})\n{'=' * 78}")
        show = mine[: int(sys.argv[0] and 6)] if len(mine) > 6 else mine
        for r in show:
            n = int(r["name"].rsplit("_", 1)[-1])
            print(f"  [{r['outcome']}] mutant {n}")
            for ln in diff_for(module, fn, n, srcs)[:6]:
                print(ln)
        if len(mine) > len(show):
            print(f"  ... {len(mine) - len(show)} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
