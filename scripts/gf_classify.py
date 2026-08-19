"""Classify G-F TRAIN survivors into weakness CLASSES.

WHY THE STDOUT AND NOT `mutmut results`
---------------------------------------
`mutmut results` listed only execution_ledger during Phase 1 — a scope I could not
account for, so Phase 1 refused to build on it. Same reasoning applies here: the stdout is
what the run actually emitted, one line per mutant, and it can be checked against the
run's own tally.

VALIDATION BEFORE USE (methodology (f): validate every probe)
-------------------------------------------------------------
The parse is only trusted if it independently reproduces the numbers already committed in
evidence: TRAIN 904 killed of 1518, VALIDATION 261 of 468. If it does not, the parse is
wrong and nothing downstream is worth reading. A classifier that silently drops 200
mutants looks exactly like one that works.

WHAT "CLASS" MEANS HERE
-----------------------
Not the mutation operator. The operator is what mutmut did; the class is what the SUITE is
missing, which is the thing tests get written against. The primary axis is therefore
outcome, because the outcomes mean genuinely different things:

  no-coverage  no test executes this line at all      -> write a test, any test
  survived     tests execute it and do not notice     -> the assertion is too weak
  timeout      the mutant hangs                       -> often a loop bound; needs care,
                                                         since some are equivalent mutants
                                                         that can never be killed
"""

from __future__ import annotations

import collections
import json
import pathlib
import re
import sys

REPO = pathlib.Path.home() / "Projects/LLM Router"
STDOUT = REPO / ".llm_router/gf/baseline/mutmut_stdout.txt"
GF = REPO / ".llm_router/zero-tolerance-audit/gf"
OUT = pathlib.Path(__file__).parent / "gf_classes.json"

# mutmut's own legend. Anything not 🎉 is a SURVIVOR under doc 20 §4.
OUTCOME = {"🎉": "killed", "🙁": "survived", "🫥": "no_coverage", "⏰": "timeout", "🤔": "suspicious"}
LINE = re.compile(r"^(🎉|🙁|🫥|⏰|🤔)\s+(\S+)\s*$")
NAME = re.compile(r"^llm_router\.(?P<mod>[\w.]+)\.x_(?P<fn>.+)__mutmut_(?P<n>\d+)$")

EXPECTED = {"train": (904, 1518), "validation": (261, 468)}


def parse_stdout() -> dict[str, str]:
    """name -> outcome. Last write wins; mutmut prints a name at most once per run."""
    out: dict[str, str] = {}
    for raw in STDOUT.read_text(errors="replace").splitlines():
        m = LINE.match(raw.strip())
        if m:
            out[m.group(2)] = OUTCOME[m.group(1)]
    return out


def load(split: str) -> list[str]:
    return [ln.strip() for ln in (GF / f"{split}.txt").read_text().splitlines() if ln.strip()]


def main() -> int:
    outcomes = parse_stdout()
    print(f"parsed {len(outcomes)} outcome lines from mutmut_stdout.txt\n")

    # --- Gate: reproduce the committed numbers, or stop. ---
    splits = {s: load(s) for s in ("train", "validation")}
    ok = True
    for split, (exp_killed, exp_n) in EXPECTED.items():
        names = splits[split]
        got = collections.Counter(outcomes.get(n, "<MISSING>") for n in names)
        killed, n = got["killed"], len(names)
        flag = "ok" if (killed, n) == (exp_killed, exp_n) else "MISMATCH"
        if flag != "ok":
            ok = False
        print(f"{split:11} killed {killed}/{n} (evidence says {exp_killed}/{exp_n}) [{flag}]")
        if got["<MISSING>"]:
            print(f"  !! {got['<MISSING>']} names have NO line in stdout")
            ok = False
    if not ok:
        print("\nPARSE DOES NOT REPRODUCE THE COMMITTED NUMBERS — stopping. "
              "Fix the parse before reading anything below it.")
        return 1
    print("\nparse reproduces both committed figures -> trusted\n")

    # --- TRAIN survivors only. Validation is not inspected here, by protocol. ---
    train = splits["train"]
    rows = []
    unparsed = []
    for name in train:
        outcome = outcomes[name]
        if outcome == "killed":
            continue
        m = NAME.match(name)
        if not m:
            unparsed.append(name)
            continue
        rows.append({"name": name, "outcome": outcome,
                     "module": m.group("mod"), "function": m.group("fn")})
    if unparsed:
        print(f"!! {len(unparsed)} survivor names did not match the name regex, e.g. {unparsed[:3]}")
        return 1

    print(f"TRAIN survivors (doc 20 §4 conservative): {len(rows)}\n")

    by_outcome = collections.Counter(r["outcome"] for r in rows)
    print("by outcome — the primary axis, because these need different work:")
    for k, v in by_outcome.most_common():
        print(f"  {k:12} {v:4}")

    print("\nby module:")
    by_mod = collections.Counter(r["module"] for r in rows)
    tot_mod = collections.Counter(NAME.match(n).group("mod") for n in train if NAME.match(n))
    for mod, v in by_mod.most_common():
        print(f"  {mod:22} {v:4} of {tot_mod[mod]:4} train mutants  ({v / tot_mod[mod]:.0%} survive)")

    print("\ntop 25 functions by surviving mutants:")
    fkey = collections.Counter((r["module"], r["function"]) for r in rows)
    ftot = collections.Counter()
    for n in train:
        m = NAME.match(n)
        if m:
            ftot[(m.group("mod"), m.group("fn"))] += 1
    for (mod, fn), v in fkey.most_common(25):
        oc = collections.Counter(r["outcome"] for r in rows
                                 if (r["module"], r["function"]) == (mod, fn))
        detail = " ".join(f"{k}={n}" for k, n in sorted(oc.items()))
        print(f"  {v:4}/{ftot[(mod, fn)]:<4} {mod}.{fn:38} {detail}")

    # A function with EVERY train mutant surviving is a different animal from one with a
    # few: it means nothing meaningfully tests it, whatever the coverage percentage says.
    total_dead = [(k, v) for k, v in fkey.items() if v == ftot[k] and v >= 3]
    print(f"\nfunctions where EVERY train mutant survives (>=3 mutants): {len(total_dead)}")
    for (mod, fn), v in sorted(total_dead, key=lambda kv: -kv[1])[:20]:
        print(f"  {v:4}  {mod}.{fn}")

    OUT.write_text(json.dumps(
        {"train_survivors": rows,
         "by_outcome": dict(by_outcome),
         "by_module": dict(by_mod),
         "by_function": {f"{m}.{f}": {"survivors": v, "total": ftot[(m, f)]}
                         for (m, f), v in fkey.most_common()}},
        indent=2) + "\n")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
