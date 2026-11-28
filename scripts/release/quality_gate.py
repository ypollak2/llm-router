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

**Fresh baseline: CONFIRMED, 2026-09-25.** Authoring time (2026-09-24) found
Ollama's one model slot held by `qwen3.8:latest`, not the pinned
`qwen3-coder:30b` — the exact contention condition below — so no fresh run
was attempted that day. The next day, with the owner's brief authorisation to
pause whatever held the slot (`ollama stop qwen3.8:latest` immediately before
each run — a transient unload, no persistent config touched, and the slot
returned to its prior occupant afterwards), the pinned pair ran 3 times back
to back against `qwen3-coder:30b`, clean every time (no resident-model note,
no warm-up-probe failure):

    date:     2026-09-25
    model:    qwen3-coder:30b (local backend)
    n:        3/3 runs, both tasks, no contention on any run
    qa-max-value (easy):  3/3 correct — 6.8s, 6.3s, 6.3s (median 6.3s)
    hd-last-page (hard):  3/3 correct — 5.1s, 5.2s, 5.5s (median 5.2s)

This confirms `BASELINE_CORRECT = 2/2` on this machine, not just on
2026-09-12's. If a future run drops below this, it is a real regression to
investigate, not an unmeasured pin.

## Detecting contention, not just unreachability (independent review, PR #146)

Round 1 checked only `is_ollama_available()` (`discover.py:39`), which probes
`/api/tags` — server-up, nothing more. Reproduced as a HIGH defect: Ollama can
be reachable while its one model slot is held by a different model, in which
case the gate ran, got empty answers at ~90-115s each, and reported "This is a
quality regression" — a false diagnosis of a resource-contention problem.

Round 1's FIX over-corrected, per the same reviewer, round 2: it treated ANY
other resident model as contention by itself, via `_resident_model_conflict()
or _warm_up_probe()`. That is a false positive — Ollama's single-slot cap
means a request for the pinned model simply evicts whatever is idly resident
and loads its own; an idle different model costs at most one swap, not a
stalled answer. As written, the gate refused a release whenever some other
model merely happened to be loaded, which trains people to pass
`--skip-quality` by reflex on a machine that was never actually contended.

The check now runs two functions with different jobs, in order:

1. `_resident_model_note()` — `GET /api/ps`. If a DIFFERENT model is resident,
   this is CONTEXT, not a verdict: printed as a note (visible either way) and
   folded into the contention message if the probe below fails, but it never
   blocks a release by itself.
2. `_warm_up_probe()` — a short, cheap `/api/generate` call (8 tokens) to the
   pinned model, bounded by `WARMUP_TIMEOUT_S = 45`. THIS is what decides
   contention: an empty answer or a timeout. The bound is 45s because the
   model file is ~17GB (`qwen3-coder:30b`), so a genuinely idle slot being
   evicted and reloaded from disk is dominated by I/O and comfortably fits
   inside 45s even on a slow disk — Ollama can only run one generation per
   slot, so if something else is ACTIVELY generating, the probe queues behind
   it rather than getting evicted-and-served, and that queue is what the
   timeout catches. A resident-different-model note with a probe that still
   answers promptly means "proceed" — that is exactly the "idle, not
   contended" case this round's fix exists for.

A failing probe is treated exactly like "unavailable": fail loud unless
`--skip-quality "<reason>"` is given. Separately, if contention starts AFTER
both checks pass (during the actual bench), each task's answer is inspected:
an EMPTY answer with no recorded error is classified as a backend failure
("no answer"), reported distinctly from a WRONG answer, and handled with the
same fail-loud/`--skip-quality` semantics rather than folded into "quality
regression" — because it isn't one; nothing was measured.

## A second Ollama server: check GPU memory BEFORE the probe loads anything

The warm-up probe itself loads `TARGET_MODEL`. When the router's main local
model lives on its own Ollama server (`LLM_ROUTER_LOCAL_MODEL_URL`, e.g.
`:11435`), that server's resident model and the gate's model share one Apple
GPU, and the two servers cannot evict each other. 2026-11-27, 48 GB Mac:
qwen3.6:35b-a3b-coding (22.4 GB, pinned on :11435) plus qwen3-coder:30b
(18.6 GB, loaded on :11434 by this gate) = 40.9 GB of 51.5 GB RAM. Ollama
logged Metal OutOfMemory, the probe got an empty answer, and the :11435 runner
wedged ("decode() failed: Compute error") until it was stopped and reloaded.
That happened at 14:49Z and 14:50Z (this gate, PRV step 12) and at 15:44Z (a
by-hand probe).

