#!/usr/bin/env python3
"""Does a release regress the local backend's ANSWER quality, not just pass its
tests? North Star point 7: "a quality regression blocks a release." This is the
second half of that gate — `classifier_gate.py` checks routing decisions;
this checks whether the model actually did the work.

## What this runs

A PINNED subset of `scripts/bench_backend_quality.py`'s "easy" suite against the
`local` backend (the only backend a release gate can run without a paid API key
or a `claude`/`codex` CLI login). Method and scoring are that script's, not
reinvented here — see docs/BACKEND-QUALITY.md.

## Why the subset is this small

docs/BACKEND-QUALITY.md reports a 3s median per easy-suite task, measured
2026-09-12. Measured again here while authoring this gate (2026-09-24), on a
machine with Ollama's single model slot (Ollama.app caps at one loaded model,
per this project's own notes) being held by a DIFFERENT, concurrently-running
model: 90-115s per task, and every one of 4 tasks tried came back with an
empty answer — `ollama ps` showed the other model still resident throughout,
never the one this bench asked for. That is contention, not a measurement of
`qwen3-coder:30b`'s quality, and CLAUDE.md's wall-clock warning ("a filter
that drops nothing has not been shown to work"; measure the thing that pays)
argues against pinning a threshold off a contaminated run. `PINNED_TASKS` is
kept to 2 tasks so that even a contended run finishes in a few
minutes rather than the ~10 the 6-task trial above was heading for; a wider
subset is available by raising `--tasks` on `bench_backend_quality.py`
directly for a manual check when Ollama is free, just not as a release gate.

## The threshold

`BASELINE_CORRECT` is pinned from docs/BACKEND-QUALITY.md's PUBLISHED
easy-suite measurement — 24/25 correct, local backend, qwen3-coder:30b,
2026-09-12, contention-free — as the task instructions for this gate direct,
NOT from a fresh run on this shared machine: today's attempt (above) was
contaminated by a concurrent process holding Ollama's one model slot, and
pinning a threshold from that would encode today's resource contention as
"the correct answer" rather than a property of the model. 24/25 = 96%; at
that measured miss rate, requiring both of a 2-task pinned subset to pass is
the natural least-loss threshold, not a stricter bar invented for this gate.
A run of this gate WILL fail on a machine where something else is holding
Ollama's model slot — that is a real, not a false, quality problem for a
release: the local backend a release ships is the one that has to answer
under whatever load a real host has.

## Unavailable backend: fail loud, not silent

If Ollama is not reachable, running the bench would either hang until the
per-task timeout or score every task as a failure that has nothing to do with
model quality (`BackendUnavailable` in bench_backend_quality.py exists for the
same reason on the cloud CLIs). This script checks reachability BEFORE
attempting the bench and refuses to proceed on an unmeasured population:

    unavailable, no --skip-quality   -> exit 1, release blocked
    unavailable, --skip-quality R    -> exit 0, R printed + written to
                                         dist/QUALITY_SKIPPED.txt

`--skip-quality` is an explicit, human-typed override — the reason is required
and is not a free pass to skip silently; it is printed to the terminal the
person cutting the release is looking at, and to a file so it can be copied
into the release notes. Note: `release.sh`'s publish step does `rm -rf dist/`
before building the wheel, which would delete this file if it runs later in the
same session — the terminal output at verify-time is the durable record; the
file is a convenience for copy-pasting it into the same release's notes before
that happens.

    python3 scripts/release/quality_gate.py
    python3 scripts/release/quality_gate.py --skip-quality "no GPU on this CI runner"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_router.discover import is_ollama_available  # noqa: E402

PINNED_SUITE = "easy"
# Kept intentionally small — see "Why the subset is this small" above. One QA
# task (unambiguous factual lookup, not one of the two easy-suite questions
# docs/BACKEND-QUALITY.md flags as badly worded) and one EDIT task (scored by
# importing the result and asserting on it, not by text matching).
PINNED_TASKS = ["qa-max-value", "ed-add-offset"]
# Pinned from docs/BACKEND-QUALITY.md's published easy-suite result (24/25
# correct, local/qwen3-coder:30b, 2026-09-12) — see "The threshold" above for
# why this is NOT taken from a fresh run on this machine.
BASELINE_CORRECT = 2
N_TASKS = len(PINNED_TASKS)

DIST_DIR = ROOT / "dist"
SKIP_NOTE_PATH = DIST_DIR / "QUALITY_SKIPPED.txt"
BENCH_SCRIPT = ROOT / "scripts" / "bench_backend_quality.py"
BENCH_OUT = ROOT / "scripts" / "release_quality_gate_out"
BENCH_SANDBOX = Path("/tmp/bq_release_quality_gate")


def _run_bench() -> tuple[int, int, list[str]]:
    """Runs the pinned subset. Returns (correct, n, [failing task ids])."""
    BENCH_OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # M-02 (bench_backend_quality.py): mark this as synthetic traffic so it
    # never counts toward the production routing ledger.
    env["LLM_ROUTER_SYNTHETIC"] = "1"
    env["BENCH_OUT"] = str(BENCH_OUT)
    env["BENCH_SANDBOX"] = str(BENCH_SANDBOX)
    result = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--backend", "local",
         "--suite", PINNED_SUITE, "--only", ",".join(PINNED_TASKS)],
        cwd=ROOT, env=env,
    )
    if result.returncode == 2:
        # bench_backend_quality.py's own BackendUnavailable abort (see its
        # main()) — the backend answered with a refusal/quota string mid-run.
        # Treat exactly like "unavailable" rather than scoring a partial subset.
        raise RuntimeError("local backend became unavailable mid-run "
                            f"(bench exit code {result.returncode})")

    res_path = BENCH_OUT / "local.json"
    rows = json.loads(res_path.read_text()) if res_path.exists() else []
    correct = sum(1 for r in rows if r["correct"])
    failing = [r["task"] for r in rows if not r["correct"]]
    return correct, len(rows), failing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-quality", metavar="REASON", default=None,
                    help="explicit reason to skip the bench when the local "
                         "backend is unavailable; printed and recorded, not silent")
    args = ap.parse_args(argv)

    if not is_ollama_available():
        if args.skip_quality:
            DIST_DIR.mkdir(parents=True, exist_ok=True)
            note = (f"Answer-quality bench SKIPPED: {args.skip_quality}\n"
                    f"Local backend (Ollama) was unreachable at release-verify time.\n"
                    f"Pinned subset not run: {', '.join(PINNED_TASKS)} "
                    f"(suite={PINNED_SUITE}).\n")
            SKIP_NOTE_PATH.write_text(note)
            print(f"⚠️  Quality bench SKIPPED: {args.skip_quality}")
            print(f"   Reason recorded in {SKIP_NOTE_PATH} for the release notes.")
            return 0
        print("Local backend (Ollama) is unreachable — the answer-quality bench "
              "did not run.", file=sys.stderr)
        print("A release without this check ships an unmeasured quality change.",
              file=sys.stderr)
        print("Start Ollama and re-run, or explicitly accept the risk:",
              file=sys.stderr)
        print('  bash scripts/release/pre-release-verify.sh '
              '--skip-quality "<reason>"', file=sys.stderr)
        return 1

    try:
        correct, n, failing = _run_bench()
    except RuntimeError as e:
        print(f"Answer-quality bench aborted: {e}", file=sys.stderr)
        print("A release without this check ships an unmeasured quality change.",
              file=sys.stderr)
        return 1

    print(f"quality {correct}/{n} >= baseline {BASELINE_CORRECT}/{N_TASKS} "
          f"(suite={PINNED_SUITE}, backend=local)")
    if failing:
        print(f"  failing: {', '.join(failing)}")

    if n != N_TASKS:
        print(f"expected {N_TASKS} task result(s), got {n} — "
              "bench_backend_quality.py's --only did not return the full "
              "pinned subset; treating as a failure.", file=sys.stderr)
        return 1

    if correct < BASELINE_CORRECT:
        print(f"\nquality {correct}/{n} is below the pinned baseline "
              f"{BASELINE_CORRECT}/{N_TASKS}. This is a quality regression: "
              "investigate before releasing, or if the drop is expected "
              "(e.g. a deliberate model change), update BASELINE_CORRECT in "
              "this file in a commit that says why.")
        return 1

    print("\nanswer-quality gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
