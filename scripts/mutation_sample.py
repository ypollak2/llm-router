#!/usr/bin/env python3
"""Mutation sample over the money / routing / verification modules.

WP-14 requires "10-mutation sample on money/routing/verification: >=8 killed".
Gate G-F requires "mutation score >= mutation_baseline + 0.15, floor 0.80".

Neither was runnable: mutmut is a declared dev dependency but was never wired to
anything, and no ``mutation_baseline`` exists in the repo. "baseline + 0.15" with
no baseline has no value, so G-F could not be evaluated either way.

WHY A DECLARED SAMPLE RATHER THAN A FULL MUTMUT CENSUS
------------------------------------------------------
A full mutmut run over these modules takes hours and its score moves whenever
unrelated code is added, which makes it a poor gate value to compare across two
SHAs. The plan asks for a *sample*, and a sample is only honest if it is fixed
and declared up front — otherwise the person running it chooses the mutations
after seeing which ones pass, and the score means nothing.

So the ten mutations below are enumerated, semantic, and frozen. Each is a change
a reviewer would call a real defect, not a trivial operator flip. Each names the
narrowest test subset that ought to catch it: a mutation "killed" only by running
the entire suite tells you far less than one killed by the tests that claim to
own that behaviour.

BASELINE COMPARABILITY
----------------------
Run with ``--baseline-sha`` to execute the identical sample in a detached
worktree at the pre-remediation SHA recorded in 00_AUDIT_BASELINE.md. That is the
only honest way to produce G-F's baseline: measuring it at HEAD, after the
remediation, would make the gate unsatisfiable by construction — you would need
+0.15 over your own improved score. Mutations that do not apply cleanly at the
baseline (because the code did not exist yet) are reported as N/A rather than
counted as killed, since crediting them would inflate the improvement.

SAFETY
------
Every mutation is applied to a file, tested, then restored from an in-memory copy
in a finally block, and the worktree is verified clean afterwards. This mirrors
the discipline the original audit used for its own fault injections.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Pre-remediation SHA from .llm_router/zero-tolerance-audit/00_AUDIT_BASELINE.md.
BASELINE_SHA = "c2c28821f690f7cbda42b46da06fc36ef77d816e"


@dataclass(frozen=True)
class Mutation:
    mid: str
    area: str  # money | routing | verification
    path: str
    old: str
    new: str
    tests: list[str] = field(default_factory=list)
    rationale: str = ""
    #: A Python expression, run under ``PYTHONPATH=src``, whose printed value
    #: must DIFFER between clean and mutated source. This is how a SURVIVOR is
    #: distinguished from an EQUIVALENT MUTANT (see ``evaluate``). Killed
    #: mutants need no probe: failing a test IS proof the behaviour changed.
    probe: str = ""


MUTATIONS: list[Mutation] = [
    # ── money ────────────────────────────────────────────────────────────────
    Mutation(
        "M1", "money", "src/llm_router/pricing.py",
        '"claude-opus-5": Price("claude-opus-5", 5.00, 25.00)',
        '"claude-opus-5": Price("claude-opus-5", 999.00, 25.00)',
        ["tests/economics/"],
        "Stale baseline price. The audit's own Q3(b) ran this and every test passed.",
        probe="__import__('llm_router.pricing', fromlist=['x']).rates_per_m('claude-opus-5')",
    ),
    Mutation(
        "M2", "money", "src/llm_router/cost.py",
        "            rates = _pricing.rates_per_m(_resolved)\n    if rates is None:\n        return 0.0",
        "            rates = _pricing.rates_per_m(_resolved)\n    if True:\n        return 0.0",
        ["tests/economics/", "tests/test_savings.py"],
        "Every Claude cost becomes 0.0, so savings = baseline - 0 = baseline and "
        "every surface overstates savings by the full actual cost. NOTE: the "
        "first version of this mutation only nulled the FIRST lookup, which the "
        "llm_router.pricing fallback absorbed -- an equivalent mutant that changed no "
        "behaviour and was briefly misreported as a coverage hole. A survivor is "
        "only evidence of a missing test once the mutation is confirmed to change "
        "observable behaviour.",
        # The probe that would have caught the original M2 immediately: it
        # returned 0.03 both before and after, because the pricing fallback
        # absorbed the mutation.
        probe="__import__('llm_router.cost', fromlist=['x'])._claude_cost('claude-opus-5', 1000, 1000)",
    ),
    Mutation(
        "M3", "money", "src/llm_router/hooks/session-end.py",
        "    total_saved = total_base - total_cost",
        "    total_saved = max(0.0, total_base - total_cost)",
        ["tests/economics/test_savings_sign.py"],
        "Reintroduces the AUD-06 clamp that hid overspend from users.",
    ),
    Mutation(
        "M4", "money", "src/llm_router/pricing.py",
        'SAVINGS_BASELINE_MODEL = "claude-opus-5"',
        'SAVINGS_BASELINE_MODEL = "claude-haiku-4-5"',
        ["tests/economics/"],
        "Silently understates every savings figure by choosing a cheaper counterfactual.",
        probe="__import__('llm_router.pricing', fromlist=['x']).savings_baseline_rates()",
    ),
    # ── routing ──────────────────────────────────────────────────────────────
    Mutation(
        "M5", "routing", "src/llm_router/tool_surface.py",
        'CORE_TOOLS: frozenset[str] = frozenset({\n    "llm_query",',
        'CORE_TOOLS: frozenset[str] = frozenset({\n    "llm_bogus_xyz",',
        ["tests/test_tool_surface.py", "tests/routing/"],
        probe="sorted(__import__('llm_router.tool_surface', fromlist=['x']).CORE_TOOLS)",
        rationale="Bogus canonical tool name in the CORE tier. Audit Q3(c): lint clean, "
        "106 tests green. NOTE: the first anchor here was the bare string "
        '\'"llm_query"\', which matches 20 sites in this file -- it rewrote the '
        "whole tool surface at once and still reported 'killed', which measures "
        "nothing. Anchored to the CORE_TOOLS binding so exactly one tier changes.",
    ),
    Mutation(
        "M6", "routing", "src/llm_router/router.py",
        "            AGENTIC_TASK_TYPES if _agentic_pin_is_explicit else DYNAMIC_AGENTIC_TASK_TYPES",
        "            AGENTIC_TASK_TYPES",
        ["tests/audit/test_provider_matrix.py"],
        "Reverts the dynamic-pin narrowing; restores the single-model collapse.",
    ),
    Mutation(
        "M7", "routing", "src/llm_router/router.py",
        "    offset = _stable_task_offset(task_type.value, len(models))",
        "    offset = 0",
        ["tests/audit/", "tests/routing/"],
        "Kills per-task rotation, so every task type leads with the same model. "
        "OWNERSHIP NOTE: this is gated by tests/audit/test_execution_variety.py, "
        "NOT by test_provider_matrix.py -- the file named for provider/task "
        "ordering, and where a reader looking for rotation coverage would go. "
        "The behaviour is covered; the coverage is filed somewhere nobody would "
        "look for it, which is its own (smaller) finding.",
    ),
    # ── verification / telemetry ─────────────────────────────────────────────
    Mutation(
        "M8", "verification", "src/llm_router/coverage.py",
        'def record_unobserved(reason: Reason) -> None:\n    """Record that a prompt exited WITHOUT producing a routing directive."""\n    _record("u", reason.name)',
        'def record_unobserved(reason: Reason) -> None:\n    """Record that a prompt exited WITHOUT producing a routing directive."""\n    return',
        ["tests/telemetry/"],
        "Silently stops counting bypasses -- reinstates the I-1 blind spot exactly.",
    ),
    Mutation(
        "M9", "verification", "src/llm_router/coverage.py",
        "        if not self.readable or self.total_n == 0:\n            return None",
        "        if not self.readable or self.total_n == 0:\n            return 100.0",
        ["tests/telemetry/"],
        "Unknown coverage renders as perfect coverage -- fails in the flattering direction.",
    ),
    Mutation(
        "M10", "verification", "src/llm_router/execution_ledger.py",
        "def record_event(ev: LedgerEvent, *, path: Path | None = None) -> bool:",
        "def record_event(ev: LedgerEvent, *, path: Path | None = None) -> bool:\n    return False",
        ["tests/test_execution_ledger.py"],
        "Ledger drops every event. Audit Q3(d) confirmed this gate works; kept in "
        "the sample as a positive control -- a sample with no known-killed "
        "mutation cannot distinguish 'gates are good' from 'harness is broken'.",
    ),
]


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run a subprocess with BYTECODE CACHING DISABLED.

    This is load-bearing, not hygiene. A mutation that does not change the
    file's BYTE LENGTH -- `pressure=0.0` -> `pressure=1.0`, or swapping two
    identifiers -- combined with a write landing in the same integer second as
    the mtime recorded in an existing `.pyc`, lets Python serve STALE BYTECODE.
    The probe then reads the wrong version of the module.

    Measured, three rapid apply/restore cycles on B10:
        caching ON:   clean 0.0 mutated 1.0 | clean 1.0 mutated 1.0 | clean 1.0 mutated 1.0
        caching OFF:  clean 0.0 mutated 1.0 | clean 0.0 mutated 1.0 | clean 0.0 mutated 1.0
    With caching on, from the second cycle the CLEAN source reads as MUTATED --
    so `clean_probe == mutated_probe` and the harness reports EQUIVALENT for a
    mutation that changes behaviour perfectly well.

    I first tested this hypothesis ONCE, got a favourable answer because that
    single run straddled a second boundary, and recorded it as REJECTED. It was
    not. One measurement of a timing-dependent effect measures the timing, not
    the effect.
    """
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout + proc.stderr


