#!/usr/bin/env python3
"""Work through the verifier-authoring backlog.

    verifier_cli.py suggest --limit 20      propose verifiers, ranked
    verifier_cli.py show <task_id>          task, contract, code, mutations
    verifier_cli.py validate <task_id>      run the mutation gate
    verifier_cli.py approve <task_id> --by me
    verifier_cli.py activate <task_id> --by me
    verifier_cli.py reject <task_id> --by me --reason "..."
    verifier_cli.py report

`approve` requires `--by` and refuses the value "assistant": the whole design
rests on a person, not this program, deciding that a verifier measures the right
thing. `activate` is separate again, so "I read it" and "start grading with it"
are two deliberate acts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import mutants as mut  # noqa: E402
from groundtruth import pool as poolmod  # noqa: E402
from groundtruth import propose as pr  # noqa: E402
from groundtruth import verifier_registry as vreg  # noqa: E402


def _pool(args) -> poolmod.Pool:
    return poolmod.Pool(getattr(args, "pool", None))


def _registry(args) -> vreg.Registry:
    return vreg.Registry(getattr(args, "registry", None))


def _eligible(pool: poolmod.Pool) -> list:
    return pool.in_state(poolmod.READY_FOR_REPLAY, poolmod.ELIGIBLE)


def cmd_suggest(args) -> int:
    pool, reg = _pool(args), _registry(args)
    cands = _eligible(pool)
    if args.task_type:
        cands = [c for c in cands if c.task_type == args.task_type]
    if args.with_tests:
        cands = [c for c in cands if (c.envelope or {}).get("test_command")]
    if not cands:
        print("No eligible candidates. Accumulate first "
              "(LLM_ROUTER_GROUND_TRUTH=1), then run this again.")
        return 0

    ranked = vreg.rank(cands, reg)[: args.limit or 20]
    made = 0
    for score, cand, why in ranked:
        if reg.get(cand.task_id) and not args.force:
            continue
        p = pr.propose(cand)
        rec = vreg.VerifierRecord(task_id=cand.task_id, proposal=p.to_json(),
                                  created_at=__import__("time").time())
        reg.save(rec)
        made += 1
        contract = p.acceptance_contract
        print(f"\n{cand.task_id}   priority {score:.0f}")
        print(f"  task           {(p.acceptance_contract.get('task') or '')[:72]}")
        print(f"  strategy       {p.verification_strategy}  ({p.verification_class})")
        print(f"  contract       {len(contract.get('required', []))} required, "
              f"{len(contract.get('invariants', []))} invariants, "
              f"{len(contract.get('optional', []))} optional (excluded from PASS)")
        print(f"  existing tests {len(p.existing_evidence)} reused-candidate(s)")
        print(f"  new verifier   {'yes' if p.new_verifier_required else 'no'}")
        print(f"  confidence     {p.provisional_confidence}  "
              f"(no evidence yet — run `validate`)")
        print(f"  status         {rec.status}")
        if why:
            print(f"  why now        {'; '.join(why[:3])}")
        for b in p.blockers:
            print(f"  BLOCKER        {b}")
        for r in p.risks[:2]:
            print(f"  risk           {r}")
    print(f"\n{made} proposal(s) written to {reg.path}")
    print("None is trusted. Run `validate`, then `approve --by <you>`.")
    return 0


def cmd_show(args) -> int:
    reg = _registry(args)
    rec = reg.get(args.task_id)
    if not rec:
        print(f"no verifier record for {args.task_id}", file=sys.stderr)
        return 1
    p = rec.proposal
    c = p.get("acceptance_contract", {})
    print(f"task_id    {rec.task_id}")
    print(f"status     {rec.status}     confidence {rec.confidence}")
    print(f"strategy   {p.get('verification_strategy')}")
    print(f"\ntask:\n  {(c.get('task') or '')[:400]}")
    print("\nacceptance contract")
    for tier in ("required", "invariants", "optional"):
        rows = c.get(tier, [])
        label = tier.upper() if tier != "optional" else "optional (NOT part of PASS)"
        print(f"  {label}:")
        for cond in rows or []:
            print(f"    - {cond['text']}")
        if not rows:
            print("    (none)")
    if c.get("unclear"):
        print("  UNCLEAR — a human must settle these:")
        for u in c["unclear"]:
            print(f"    ? {u}")
    print(f"\nexisting evidence: {p.get('existing_evidence')}")
    print(f"  rationale: {p.get('existing_rationale')}")
    if p.get("gaming_guards"):
        print("\ngaming guards:")
        for g in p["gaming_guards"]:
            print(f"  - {g}")
    for f in p.get("proposed_files", []):
        print(f"\n--- proposed file: {f['path']}\n")
        print(f["content"])
    if p.get("verifier_snippet"):
        print(f"\n--- verifier snippet\n{p['verifier_snippet']}")
    if rec.validation:
        v = rec.validation
        print(f"\nvalidation: baseline_passed={v.get('baseline_passed')} "
              f"mutants {v.get('mutants_detected')}/{v.get('mutants_applied')} detected")
        print(f"  {v.get('rationale')}")
        for r in v.get("results", []):
            if r.get("applied"):
                mark = "killed" if r.get("detected") else "SURVIVED"
                print(f"    {mark:9s} {r['name']}: {r.get('description','')}")
    else:
        print("\nvalidation: none — this verifier is unproven")
    return 0


def cmd_validate(args) -> int:
    reg = _registry(args)
    rec = reg.get(args.task_id)
    if not rec:
        print(f"no verifier record for {args.task_id}", file=sys.stderr)
        return 1
    p = rec.proposal
    files = p.get("proposed_files") or []
    snippet = p.get("verifier_snippet")
    contract = p.get("acceptance_contract", {})
    complete = bool(contract.get("required")) and not contract.get("unclear")

    if files and args.target and args.target_source:
        v = mut.validate_pytest_verifier(
            task_id=rec.task_id, test_source=files[0]["content"],
            target_path=args.target,
            target_source=Path(args.target_source).read_text(encoding="utf-8"))
    elif snippet and args.good_answer:
        v = mut.validate_snippet_verifier(
            task_id=rec.task_id, snippet=snippet,
            good_answer=args.good_answer, bad_answers=args.bad_answer or [])
    else:
        print("Nothing to validate against.\n"
              "  pytest verifier : --target <path> --target-source <file>\n"
              "  snippet verifier: --good-answer <text> --bad-answer <text> ...",
              file=sys.stderr)
        return 2

    ok, why = rec.mark_validated(v, contract_complete=complete)
    reg.save(rec)
    print(f"{rec.task_id}: {rec.status}  confidence={rec.confidence}")
    print(f"  baseline passed : {v.baseline_passed}")
    print(f"  mutants detected: {v.detected}/{v.total}")
    print(f"  {why}")
    if not ok:
        print("  NOT usable as Ground Truth infrastructure.", file=sys.stderr)
        return 3
    return 0


def cmd_approve(args) -> int:
    reg = _registry(args)
    rec = reg.get(args.task_id)
    if not rec:
        print(f"no verifier record for {args.task_id}", file=sys.stderr)
        return 1
    ok, msg = rec.approve(args.by, args.reason or "")
    reg.save(rec)
    print(f"{rec.task_id}: {msg} (status={rec.status})")
    return 0 if ok else 4


def cmd_activate(args) -> int:
    reg = _registry(args)
    rec = reg.get(args.task_id)
    if not rec:
        return 1
    ok, msg = rec.activate(args.by)
    reg.save(rec)
    print(f"{rec.task_id}: {msg} (status={rec.status})")
    return 0 if ok else 4


def cmd_reject(args) -> int:
    reg = _registry(args)
    rec = reg.get(args.task_id)
    if not rec:
        return 1
    ok, msg = rec.reject(args.by, args.reason or "rejected on review")
    reg.save(rec)
    print(f"{rec.task_id}: {msg} (status={rec.status})")
    return 0 if ok else 4


def cmd_report(args) -> int:
    pool, reg = _pool(args), _registry(args)
    total = len(_eligible(pool))
    s = reg.stats(candidate_total=total)
    print("Verifier authoring")
    print("=" * 54)
    print(f"Ground Truth candidates: {s['candidates']}")
    print(f"\nVerifier status:")
    for st in (vreg.ACTIVE, vreg.APPROVED, vreg.VALIDATED, vreg.PROPOSED, vreg.REJECTED):
        print(f"  {st:10s} {s['by_status'].get(st, 0)}")
    print(f"  {'none':10s} {s['no_verifier_generated']}")
    print("\nConfidence (excluding rejected):")
    for c in (mut.HIGH, mut.MEDIUM, mut.LOW, mut.UNUSABLE):
        print(f"  {c:9s} {s['by_confidence'].get(c, 0)}")
    print(f"\nMechanical verifier coverage: {s['mechanical_coverage']:.0%}")
    if s["by_strategy"]:
        print("\nStrategy:")
        for k, v in sorted(s["by_strategy"].items(), key=lambda kv: -kv[1]):
            print(f"  {k:26s} {v}")
    if s["top_blockers"]:
        print("\nTop blockers:")
        for k, v in s["top_blockers"].items():
            print(f"  {k:46s} {v}")
    active = s["by_status"].get(vreg.ACTIVE, 0)
    print(f"\nACTIVE verifiers usable by run_matrix: {active}")
    if not active:
        print("  Nothing is trusted yet. A proposal becomes trusted only after "
              "validation AND human approval.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", type=Path, default=None)
    ap.add_argument("--registry", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("suggest"); s.set_defaults(fn=cmd_suggest)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--task-type", default=None)
    s.add_argument("--with-tests", action="store_true",
                   help="only candidates whose envelope captured a test command")
    s.add_argument("--force", action="store_true", help="re-propose existing records")

    s = sub.add_parser("show"); s.set_defaults(fn=cmd_show)
    s.add_argument("task_id")

    s = sub.add_parser("validate"); s.set_defaults(fn=cmd_validate)
    s.add_argument("task_id")
    s.add_argument("--target", help="path the generated test imports")
    s.add_argument("--target-source", help="file holding a known-good implementation")
    s.add_argument("--good-answer")
    s.add_argument("--bad-answer", action="append")

    for name, fn in (("approve", cmd_approve), ("activate", cmd_activate),
                     ("reject", cmd_reject)):
        s = sub.add_parser(name); s.set_defaults(fn=fn)
        s.add_argument("task_id")
        s.add_argument("--by", required=True, help="who is doing this (not 'assistant')")
        s.add_argument("--reason", default="")

    s = sub.add_parser("report"); s.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
