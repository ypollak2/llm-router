"""Classify every TRAIN survivor by WHAT WAS MUTATED, mechanically.

The question this answers is not "how many survive" (614, already known) but "what would
killing them require a test to assert". Those are different questions and only the second
one produces a plan.

The kind is derived from the recovered diff, not guessed from the mutant number. Anything
the rules cannot classify is reported as `unclassified` and counted — a bucket that
quietly absorbs the awkward cases would make every number below it meaningless.
"""

from __future__ import annotations

import ast
import collections
import difflib
import json
import pathlib
import re
import sys

REPO = pathlib.Path.home() / "Projects/LLM Router"
MUT = REPO / "mutants/src/llm_router"
HERE = pathlib.Path(__file__).parent

_CACHE: dict[str, dict[str, str]] = {}


def bodies(module: str) -> dict[str, str]:
    if module in _CACHE:
        return _CACHE[module]
    src = (MUT / f"{module}.py").read_text()
    lines = src.splitlines()
    out = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = "\n".join(lines[node.lineno - 1: node.end_lineno])
    _CACHE[module] = out
    return out


def changed_pair(module: str, fn: str, n: int) -> tuple[str, str] | None:
    srcs = bodies(module)
    orig, mut = srcs.get(f"x_{fn}__mutmut_orig"), srcs.get(f"x_{fn}__mutmut_{n}")
    if orig is None or mut is None:
        return None
    a, b = orig.splitlines(), mut.splitlines()
    minus = [ln.strip() for ln in difflib.unified_diff(a, b, lineterm="", n=0)
             if ln.startswith("-") and not ln.startswith("---")]
    plus = [ln.strip() for ln in difflib.unified_diff(a, b, lineterm="", n=0)
            if ln.startswith("+") and not ln.startswith("+++")]
    # First line is always the def rename; drop it.
    minus = [m for m in minus if not m.startswith("-def ")]
    plus = [p for p in plus if not p.startswith("+def ")]
    if not minus and not plus:
        return None
    return ("\n".join(m[1:] for m in minus), "\n".join(p[1:] for p in plus))


CMP = [" == ", " != ", " < ", " > ", " <= ", " >= ", " is not ", " is ", " not in ", " in "]
ARITH = [" + ", " - ", " * ", " / ", " // ", " % "]


def _ops(text: str) -> collections.Counter:
    """Multiset of comparison/boolean operators, longest-match first so `<=` is not
    counted as a `<`. Counting rather than membership-testing matters: a line with two
    comparisons changes one of them, and `"<=" in a` stays True either way."""
    out = collections.Counter()
    i, n = 0, len(text)
    toks = ["<=", ">=", "==", "!=", "<", ">"]
    # `is not` CONTAINS ` is `, and `not in` contains ` in `. Counting both naively made
    # `x is None` -> `x is not None` unclassifiable: `is` appeared in both sides, so the
    # difference was an addition with no matching removal and no pair could form.
    out["is not"] = text.count("is not")
    out["not in"] = text.count("not in")
    out["is"] = text.count(" is ") - out["is not"]
    out["in"] = text.count(" in ") - out["not in"]
    out["and"] = text.count(" and ")
    out["or"] = text.count(" or ")
    for k in ("is", "in"):
        out[k] = max(0, out[k])
    while i < n:
        for t in toks:
            if text.startswith(t, i):
                out[t] += 1
                i += len(t)
                break
        else:
            i += 1
    return out


BOUNDARY = {("<=", "<"), ("<", "<="), (">=", ">"), (">", ">="), ("<=", ">="), (">=", "<=")}
EQUALITY = {("==", "!="), ("!=", "=="), ("is", "is not"), ("is not", "is"),
            ("in", "not in"), ("not in", "in")}
LOGICAL = {("and", "or"), ("or", "and")}