class AmbiguousMutation(Exception):
    """The mutation's anchor matches more than one site."""


def _apply(root: Path, m: Mutation) -> str | None:
    """Apply a mutation. Returns the original text, or None if it did not apply.

    The anchor MUST match exactly once. This started as a blanket ``replace()``,
    which silently mutated every matching site: M2's anchor turned out to occur
    three times in cost.py, so the run mutated three unrelated functions at once
    and reported "killed" without telling us which gate caught what. A mutation
    that hits an unknown number of places is not a measurement.
    """
    target = root / m.path
    if not target.exists():
        return None
    original = target.read_text()
    n = original.count(m.old)
    if n == 0:
        return None
    if n > 1:
        raise AmbiguousMutation(f"{m.mid}: anchor matches {n} sites in {m.path}")
    target.write_text(original.replace(m.old, m.new, 1))
    return original


def _probe(root: Path, python: str, expr: str) -> str | None:
    """Print one observable value from the current source. None if it errored.

    An errored probe is NOT treated as a difference: "it used to return 0.03 and
    now it raises" is a real behaviour change, but "the probe was written wrong"
    looks identical from here, and this audit has already been misled once by a
    probe that emitted nothing and was read as a result.
    """
    rc, out = _run([python, "-c", f"import sys; sys.path.insert(0,'src'); print({expr})"], root)
    return out.strip() if rc == 0 else None


