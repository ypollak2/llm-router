#!/usr/bin/env python3
"""Does the semantic layer beat the corrected baseline? Run the arms and see.

Everything in `llm_router.semantic` is unmeasured. This is the script that
changes that, and it is deliberately built so that a negative result is easy to
report and hard to hide.

THE ARMS, AND WHAT EACH ONE ISOLATES

    A   no context at all                 the floor
    B   OKF context                       the CORRECTED BASELINE — what ships
                                          today, after the five prerequisite
                                          fixes
    C   semantic pack, no traversal       does a parsed index beat lexical OKF?
    D   semantic pack, 2 hops             does traversal add anything over C?

B is the one that matters. Beating arm A proves only that retrieval beats no
retrieval, which OKF already demonstrated at +66.7%. The question this layer
has to answer is whether it beats the thing already in production, and BC is
the configuration a default-on router would actually send.

WHY THE QUESTIONS COME FROM THE REPOSITORY

Derived, not authored: symbols the repo defines exactly once, so ground truth
comes from grep rather than opinion. Reused wholesale from
`scripts/bench_grounding.py` — including its strict/lenient scorer, so these
numbers are directly comparable with the corrected baseline rather than being a
second measurement with its own private rules.

PAIRED, AND BUDGET-MATCHED

Every arm sees the same questions in the same order against the same model,
and every context arm gets the same token budget. An arm that quietly retrieved
more would win on volume rather than on selection, which is not the question.

WHAT THIS DOES NOT MEASURE

Task completion. This is a retrieval benchmark: it asks where a symbol is
defined, and a system that retrieves better should answer more of them. It says
nothing about whether that improves a patch, and the research document's M0/M1/M2
history track is a different experiment on a different task set entirely.

    python3 scripts/run_semantic_arms.py --n 60 --model qwen3-coder:30b
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Reuse the baseline's question derivation, model call and scorer verbatim.
_SPEC = importlib.util.spec_from_file_location(
    "bench_grounding", REPO / "scripts" / "bench_grounding.py")
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)

ARMS = ("A", "B", "C", "BC")
_ARM_WHAT = {
    "A": "no context",
    "B": "OKF context (the corrected baseline)",
    "C": "semantic pack, no traversal",
    # The configuration that actually SHIPS if source retrieval defaults to on.
    # `context_injection.inject` attaches OKF first and then the semantic pack,
    # so a default-on router sends both — and B and C each measured one of them
    # alone. An arm that nobody ran is an arm nobody can vouch for, and this is
    # the one users would get.
    "BC": "OKF + semantic together (what default-on ships)",
}


def _semantic_context(prompt: str, base: Path,
                      budget_tokens: int) -> tuple[str, dict]:
    """A rendered pack, plus what it cost — or ("", …) when it finds nothing."""
    from llm_router.semantic import pack as spack

    built = spack.build(prompt, root=str(REPO), base=base,
                        budget_tokens=budget_tokens)
    return spack.render(built), {
        "status": built.retrieval_status,
        "tokens": built.retrieved_tokens,
        "evidence": len(built.evidence),
        "omissions": len(built.omissions),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--model", default="qwen3-coder:30b")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--budget-tokens", type=int, default=2000)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    for arm in arms:
        if arm not in ARMS:
            print(f"unknown arm {arm!r}; valid: {list(ARMS)}", file=sys.stderr)
            return 2

    questions = bench.derive_questions(args.n, args.seed)
    if not questions:
        print("no uniquely-defined symbols found — is this a git checkout?")
        return 1

    # Build the index once, and report it, so the run says what it read.
    from llm_router.semantic import indexer as ix
    base = Path.home() / ".llm-router" / "knowledge"
    t0 = time.monotonic()
    idx = ix.index(root=str(REPO))
    print(f"index: {idx.files_parsed} parsed / {idx.files_skipped} unchanged / "
          f"{idx.entities} entities in {time.monotonic() - t0:.1f}s")
    print(f"n = {len(questions)} · model = {args.model} · seed = {args.seed} · "
          f"budget = {args.budget_tokens} tokens")
    print(f"arms: {', '.join(f'{a} ({_ARM_WHAT[a]})' for a in arms)}\n")

    rows = []
    for i, q in enumerate(questions, 1):
        row = {"symbol": q["symbol"], "answer": q["answer"], "arms": {}}
        marks = []
        for arm in arms:
            if arm == "A":
                context, meta = "", {"status": "off", "tokens": 0}
            elif arm == "B":
                context, meta = bench.okf_context(q["prompt"]), {"status": "okf"}
            elif arm == "BC":
                okf_ctx = bench.okf_context(q["prompt"])
                sem_ctx, meta = _semantic_context(
                    q["prompt"], base, args.budget_tokens)
                context = "\n\n".join(x for x in (okf_ctx, sem_ctx) if x)
                meta = {**meta, "status": "okf+semantic"}
            else:
                context, meta = _semantic_context(
                    q["prompt"], base, args.budget_tokens)
            answer, secs = bench.ask(args.model, q["prompt"], context,
                                     args.timeout)
            strict, lenient = bench.score(answer, q)
            row["arms"][arm] = {
                "strict": strict, "lenient": lenient, "secs": round(secs, 1),
                "had_context": bool(context), "meta": meta,
                "answer": answer[:160],
            }
            marks.append(f"{arm}={'Y' if strict else 'n'}")
        rows.append(row)
        print(f"{i:3d}/{len(questions)} {q['symbol'][:30]:32s} "
              f"{' '.join(marks)}", flush=True)

    n = len(rows)
    print(f"\n{'arm':4s}{'what':40s}{'strict':>9s}{'acc':>8s}{'had ctx':>9s}"
          f"{'p50 s':>8s}")
    totals = {}
    for arm in arms:
        hits = sum(r["arms"][arm]["strict"] for r in rows)
        ctx = sum(r["arms"][arm]["had_context"] for r in rows)
        secs = statistics.median(r["arms"][arm]["secs"] for r in rows)
        totals[arm] = hits
        print(f"{arm:4s}{_ARM_WHAT[arm][:38]:40s}{hits:>9d}{hits / n:>8.1%}"
              f"{ctx:>9d}{secs:>8.1f}")

    # Paired differences against the baseline, which is the only comparison
    # that answers the question this layer exists to answer.
    if "B" in totals:
        print(f"\nversus arm B, the corrected baseline (paired, n={n}):")
        for arm in arms:
            if arm == "B":
                continue
            wins = sum(1 for r in rows
                       if r["arms"][arm]["strict"] and not r["arms"]["B"]["strict"])
            losses = sum(1 for r in rows
                         if r["arms"]["B"]["strict"] and not r["arms"][arm]["strict"])
            delta = totals[arm] - totals["B"]
            print(f"  {arm} vs B: {delta:+d} of {n} ({delta / n:+.1%})  "
                  f"[{arm} wins {wins}, B wins {losses}, "
                  f"agree {n - wins - losses}]")
            # McNemar's discordant pairs are the whole signal in a paired
            # binary comparison; b+c under ~10 means the interval is wide
            # enough that a point estimate is close to meaningless.
            if wins + losses < 10:
                print(f"       only {wins + losses} discordant pair(s) — too "
                      f"few to distinguish these arms, whatever the delta says")

    if n < 50:
        print(f"\n** n = {n} is too few to tell. Four days in this repo with "
              f"21-64 prompts produced 2.5%, 1.6%, 0% and 4.3%, all noise. **")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "n": n, "model": args.model, "seed": args.seed,
            "budget_tokens": args.budget_tokens, "arms": arms,
            "totals": totals, "rows": rows,
        }, indent=2), encoding="utf-8")
        print(f"\nper-question detail: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