def kind_of(before: str, after: str) -> str:
    b, a = before.strip(), after.strip()
    if re.search(r"XX.*XX", after):
        return "string_literal"          # mutmut wraps a str literal in XX..XX
    if b and not a:
        return "statement_deleted"
    if a in ("pass", "return", "return None") and b not in ("pass", "return", "return None"):
        return "body_removed"

    # --- operator changes, before anything textual, and split by what they MEAN ---
    ob, oa = _ops(b), _ops(a)
    gone = ob - oa
    added = oa - ob
    if gone and added:
        pairs = {(g, x) for g in gone for x in added}
        if pairs & BOUNDARY:
            return "boundary_flip"       # off-by-one class: <= vs <, >= vs >
        if pairs & EQUALITY:
            return "equality_flip"
        if pairs & LOGICAL:
            return "logical_flip"

    if ("True" in b and "False" in a) or ("False" in b and "True" in a):
        return "boolean_flip"

    ctl = {"continue", "break", "return"}
    if {w for w in ctl if re.search(rf"\b{w}\b", b)} != {w for w in ctl if re.search(rf"\b{w}\b", a)}:
        return "control_flow"

    # `f(x, None)` -> `f(x, )`: the DEFAULT is removed, which turns a miss into a raise.
    if re.search(r",\s*\)", a) and not re.search(r",\s*\)", b):
        return "default_removed"

    if "None" in a and "None" not in b:
        return "arg_to_none"

    # `total += x` -> `total = x` (only the last row counts) or `total -= x` (sign
    # inverted). On an accumulator over query rows this is a money bug, so it gets its
    # own class rather than being filed under generic arithmetic.
    aug = re.compile(r"(?<![=!<>+\-*/%])([+\-*/]?=)(?!=)")
    if aug.findall(b) != aug.findall(a):
        return "accumulator"

    nb = re.findall(r"(?<![\w.])\d+(?:\.\d+)?", b)
    na = re.findall(r"(?<![\w.])\d+(?:\.\d+)?", a)
    if nb != na and (nb or na):
        return "numeric_literal"

    for op in ARITH:
        if op in b and op not in a:
            return "arithmetic"
    # `"█" * filled` -> `"█" / filled`: arithmetic op SWAPPED, both still present.
    if any(op in b for op in ARITH) and any(op in a for op in ARITH):
        if [op for op in ARITH if op in b] != [op for op in ARITH if op in a]:
            return "arithmetic"

    if b.count(",") > a.count(","):
        return "kwarg_removed"
    if b.upper() == a.upper() and b != a:
        return "string_case"
    return "unclassified"


# Which functions are OS/IO side-effect shims? Decided by what the ORIGINAL body does,
# not by name: a function whose only job is to hand argv to a subprocess or write a file
# has no in-process behaviour for a behavioural test to assert on.
SHIM_MARKERS = ("subprocess.run", "subprocess.Popen", "osascript", "notify-send",
                "os.system", "webbrowser.open")


def is_shim(module: str, fn: str) -> bool:
    body = bodies(module).get(f"x_{fn}__mutmut_orig", "")
    return any(m in body for m in SHIM_MARKERS)


def main() -> int:
    data = json.loads((HERE / "gf_classes.json").read_text())
    rows = data["train_survivors"]

    kinds = collections.Counter()
    shim_kinds = collections.Counter()
    per_row = []
    failed = 0
    for r in rows:
        n = int(r["name"].rsplit("_", 1)[-1])
        pair = changed_pair(r["module"], r["function"], n)
        if pair is None:
            failed += 1
            k = "no_diff_recoverable"
            before = after = ""
        else:
            before, after = pair
            k = kind_of(before, after)
        shim = is_shim(r["module"], r["function"])
        kinds[k] += 1
        if shim:
            shim_kinds[k] += 1
        per_row.append({**r, "kind": k, "shim": shim, "before": before[:200], "after": after[:200]})

    print(f"TRAIN survivors classified: {len(per_row)}   (diff unrecoverable for {failed})\n")
    print("by mutation kind:")
    for k, v in kinds.most_common():
        print(f"  {k:22} {v:4}   of which in an OS/IO shim: {shim_kinds[k]:3}")

    n_shim = sum(1 for r in per_row if r["shim"])
    print(f"\nsurvivors inside OS/IO shim functions: {n_shim} of {len(per_row)} "
          f"({n_shim / len(per_row):.0%})")
    shim_fns = collections.Counter(f"{r['module']}.{r['function']}" for r in per_row if r["shim"])
    for fn, v in shim_fns.most_common():
        print(f"  {v:4}  {fn}")

    print("\nunclassified examples (so the bucket can be audited, not trusted):")
    for r in [r for r in per_row if r["kind"] == "unclassified"][:8]:
        print(f"  {r['module']}.{r['function']}")
        print(f"    - {r['before'][:110]}")
        print(f"    + {r['after'][:110]}")

    (HERE / "gf_kinds.json").write_text(json.dumps(per_row, indent=2) + "\n")
    print(f"\nwrote {HERE / 'gf_kinds.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
