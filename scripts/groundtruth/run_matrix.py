#!/usr/bin/env python3
"""Run every candidate tier against every task and record acceptability.

    python3 scripts/groundtruth/run_matrix.py --version v1 --split tune \
        --tiers local,cheap,mid,premium --out outcomes.json

The output is the model x task matrix:

    {"gt-0001": {"local": {"accepted": false, "reason": "...", "cost_usd": ...},
                 "cheap": {"accepted": true,  ...}, ...}, ...}

Two rules this module exists to enforce:

1. **The router does not participate.** Each tier is called directly, so the
   label cannot be contaminated by what the router would have chosen. A ground
   truth derived from the system under test measures nothing.

2. **Acceptability comes from the task's own assertion**, never from a judge
   and never from comparing tiers to each other. A task with no assertion is
   not run at all — it is in `ambiguous.jsonl` and stays there.

Sampling: a model is stochastic, so one sample per (task, tier) gives a noisy
verdict. `--samples N` runs each cell N times and records the pass count; the
cell is accepted under `--accept-rule` (default "all", the conservative
choice: a tier that fails sometimes is not reliable enough to route to).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import dataset as ds  # noqa: E402
from groundtruth.verifiers import run_verifier  # noqa: E402

# Bumped whenever a verifier helper changes meaning, so an outcome row records
# which verifier semantics produced it and two runs are comparable or visibly not.
VERIFIER_VERSION = "gt-verifiers/2"

# Tier definitions. Ordered cheapest first — `label.py` relies on this order
# being a true cost ordering, so a new entry goes in its cost position.
# `model` is resolved against the router's provider registry at call time.
TIERS: dict[str, dict] = {
    "local": {"order": 0, "model": os.environ.get("GT_MODEL_LOCAL", "ollama/qwen3-coder:30b")},
    "cheap": {"order": 1, "model": os.environ.get("GT_MODEL_CHEAP", "openai/gpt-4o-mini")},
    "mid": {"order": 2, "model": os.environ.get("GT_MODEL_MID", "openai/gpt-4o")},
    "premium": {"order": 3, "model": os.environ.get("GT_MODEL_PREMIUM",
                                                    "anthropic/claude-sonnet-4-6")},
}


def _git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def call_model(model: str, prompt: str, *, timeout: int) -> tuple[str, float, str]:
    """Return (answer, cost_usd, error). Never raises.

    Uses litellm, which every provider in this repo already goes through, so
    the matrix sees the same model surface the router does.
    """
    try:
        import litellm  # noqa: PLC0415
    except ImportError:
        return "", 0.0, "litellm not installed"
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            timeout=timeout,
        )
        text = (resp.choices[0].message.content or "").strip()
        try:
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:  # noqa: BLE001 - cost is best-effort, the verdict is not
            cost = 0.0
        return text, cost, ""
    except Exception as exc:  # noqa: BLE001
        return "", 0.0, f"{type(exc).__name__}: {exc}"


#: A `sandbox` value that means "nobody has authored a fixture tree yet".
#: `author_tasks.py` writes this literal for every EDIT task it scaffolds.
_SANDBOX_PLACEHOLDER = "TODO"


def resolve_sandbox(root, task) -> tuple[object | None, str]:
    """Where this task's verifier must run, or why it cannot run at all.

    Returns `(cwd, refusal_reason)`. Exactly one is meaningful: a path and an
    empty reason, or `None` and a stated reason.

    T-06/F31 (audit 2026-09-22). `run_verifier` takes a `cwd`, and `run_matrix`
    never passed one. `sandbox` is documented as "fixture tree name for
    EDIT/repo-bound tasks", `author_tasks.py` writes the literal "TODO" into it,
    and nothing in the pipeline ever resolved it to a directory. So a repo-bound
    task's verifier ran in whatever directory the operator happened to be in —
    grading a claim about repo state against an unrelated tree, and reporting
    the verdict as a Ground Truth label.

    That is the same rule eligibility already applies as
    `no-replayer-for-required-state`: a task that needs repo state it cannot be
    given is REFUSED, not guessed at. Refusing produces an AMBIGUOUS row with a
    reason; grading in the wrong tree produces a confident lie.
    """
    name = (getattr(task, "sandbox", None) or "").strip()
    if not name:
        return None, ""          # not repo-bound; the CWD is irrelevant to it
    if name == _SANDBOX_PLACEHOLDER:
        return None, (f"sandbox fixture not authored (still {_SANDBOX_PLACEHOLDER!r}) "
                      f"— refusing rather than grading against an unrelated tree")
    tree = root / "sandboxes" / name
    if not tree.is_dir():
        return None, (f"sandbox fixture {name!r} not found at {tree} "
                      f"— refusing rather than grading against an unrelated tree")
    return tree, ""


def _content_task_id(prompt: str) -> str | None:
    """The pool's id for this prompt, or None if it cannot be derived.

    T-06 (audit 2026-09-22). Pool candidates are keyed `gtc-<content-hash>`;
    frozen dataset tasks are keyed `gt-<seq>` by `author_tasks.py`. The two
    namespaces never intersect, so `reg.active_for(t.task_id)` returned None for
    every pool-authored verifier — a candidate could pass eligibility, envelope,
    pool, propose, MUTATION VALIDATION and HUMAN SIGN-OFF and still never grade
    anything. The rigorous half of the subsystem was decorative.

    Both ids are derivable from the same place: `accumulate._task_id` is
    `gtc-{exact_key(prompt)}`. So a frozen task's prompt re-derives the pool id
    exactly, with no mapping table to drift.
    """
    if not prompt:
        return None
    try:
        from groundtruth.extract_corpus import exact_key
        return f"gtc-{exact_key(prompt)}"
    except Exception:  # noqa: BLE001 — a missing helper must not break the run
        return None


def _registry_record_for(reg, task):
    """ACTIVE verifier for *task*, looked up by BOTH identities (T-06)."""
    rec = reg.active_for(task.task_id)
    if rec:
        return rec
    alt = _content_task_id(getattr(task, "prompt", ""))
    return reg.active_for(alt) if alt else None


def _registry_any_for(reg, task):
    """Any verifier record for *task*, ACTIVE or not, under either identity."""
    rec = reg.get(task.task_id)
    if rec:
        return rec
    alt = _content_task_id(getattr(task, "prompt", ""))
    return reg.get(alt) if alt else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("--split", choices=("tune", "test"), default="tune")
    ap.add_argument("--reason", default="",
                    help="required when --split test: why you are reading held-out data")
    ap.add_argument("--root", type=Path, default=Path("data/groundtruth"))
    ap.add_argument("--tiers", default="local,cheap,mid,premium")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--accept-rule", choices=("all", "any", "majority"), default="all")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--out", default="")
    ap.add_argument("--use-registry", action="store_true",
                    help="also grade tasks with ACTIVE verifiers from the registry")
    ap.add_argument("--registry", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be called and the estimated cell count")
    args = ap.parse_args()

    root = args.root / args.version
    if not root.exists():
        raise SystemExit(f"no frozen dataset at {root} — run freeze.py first")

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    unknown = [t for t in tiers if t not in TIERS]
    if unknown:
        raise SystemExit(f"unknown tiers {unknown}; known: {list(TIERS)}")

    try:
        tasks = ds.load_split(root, args.split, reason=args.reason)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    runnable = [t for t in tasks if t.verifier_kind == ds.MECHANICAL and t.verifier]
    skipped = len(tasks) - len(runnable)

    # Part 15: a task may also be graded by a verifier authored through the
    # assistant — but ONLY once that verifier is ACTIVE. A PROPOSED or even
    # APPROVED verifier is a draft; grading with it would let a generated
    # assertion define truth, which is the one thing the authoring flow exists
    # to prevent.
    if args.use_registry:
        from groundtruth.verifier_registry import Registry
        reg = Registry(args.registry)
        adopted = 0
        unbridgeable = 0
        for t in tasks:
            if t.verifier:
                continue
            rec = _registry_record_for(reg, t)
            if not rec:
                continue
            snippet = (rec.proposal or {}).get("verifier_snippet")
            if not snippet:
                # T-06. The pytest and mutation-tested strategies populate
                # `proposed_files`, not `verifier_snippet`, and adopting those
                # means EXECUTING generated files against the repo. That is a
                # decision about trust, not a missing line of plumbing, so it is
                # refused out loud rather than skipped in silence — a candidate
                # that passed mutation validation and human sign-off and then
                # vanished here is exactly what made this subsystem decorative.
                unbridgeable += 1
                continue
            t.verifier = snippet
            t.verifier_kind = ds.MECHANICAL
            t.verification_type = rec.proposal.get("verification_class")
            runnable.append(t)
            adopted += 1
        if adopted:
            print(f"registry  adopted {adopted} ACTIVE verifier(s)")
        if unbridgeable:
            print(f"registry  {unbridgeable} ACTIVE verifier(s) REFUSED: they carry "
                  f"proposed_files (pytest/mutation strategy), which this runner "
                  f"does not execute. Not a silent skip — see T-06.")
        non_active = sum(1 for t in tasks if not t.verifier
                         and _registry_any_for(reg, t)
                         and not _registry_record_for(reg, t))
        if non_active:
            print(f"registry  {non_active} verifier(s) exist but are NOT ACTIVE — "
                  f"not used")

    print(f"dataset   {root}")
    print(f"split     {args.split}  ({len(tasks)} tasks, {len(runnable)} runnable, "
          f"{skipped} without a mechanical verifier)")
    print(f"tiers     {tiers}")
    print(f"cells     {len(runnable)} x {len(tiers)} x {args.samples} samples = "
          f"{len(runnable) * len(tiers) * args.samples} model calls")

    if not runnable:
        print("\nNOTHING TO RUN: no task in this split has a mechanical verifier.\n"
              "Author verifiers in the tasks file, re-freeze, and run again.",
              file=sys.stderr)
        return 3
    if args.dry_run:
        for t in tiers:
            print(f"  {t:8s} -> {TIERS[t]['model']}")
        return 0

    matrix: dict[str, dict] = {}
    started = time.monotonic()  # monotonic: this machine sleeps mid-run
    for i, task in enumerate(runnable, start=1):
        cell: dict[str, dict] = {}
        for tier in tiers:
            model = TIERS[tier]["model"]
            passes = 0
            graded = 0            # samples where the verifier actually ran
            cost = 0.0
            reasons: list[str] = []
            errors: list[str] = []
            answers: list[str] = []
            t0 = time.monotonic()
            sandbox_cwd, sandbox_refusal = resolve_sandbox(root, task)
            if sandbox_refusal:
                # F31: stated reason, never a silent grade in the wrong tree.
                errors.append(sandbox_refusal)
            for _ in range(args.samples if not sandbox_refusal else 0):
                answer, c, err = call_model(model, task.prompt, timeout=args.timeout)
                cost += c
                if err:
                    # The model never answered. That is a fact about the
                    # infrastructure, not about the model's capability, so it
                    # must not be counted as a FAIL.
                    errors.append(err)
                    continue
                answers.append(answer)
                ok, why = run_verifier(task.verifier or "", answer, cwd=sandbox_cwd)
                if why.startswith(("verifier timeout", "verifier crashed")):
                    # The verifier could not decide. Also not the model's fault.
                    errors.append(why)
                    continue
                graded += 1
                passes += ok
                if not ok:
                    reasons.append(why)
            latency_ms = (time.monotonic() - t0) * 1000.0

            # Three states. AMBIGUOUS is the default when evidence is missing;
            # PASS and FAIL each require the verifier to have actually spoken.
            if graded == 0:
                outcome = ds.AMBIGUOUS
                reason = (f"no sample could be graded ({len(errors)} error(s)): "
                          f"{errors[0][:120]}" if errors else "no sample was graded")
            elif graded < args.samples:
                # Partial evidence. Conservative: only call it if what we DID
                # grade is unanimous; a split verdict on partial data is not
                # sufficient evidence in either direction.
                if passes == graded:
                    outcome = ds.PASS
                    reason = f"{passes}/{graded} graded samples passed ({len(errors)} ungraded)"
                elif passes == 0:
                    outcome = ds.FAIL
                    reason = reasons[0] if reasons else "all graded samples failed"
                else:
                    outcome = ds.AMBIGUOUS
                    reason = (f"split verdict on partial evidence: {passes}/{graded} "
                              f"passed, {len(errors)} ungraded")
            elif args.accept_rule == "all":
                outcome = ds.PASS if passes == graded else ds.FAIL
                reason = "" if outcome == ds.PASS else (reasons[0] if reasons else "")
            elif args.accept_rule == "any":
                outcome = ds.PASS if passes > 0 else ds.FAIL
                reason = "" if outcome == ds.PASS else (reasons[0] if reasons else "")
            else:
                outcome = ds.PASS if passes * 2 > graded else ds.FAIL
                reason = "" if outcome == ds.PASS else (reasons[0] if reasons else "")

            cell[tier] = {
                "task_id": task.task_id,
                "model": model,
                "tier": tier,
                "tier_order": TIERS[tier]["order"],
                "outcome": outcome,
                # Kept so older readers of this file do not silently misread a
                # three-state matrix as a two-state one.
                "accepted": outcome == ds.PASS,
                "verification_type": getattr(task, "verification_type", None) or ds.V_MECHANICAL,
                "verifier": (task.verifier or "")[:200],
                "verifier_version": VERIFIER_VERSION,
                "confidence": ds.CONFIDENCE_HIGH if outcome != ds.AMBIGUOUS
                              else ds.CONFIDENCE_LOW,
                "evidence": reason,
                "errors": errors[:3],
                "passes": passes,
                "graded": graded,
                "samples": args.samples,
                "cost_usd": round(cost, 6),
                "latency_ms": round(latency_ms, 1),
                "answer_chars": [len(a) for a in answers],
                "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        matrix[task.task_id] = cell
        sym = {ds.PASS: "P", ds.FAIL: "F", ds.AMBIGUOUS: "?"}
        acc = "".join(sym[cell[t]["outcome"]] for t in tiers)
        print(f"  [{i:3d}/{len(runnable)}] {task.task_id:12s} {acc}  {task.prompt[:54]}")

    elapsed = time.monotonic() - started
    payload = {
        "dataset_version": args.version,
        "split": args.split,
        "tiers": {t: TIERS[t] for t in tiers},
        "samples": args.samples,
        "accept_rule": args.accept_rule,
        "tasks_run": len(runnable),
        "tasks_skipped_no_verifier": skipped,
        "elapsed_seconds": round(elapsed, 1),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verifier_version": VERIFIER_VERSION,
        "reproducibility": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_rev": _git_rev(),
            "temperature": 0.0,
            "note": ("Model outputs are not guaranteed reproducible; the "
                     "VERIFIER is. Re-running the verifier over stored answers "
                     "must give identical labels."),
        },
        "matrix": matrix,
    }
    out = Path(args.out) if args.out else root / f"outcomes.{args.split}.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}  ({elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