`_peer_memory_conflict()` runs first and only reads (`GET /api/ps`,
`GET /api/tags`): if `TARGET_MODEL` is not already resident here and the
peer servers' resident bytes plus `TARGET_MODEL`'s file size exceed
`GPU_MEMORY_FRACTION` (0.75, Metal's usual working-set cap) of physical RAM,
the gate refuses, with the same fail-loud / `--skip-quality` semantics as
contention, and never sends the probe. Peers: `LLM_ROUTER_LOCAL_MODEL_URL`
(environment, else the router's state `.env`) plus a comma-separated
`QUALITY_GATE_PEER_OLLAMA_URLS`. To run the gate, free the peer first
(`OLLAMA_HOST=<peer> ollama stop <model>`) and reload it afterwards.

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
# 45s: qwen3-coder:30b is an ~17GB model file, so an idle-slot evict+reload is
# I/O-bound and comfortably fits in 45s; a slot that's ACTIVELY generating for
# another caller queues our probe behind it (Ollama runs one generation per
# slot), which is exactly what this bound is meant to catch. See "Detecting
# contention" above.
WARMUP_TIMEOUT_S = 45
# Share of physical RAM that resident models on this server's peers plus
# TARGET_MODEL may use before loading TARGET_MODEL would overrun the GPU — see
# "A second Ollama server" above (40.9 of 51.5 GB OOMed: 0.79 > 0.75).
GPU_MEMORY_FRACTION = 0.75
PEER_URLS_ENV = "QUALITY_GATE_PEER_OLLAMA_URLS"
LOCAL_MODEL_URL_ENV = "LLM_ROUTER_LOCAL_MODEL_URL"

# Pinned pair — see "Why an easy task AND a hard task" above.
EASY_TASK = "qa-max-value"
HARD_TASK = "hd-last-page"
PINNED: list[tuple[str, str]] = [("easy", EASY_TASK), ("hard", HARD_TASK)]
N_TASKS = len(PINNED)
# Pinned from docs/BACKEND-QUALITY.md's 2026-09-12 measurement and confirmed
# by a fresh 3-run pass on this machine on 2026-09-25 — see "The threshold"
# above for the dated baseline record.
BASELINE_CORRECT = 2
FRESH_BASELINE_DATE = "2026-09-25"
FRESH_BASELINE_N = 3

DIST_DIR = ROOT / "dist"
SKIP_NOTE_PATH = DIST_DIR / "QUALITY_SKIPPED.txt"
BENCH_SCRIPT = ROOT / "scripts" / "bench_backend_quality.py"
BENCH_OUT = ROOT / "scripts" / "release_quality_gate_out"
BENCH_SANDBOX_ROOT = Path("/tmp/bq_release_quality_gate")


def _pinned_str() -> str:
    return ", ".join(f"{suite}/{task}" for suite, task in PINNED)


def _resident_model_note() -> str | None:
    """`GET /api/ps`: is a DIFFERENT model currently resident in Ollama's one
    model slot? This is CONTEXT, not a verdict — Ollama evicts an idle
    resident model and loads the requested one on the next call, so a
    different model being loaded costs at most one swap, not a stalled
    answer. (Round 1 of this gate treated this by itself as contention, a
    false positive the reviewer caught: it refused a release whenever some
    OTHER model merely happened to be loaded, which trains people to reach
    for `--skip-quality` on a machine that was never actually contended.)

    Returns None on any probe failure, or when the resident model already
    matches `TARGET_MODEL`, or when nothing is loaded — "nothing to report",
    not "clean". Whether this is actually contention is `_warm_up_probe`'s
    call, not this function's.
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
        return (f"Ollama's one model slot is currently held by "
                f"{', '.join(others)}, not {TARGET_MODEL}")
    return None


def _get_json(url: str) -> dict | None:
    """GET `url` as JSON; None on any failure (a peer that does not answer
    holds no memory this check can see)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None


def _state_env_value(key: str) -> str:
    """`key` from the router's state `.env` (where the live router reads
    LLM_ROUTER_LOCAL_MODEL_URL), or "" when absent/unreadable."""
    try:
        from llm_router import paths
        env_file = paths.regular_file_or_none(paths.state_path(".env"))
        if env_file is None:
            return ""
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _peer_ollama_urls() -> list[str]:
    """Other Ollama servers on this machine whose resident models share the GPU
    with OLLAMA_BASE_URL's. See "A second Ollama server" above."""
    local_model_url = (os.environ.get(LOCAL_MODEL_URL_ENV, "").strip()
                       or _state_env_value(LOCAL_MODEL_URL_ENV))
    raw = [local_model_url, *os.environ.get(PEER_URLS_ENV, "").split(",")]
    base = OLLAMA_BASE_URL.rstrip("/").replace("127.0.0.1", "localhost")
    urls: list[str] = []
    for u in raw:
        u = u.strip().rstrip("/")
        if u and u.replace("127.0.0.1", "localhost") != base and u not in urls:
            urls.append(u)
    return urls


def _physical_memory_bytes() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def _peer_memory_conflict() -> str | None:
    """Would loading TARGET_MODEL next to the peers' resident models overrun
    GPU memory? Read-only (`/api/ps`, `/api/tags`); returns the reason, or None
    when it fits or cannot be decided. See "A second Ollama server" above."""
    peers = _peer_ollama_urls()
    if not peers:
        return None
    here = _get_json(f"{OLLAMA_BASE_URL}/api/ps") or {}
    if any((m.get("model") or m.get("name")) == TARGET_MODEL
           for m in here.get("models", [])):
        return None  # already loaded: the probe adds nothing
    resident: list[str] = []
    peer_bytes = 0
    for url in peers:
        ps = _get_json(f"{url}/api/ps")
        if ps is None:
            print(f"note: peer Ollama {url} did not answer /api/ps; its GPU "
                  "memory is not counted.")
        for m in (ps or {}).get("models", []):
            size = int(m.get("size") or 0)
            peer_bytes += size
            resident.append(f"{m.get('model') or m.get('name')} on {url} "
                            f"({size / 1e9:.1f} GB)")
    if not peer_bytes:
        return None
    tags = _get_json(f"{OLLAMA_BASE_URL}/api/tags") or {}
    target_bytes = next((int(m.get("size") or 0) for m in tags.get("models", [])
                         if (m.get("model") or m.get("name")) == TARGET_MODEL), 0)
    total = _physical_memory_bytes()
    if not target_bytes or not total:
        print(f"note: GPU-memory check undecided ({TARGET_MODEL} size "
              f"{'unknown' if not target_bytes else 'known'}, RAM "
              f"{'unknown' if not total else 'known'}); peers hold "
              f"{peer_bytes / 1e9:.1f} GB. Proceeding to the probe.")
        return None
    need = peer_bytes + target_bytes
    if need <= GPU_MEMORY_FRACTION * total:
        return None
    return (f"loading {TARGET_MODEL} ({target_bytes / 1e9:.1f} GB) next to "
            f"{', '.join(resident)} needs {need / 1e9:.1f} GB, over "
            f"{GPU_MEMORY_FRACTION:.0%} of {total / 1e9:.1f} GB RAM; that "
            "overruns GPU memory (Metal OutOfMemory) and can wedge the peer. "
            "Free the peer first: OLLAMA_HOST=<peer> ollama stop <model>")


def _warm_up_probe() -> str | None:
    """A short, cheap generation against the pinned model. THIS is what
    decides contention (see "Detecting contention" in the module docstring)
    — an empty answer or a timeout means the slot is actually busy, not just
    holding a different, idle model. Bounded by `WARMUP_TIMEOUT_S`."""
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

    # Printed on EVERY run, pass or fail, so a releaser sees it in the terminal
    # output and not only in a comment they may never open. See "The threshold"
    # above for the reasoning and the dated baseline record.
    print(f"note: BASELINE_CORRECT ({BASELINE_CORRECT}/{N_TASKS}) is pinned from "
          "docs/BACKEND-QUALITY.md's 2026-09-12 measurement, confirmed by a fresh "
          f"{FRESH_BASELINE_N}/{FRESH_BASELINE_N}-run pass on {FRESH_BASELINE_DATE} "
          "(qwen3-coder:30b; qa-max-value median 6.3s, hd-last-page median 5.2s).")

    if not is_ollama_available():
        if args.skip_quality:
            return _skip(args.skip_quality,
                        "Local backend (Ollama) was unreachable at release-verify time.")
        return _fail_loud([
            "Local backend (Ollama) is unreachable — the answer-quality bench "
            "did not run.",
        ])

    # Before the probe: the probe itself loads TARGET_MODEL. See "A second
    # Ollama server" above.
    overload = _peer_memory_conflict()
    if overload:
        if args.skip_quality:
            return _skip(args.skip_quality,
                        f"Local backend would OVERRUN GPU memory, not run: {overload}")
        return _fail_loud([
            "Local backend would OVERRUN GPU memory next to another Ollama "
            "server; the bench was not started:",
            f"  {overload}",
        ])

    # A different resident model is CONTEXT, not a verdict — see
    # _resident_model_note's docstring and "Detecting contention" above. It is
    # printed either way and folded into the message only if the probe fails.
    resident_note = _resident_model_note()
    if resident_note:
        print(f"note: {resident_note} — not contention by itself; only a "
              "failed/empty/slow warm-up probe is.")

    probe_failure = _warm_up_probe()
    if probe_failure:
        contention = probe_failure + (f" ({resident_note})" if resident_note else "")
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