def evaluate(root: Path, python: str, only: list[str] | None = None) -> dict:
    results: list[dict] = []
    for m in MUTATIONS:
        if only and m.mid not in only:
            continue
        # Taken BEFORE the mutation is applied, so the comparison has a real
        # reference point rather than one derived from the mutated tree.
        clean_probe = _probe(root, python, m.probe) if m.probe else None
        try:
            original = _apply(root, m)
        except AmbiguousMutation as exc:
            results.append({"id": m.mid, "area": m.area, "status": "invalid",
                            "note": str(exc)})
            print(f"  {m.mid:4} {m.area:12} INVALID  {exc}")
            continue
        if original is None:
            results.append({"id": m.mid, "area": m.area, "status": "n/a",
                            "note": "mutation did not apply at this SHA"})
            print(f"  {m.mid:4} {m.area:12} N/A   (did not apply)")
            continue
        try:
            tests = [t for t in m.tests if (root / t).exists()]
            if not tests:
                results.append({"id": m.mid, "area": m.area, "status": "n/a",
                                "note": "named tests absent at this SHA"})
                print(f"  {m.mid:4} {m.area:12} N/A   (tests absent)")
                continue
            rc, _out = _run([python, "-m", "pytest", "-q", "-x", "--no-header",
                             "-p", "no:cacheprovider", *tests], root)
            killed = rc != 0

            if killed:
                # No probe needed: a failing test IS the proof that observable
                # behaviour changed.
                results.append({"id": m.mid, "area": m.area, "status": "killed",
                                "tests": tests})
                print(f"  {m.mid:4} {m.area:12} {'killed':9} via {' '.join(tests)}")
            else:
                # #16 action 2. A survivor is evidence of a missing test ONLY
                # once the mutation is confirmed to change observable behaviour.
                # M2's first version did not: the llm_router.pricing fallback
                # absorbed it, and it was briefly reported as a coverage hole
                # because the consequence was inferred from the mutation's
                # stated INTENT rather than measured. This is that missing step.
                mutated_probe = _probe(root, python, m.probe) if m.probe else None
                if not m.probe:
                    status, note = "UNVERIFIED", (
                        "survived but carries no behaviour probe -- cannot tell a "
                        "missing test from an equivalent mutant, so NOT counted"
                    )
                elif clean_probe is None or mutated_probe is None:
                    status, note = "UNVERIFIED", (
                        f"probe did not evaluate (clean={clean_probe!r}, "
                        f"mutated={mutated_probe!r}); a probe that emits nothing "
                        "proves nothing"
                    )
                elif clean_probe == mutated_probe:
                    # WORDING MATTERS HERE. This has measured a property of the
                    # PROBE, not of the mutation: all it knows is that this probe
                    # could not tell the two apart. The first version of this
                    # message asserted "the mutation changes no observable
                    # behaviour", which is a strictly stronger claim than the
                    # evidence supports -- and it was wrong three times out of
                    # three on the baseline-era sample, where the probes simply
                    # did not exercise the mutated line.
                    status, note = "EQUIVALENT", (
                        f"THE PROBE saw no difference ({clean_probe!r} both before "
                        "and after). Either the mutation is equivalent, or the "
                        "probe does not exercise the mutated line -- CHECK WHICH "
                        "before reading this as 'not a coverage hole'."
                    )
                else:
                    status, note = "SURVIVED", (
                        f"behaviour confirmed changed ({clean_probe!r} -> "
                        f"{mutated_probe!r}) and no named test caught it"
                    )
                results.append({"id": m.mid, "area": m.area, "status": status,
                                "tests": tests, "note": note})
                print(f"  {m.mid:4} {m.area:12} {status:11} {note}")
        finally:
            (root / m.path).write_text(original)

    # Only the files this run mutated. The check previously flagged ANY dirty
    # file, so an unrelated work-in-progress edit made the harness report its own
    # cleanup as failed — a false alarm that trains you to ignore the one signal
    # that says "a mutation was left applied to your source tree".
    mutated_paths = {m.path for m in MUTATIONS if not only or m.mid in only}
    rc, out = _run(["git", "status", "--porcelain"], root)
    dirty = [
        line for line in out.splitlines()
        if line.strip() and not line.startswith("??")
        and any(p in line for p in mutated_paths)
    ]
    scored = [r for r in results if r["status"] in ("killed", "SURVIVED")]
    killed_n = sum(1 for r in scored if r["status"] == "killed")
    equivalent = [r["id"] for r in results if r["status"] == "EQUIVALENT"]
    unverified = [r["id"] for r in results if r["status"] == "UNVERIFIED"]

    # Reported, not just excluded. Dropping mutations from the denominator
    # silently would let the score be improved by writing mutations that cannot
    # change behaviour -- the same gaming the frozen sample exists to prevent.
    if equivalent:
        print(f"\n  EQUIVALENT (excluded, PROBE saw no difference): {', '.join(equivalent)}")
    if unverified:
        print(f"  UNVERIFIED (excluded, no usable probe):      {', '.join(unverified)}")

    return {
        "results": results,
        "killed": killed_n,
        "scored": len(scored),
        "equivalent": equivalent,
        "unverified": unverified,
        "not_applicable": len(results) - len(scored) - len(equivalent) - len(unverified),
        "score": (killed_n / len(scored)) if scored else None,
        "worktree_clean_after": not dirty,
        "dirty_after": dirty,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=".venv/bin/python")
    ap.add_argument("--baseline-sha", nargs="?", const=BASELINE_SHA,
                    help="run the sample in a worktree at this SHA instead of HEAD")
    ap.add_argument("--only", nargs="*", help="restrict to these mutation ids")
    ap.add_argument("--json", help="write results to this path")
    args = ap.parse_args()

    if args.baseline_sha:
        print(f"Mutation sample at BASELINE {args.baseline_sha[:7]}")
        wt = REPO / ".mutation-baseline-worktree"
        _run(["git", "worktree", "remove", "--force", str(wt)], REPO)
        rc, out = _run(["git", "worktree", "add", "--detach", str(wt), args.baseline_sha], REPO)
        if rc != 0:
            print(out, file=sys.stderr)
            return 2
        try:
            python = str(REPO / args.python) if not Path(args.python).is_absolute() else args.python
            report = evaluate(wt, python)
        finally:
            _run(["git", "worktree", "remove", "--force", str(wt)], REPO)
    else:
        print("Mutation sample at HEAD")
        report = evaluate(REPO, args.python, args.only)

    score = report["score"]
    # Always print the denominator next to the score. `score = killed / scored`
    # excludes n/a and invalid mutations, so a run where nine of ten silently
    # stopped applying would report "1.00" off a single probe. That is the same
    # defect WP-07 fixed in the product -- a rate that quietly redefines its own
    # denominator -- and a gate is the last place it belongs.
    total = len(MUTATIONS) if not args.only else len(args.only)
    print(f"\nkilled {report['killed']}/{report['scored']} scored"
          f"  ({report['not_applicable']} not scored, of {total} declared)"
          f"  score={'n/a' if score is None else f'{score:.2f}'}")
    if report["scored"] < total:
        print(f"WARNING: only {report['scored']}/{total} mutations were scored. "
              f"A score over a reduced denominator is not comparable across SHAs.")

    # Persist BEFORE any verdict check. The first version returned early on
    # "<8 scored" and so wrote nothing for the baseline run -- which is exactly
    # the run that is SUPPOSED to score fewer than 8, because most mutations
    # target code the remediation created. The guard destroyed the artifact it
    # was meant to qualify.
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
        print(f"wrote {args.json}")

    # A baseline run legitimately scores few mutations; only a HEAD run claims a
    # verdict, so only a HEAD run can fail this way.
    if not args.baseline_sha and report["scored"] < 8:
        print("FAIL: fewer than 8 mutations scored — the sample cannot support "
              "a WP-14 or G-F verdict.", file=sys.stderr)
        return 4
    if not report["worktree_clean_after"]:
        print("FAIL: worktree dirty after run:", report["dirty_after"], file=sys.stderr)
        return 3
    print("worktree clean after run ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
