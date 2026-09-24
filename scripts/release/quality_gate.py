#!/usr/bin/env python3
"""Does a release regress the local backend's ANSWER quality, not just pass its
tests? North Star point 7: "a quality regression blocks a release." This is the
second half of that gate — `classifier_gate.py` checks routing decisions;
this checks whether the model actually did the work.

## What this runs

A PINNED pair of tasks from `scripts/bench_backend_quality.py` against the
`local` backend (the only backend a release gate can run without a paid API
key or a `claude`/`codex` CLI login). Method and scoring are that script's,
not reinvented here — see docs/BACKEND-QUALITY.md.

## Why an easy task AND a hard task (independent review, round 2, PR #146)

The first version of this gate pinned two EASY-suite tasks. docs/BACKEND-
QUALITY.md documents the easy suite as having "no signal": local, codex and
claude all tie at 24/25 correct — an easy-only pin can only catch total
breakage, not a quality regression specific to the local backend, which is
exactly what this gate exists to catch. The HARD suite is where the gap
actually shows (local 6/10 vs codex/claude 10/10), so the pinned pair is now:

    EASY_TASK = "qa-max-value"   -- smoke test: catches total breakage
    HARD_TASK = "hd-last-page"   -- the task that actually carries signal

`hd-last-page` was chosen because docs/BACKEND-QUALITY.md's "What actually
failed" section names local's exact 2026-09-12 misses, and this is not one of
them: local's 4 hard-suite misses are `hd-name-the-bug`, `hd-swallows`,
`hd-paginate-count` (all QA, 0/3) and `hd-mutable-default` (EDIT, diagnosed
correctly in prose but never changed the code) — 4 misses out of 10 matches
the documented 6/10. `hd-last-page` is not named as a failure anywhere in that
section, so it is one of the 6 EDIT tasks local passed. **Never pin a task the
reference run failed** — that would make this gate red from the day it ships,
which is indistinguishable from a broken gate.

The one EASY-suite task local DID miss on 2026-09-12 is identifiable the same
way: the caveats section states plainly that `qa-report-output-v2` ("local
fails that too") is the anchored, unambiguous rewrite of a badly-worded
question, and the arithmetic checks out (24/25 easy, minus this one, is
consistent with the hard-suite count above using a single shared failure
model). `EASY_TASK` is `qa-max-value`, not that one.

## Why the pinned pair is only 2 tasks

docs/BACKEND-QUALITY.md reports a 3s median per easy-suite task, measured
2026-09-12, contention-free. Measured again here while first authoring this
gate (2026-09-24): 90-115s per task with every answer empty, because Ollama's
single model slot (Ollama.app caps at one loaded model, per this project's
own notes) was held by a different, concurrently-running model the whole
time. Two tasks keeps even a contended run inside a few minutes; see
"Detecting contention" below for what changed instead of trusting a slow run
to mean "regression."

## The threshold

`BASELINE_CORRECT` is pinned from docs/BACKEND-QUALITY.md's published,
contention-free 2026-09-12 measurement (local passes both `qa-max-value` and
`hd-last-page` in that run), not from a fresh run on this machine.

**Fresh baseline: PENDING, not claimed.** Checked at authoring time
(2026-09-24, this round of fixes) via `curl .../api/ps`: Ollama's one model
slot was held by `qwen3.8:latest`, not the pinned `qwen3-coder:30b` — the
exact contention condition below. Per this project's own measurement rules,
evicting another process's model to force a "clean" run is not itself clean
(it would just move the contention to whatever that process does next), so no
fresh run of the pinned pair was attempted or is claimed here. The next
person who runs this gate on a free Ollama should quote a real pass, at which
point this note should be replaced with that quote — not deleted silently.

## Detecting contention, not just unreachability (independent review, PR #146)

Round 1 checked only `is_ollama_available()` (`discover.py:39`), which probes
`/api/tags` — server-up, nothing more. Reproduced as a HIGH defect: Ollama can
be reachable while its one model slot is held by a different model, in which
case the gate ran, got empty answers at ~90-115s each, and reported "This is a
quality regression" — a false diagnosis of a resource-contention problem. Two
checks now run before the bench, in order:

1. `_resident_model_conflict()` — `GET /api/ps`. If a DIFFERENT model is
   resident, don't even try; contention is already visible.
2. `_warm_up_probe()` — a short, cheap `/api/generate` call (8 tokens) to the
   pinned model. Catches contention that starts between check 1 and the real
   run, or a model that is resident-but-stalled.

Either one firing is treated exactly like "unavailable": fail loud unless
`--skip-quality "<reason>"` is given. Separately, if contention starts AFTER
both checks pass (during the actual bench), each task's answer is inspected:
an EMPTY answer with no recorded error is classified as a backend failure
("no answer"), reported distinctly from a WRONG answer, and handled with the
same fail-loud/`--skip-quality` semantics rather than folded into "quality
regression" — because it isn't one; nothing was measured.

## Unavailable or contended backend: fail loud, not silent

    unavailable/contended, no --skip-quality   -> exit 1, release blocked
    unavailable/contended, --skip-quality R    -> exit 0, R printed + written to
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_router.discover import is_ollama_available  # noqa: E402

OLLAMA_BASE_URL = "http://localhost:11434"  # discover.py's own fallback default
# Kept in sync with bench_backend_quality.py's BACKENDS["local"] default via
# the same env var, so the contention pre-check watches the model the bench
# would actually request, not a guess.
TARGET_MODEL = os.environ.get("BENCH_LOCAL_MODEL", "qwen3-coder:30b")
WARMUP_TIMEOUT_S = 45  # generous enough for a cold model load, not a stall

# Pinned pair — see "Why an easy task AND a hard task" above.
EASY_TASK = "qa-max-value"
HARD_TASK = "hd-last-page"
PINNED: list[tuple[str, str]] = [("easy", EASY_TASK), ("hard", HARD_TASK)]
N_TASKS = len(PINNED)
# Pinned from docs/BACKEND-QUALITY.md — see "The threshold" above for why
# this is NOT taken from a fresh run on this machine (fresh baseline pending).
BASELINE_CORRECT = 2

DIST_DIR = ROOT / "dist"
SKIP_NOTE_PATH = DIST_DIR / "QUALITY_SKIPPED.txt"
BENCH_SCRIPT = ROOT / "scripts" / "bench_backend_quality.py"
BENCH_OUT = ROOT / "scripts" / "release_quality_gate_out"
BENCH_SANDBOX_ROOT = Path("/tmp/bq_release_quality_gate")


def _pinned_str() -> str:
    return ", ".join(f"{suite}/{task}" for suite, task in PINNED)


def _resident_model_conflict() -> str | None:
    """`GET /api/ps`: is Ollama's one model slot held by something other than
    the model this bench would ask for? Ollama.app caps at one loaded model
    (this project's own notes) — a different resident model means a request
    would contend for the slot rather than get a clean answer, which is
    exactly the condition measured live while authoring this gate: 90-115s
    per task, every answer empty, a different model resident throughout.

    Returns None on any probe failure — reachability is `is_ollama_available`'s
    job, not this one's; this function only ever adds a reason to refuse, never
    a reason to proceed past an unreachable server.
    """
    try:
        req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/ps")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 — a failed probe is not this check's finding
        return None
    resident = [m.get("model") or m.get("name") for m in data.get("models", [])]
    others = sorted({m for m in resident if m and m != TARGET_MODEL})
    if others:
        return (f"Ollama's one model slot is held by {', '.join(others)}, not "
                f"{TARGET_MODEL} — a request would contend for it rather than "
                "get a clean answer.")
    return None


def _warm_up_probe() -> str | None:
    """A short, cheap generation against the pinned model. Catches contention
    that starts AFTER `_resident_model_conflict` checks (another process can
    start between the two calls) and a model that is resident but stalled."""
    payload = json.dumps({
        "model": TARGET_MODEL,
        "prompt": "Reply with the single word: ready",
        "stream": False,
        "options": {"num_predict": 8},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=WARMUP_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 — timeout/refusal IS the finding here
        return (f"warm-up probe to {TARGET_MODEL} failed within "
                f"{WARMUP_TIMEOUT_S}s: {type(e).__name__}: {e}")
    text = (data.get("response") or "").strip()
    if not text:
        return f"warm-up probe to {TARGET_MODEL} returned an empty answer"
    return None


def _run_one(suite: str, task_id: str) -> dict:
    """Runs exactly one task through bench_backend_quality.py. Returns its
    result row. Raises RuntimeError if the bench aborted (its own
    BackendUnavailable exit code 2) or wrote no row for this task."""
    BENCH_OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # M-02 (bench_backend_quality.py): mark this as synthetic traffic so it
    # never counts toward the production routing ledger.
    env["LLM_ROUTER_SYNTHETIC"] = "1"
    env["BENCH_OUT"] = str(BENCH_OUT)
    env["BENCH_SANDBOX"] = str(BENCH_SANDBOX_ROOT / suite)
    result = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--backend", "local",
         "--suite", suite, "--only", task_id],
        cwd=ROOT, env=env,
    )
    if result.returncode == 2:
        raise RuntimeError(f"local backend became unavailable mid-run on "
                            f"{suite}/{task_id} (bench exit code 2)")
    suffix = "" if suite == "easy" else f"-{suite}"
    res_path = BENCH_OUT / f"local{suffix}.json"
    rows = json.loads(res_path.read_text()) if res_path.exists() else []
    matching = [r for r in rows if r.get("task") == task_id]
    if not matching:
        raise RuntimeError(f"{suite}/{task_id}: bench_backend_quality.py wrote "
                            "no result row for this task")
    return matching[-1]


def _run_bench() -> tuple[int, int, list[str], list[str]]:
    """Runs the pinned pair. Returns (correct, n, failing_ids, no_answer_ids).
    `no_answer_ids` is a subset of `failing_ids`: tasks that came back with an
    empty answer and no recorded error — a backend failure, not a wrong
    answer (see "Detecting contention" in the module docstring)."""
    correct = 0
    failing: list[str] = []
    no_answer: list[str] = []
    for suite, task_id in PINNED:
        row = _run_one(suite, task_id)
        label = f"{suite}/{task_id}"
        if row.get("correct"):
            correct += 1
            continue
        failing.append(label)
        if not (row.get("answer") or "").strip() and not row.get("error"):
            no_answer.append(label)
    return correct, len(PINNED), failing, no_answer


def _skip(reason: str, detail: str) -> int:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    note = (f"Answer-quality bench SKIPPED: {reason}\n"
            f"{detail}\n"
            f"Pinned pair not (fully) run: {_pinned_str()}.\n")
    SKIP_NOTE_PATH.write_text(note)
    print(f"⚠️  Quality bench SKIPPED: {reason}")
    print(f"   Reason recorded in {SKIP_NOTE_PATH} for the release notes.")
    return 0


def _fail_loud(detail_lines: list[str]) -> int:
    for line in detail_lines:
        print(line, file=sys.stderr)
    print("A release without this check ships an unmeasured quality change.",
          file=sys.stderr)
    print("Start Ollama and re-run, or explicitly accept the risk:",
          file=sys.stderr)
    print('  bash scripts/release/pre-release-verify.sh '
          '--skip-quality "<reason>"', file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-quality", metavar="REASON", default=None,
                    help="explicit reason to skip the bench when the local "
                         "backend is unavailable or contended; printed and "
                         "recorded, not silent")
    args = ap.parse_args(argv)

    if not is_ollama_available():
        if args.skip_quality:
            return _skip(args.skip_quality,
                        "Local backend (Ollama) was unreachable at release-verify time.")
        return _fail_loud([
            "Local backend (Ollama) is unreachable — the answer-quality bench "
            "did not run.",
        ])

    contention = _resident_model_conflict() or _warm_up_probe()
    if contention:
        if args.skip_quality:
            return _skip(args.skip_quality,
                        f"Local backend is CONTENDED, treated as unavailable: {contention}")
        return _fail_loud([
            "Local backend is reachable but CONTENDED, not just unreachable:",
            f"  {contention}",
            "Treating this as UNAVAILABLE rather than running a bench that "
            "would score contention as a quality regression.",
        ])

    try:
        correct, n, failing, no_answer = _run_bench()
    except RuntimeError as e:
        return _fail_loud([f"Answer-quality bench aborted: {e}"])

    print(f"quality {correct}/{n} >= baseline {BASELINE_CORRECT}/{N_TASKS} "
          f"(pinned pair: {_pinned_str()})")
    if failing:
        print(f"  failing: {', '.join(failing)}")
    if no_answer:
        print(f"  of which returned NO ANSWER (backend failure, not a wrong "
              f"answer): {', '.join(no_answer)}")

    if n != N_TASKS:
        print(f"expected {N_TASKS} task result(s), got {n} — treating as a "
              "failure.", file=sys.stderr)
        return 1

    if no_answer:
        # Contention that started AFTER the pre-run checks passed. Same
        # fail-loud/--skip-quality semantics as the pre-run check, framed as
        # what it is: the bench already spent the time, but this is still not
        # a quality measurement.
        detail = (f"backend returned no answer for: {', '.join(no_answer)} — "
                  "contention or starvation during the run, not caught by "
                  "the pre-run checks.")
        if args.skip_quality:
            return _skip(args.skip_quality, detail)
        return _fail_loud([
            "Local backend was CONTENDED during the run (too late for the "
            "pre-run checks to catch):",
            f"  {detail}",
        ])

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
