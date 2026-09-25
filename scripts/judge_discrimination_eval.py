#!/usr/bin/env python3
"""OPTIONAL live eval — measures the LLM judge's correct-vs-wrong discrimination.

NOT part of the default test run (`uv run pytest tests/`). This makes real
Ollama calls (one per item, ~50 calls for the full set) and can take a few
minutes; it exists to reproduce the before/after numbers quoted in the
feat/judge-discrimination PR, and to catch a future regression the unit
tests (deterministic, mocked `call_llm`) cannot.

Calls the REAL grading path (`llm_router.judge._evaluate_background` — the
same function `judge._grade_one`/`drain_queue` call) against the hand-labelled
set in `tests/fixtures/judge_eval_set.py`. Always isolated: sets
LLM_ROUTER_HOME / LLM_ROUTER_DB_PATH to a fresh temp directory before
importing anything from llm_router, so this never touches the operator's
real ~/.llm-router.

Usage:
    ollama stop qwen3.8:latest   # free the slot if the drafting chain is using it
    python3 scripts/judge_discrimination_eval.py --judge-model ollama/qwen3.8:latest
    python3 scripts/judge_discrimination_eval.py --judge-model ollama/qwen3.8:latest --split tune
    python3 scripts/judge_discrimination_eval.py --judge-model ollama/qwen3.8:latest --split holdout
    python3 scripts/judge_discrimination_eval.py --judge-model ollama/qwen3.8:latest --raw-out /tmp/out.json

Reports, for the requested split(s):
    - n graded / ungraded (a judge reply that failed to parse must be
      ungraded, never a 0 — see llm_router.judge module docstring)
    - mean score for correct vs wrong vs partial
    - separation: correct>wrong pair rate (= AUC / Mann-Whitney U / n1*n2,
      ties counted as 0.5)
    - accuracy of a 0.5 threshold classifier (correct/wrong only)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# ── isolate BEFORE importing llm_router ─────────────────────────────────────
_ISOLATED_HOME = os.environ.get("LLM_ROUTER_HOME") or tempfile.mkdtemp(prefix="judge-eval-home-")
os.environ["LLM_ROUTER_HOME"] = _ISOLATED_HOME
os.environ.setdefault("LLM_ROUTER_DB_PATH", str(Path(_ISOLATED_HOME) / "usage.db"))
os.environ.setdefault("LLM_ROUTER_ALLOW_STUBS", "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))

from judge_eval_set import ITEMS  # noqa: E402

from llm_router import cost  # noqa: E402
from llm_router import judge as judge_module  # noqa: E402
from llm_router.judge import _evaluate_background  # noqa: E402


# ── "old" variant: the PR#145 judge, verbatim (evenly-averaged 0-1 float
# scale, no verify-first instruction) — for a same-tool, same-set before/after
# comparison. Copied from origin/main's judge.py as of this branch's base
# commit; used only for --variant old, never imported by the production code
# path. This is what lets `--variant old` vs `--variant new` be a true A/B on
# identical items/judge-model/harness, isolating the prompt+parsing change as
# the only variable.
def _build_judge_prompt_old(prompt: str, response: str, task_type: str) -> str:
    return f"""You are an expert quality evaluator. Rate this response on three dimensions:

USER PROMPT:
{prompt}

RESPONSE:
{response}

TASK TYPE: {task_type}

Evaluate on:
1. Relevance (0–1): Does response address the prompt?
2. Completeness (0–1): Is response sufficiently thorough?
3. Correctness (0–1): Is factual content accurate?

Respond ONLY with valid JSON (no markdown, no explanation):
{{"relevance": 0.X, "completeness": 0.X, "correctness": 0.X}}"""


def _parse_judge_score_old(response_text: str) -> float | None:
    import json

    try:
        response_text = response_text.strip()
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        json_str = response_text[start:end]
        data = json.loads(json_str)
        relevance = float(data.get("relevance", 0.5))
        completeness = float(data.get("completeness", 0.5))
        correctness = float(data.get("correctness", 0.5))
        composite = (relevance + completeness + correctness) / 3.0
        return max(0.0, min(1.0, composite))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


async def _insert_decision(task_type: str, answering_model: str) -> int:
    db = await cost._get_db()
    try:
        await db.execute(
            """INSERT INTO routing_decisions
               (timestamp, task_type, profile, complexity, final_model, final_provider,
                success, input_tokens, output_tokens, cost_usd, latency_ms, judge_score)
               VALUES (datetime('now'), ?, 'balanced', 'simple', ?, 'ollama', 1, 10, 10, 0.0, 100.0, NULL)""",
            (task_type, answering_model),
        )
        await db.commit()
        cursor = await db.execute("SELECT id FROM routing_decisions ORDER BY id DESC LIMIT 1")
        row = await cursor.fetchone()
        assert row
        return row[0]
    finally:
        await db.close()


async def _read_score(decision_id: int) -> float | None:
    db = await cost._get_db()
    try:
        cursor = await db.execute(
            "SELECT judge_score FROM routing_decisions WHERE id = ?", (decision_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await db.close()


async def run(judge_model: str, items: list[dict]) -> list[dict]:
    results = []
    for i, item in enumerate(items, 1):
        decision_id = await _insert_decision(item["task_type"], "ollama/answering-placeholder")
        ok = await _evaluate_background(
            item["prompt"],
            item["response"],
            item["task_type"],
            decision_id,
            model=judge_model,
        )
        score = await _read_score(decision_id)
        results.append({**item, "graded": ok, "judge_score": score})
        print(
            f"[{i}/{len(items)}] {item['id']:10s} label={item['label']:7s} "
            f"score={score if score is not None else 'UNGRADED'}",
            file=sys.stderr,
        )
    return results


def _pair_rate(correct_scores: list[float], wrong_scores: list[float]) -> float | None:
    """Fraction of (correct, wrong) score pairs where correct > wrong (ties = 0.5).

    This is exactly AUC / the Mann-Whitney U statistic normalised by n1*n2.
    """
    if not correct_scores or not wrong_scores:
        return None
    total = 0.0
    for c in correct_scores:
        for w in wrong_scores:
            if c > w:
                total += 1.0
            elif c == w:
                total += 0.5
    return total / (len(correct_scores) * len(wrong_scores))


def _threshold_accuracy(
    correct_scores: list[float], wrong_scores: list[float], threshold: float = 0.5
) -> tuple[float, int] | tuple[None, int]:
    labelled = [(s, True) for s in correct_scores] + [(s, False) for s in wrong_scores]
    if not labelled:
        return None, 0
    hits = sum(1 for s, is_correct in labelled if (s >= threshold) == is_correct)
    return hits / len(labelled), len(labelled)


def report(results: list[dict], label: str) -> None:
    def scores_for(lbl: str) -> list[float]:
        return [r["judge_score"] for r in results if r["label"] == lbl and r["judge_score"] is not None]

    correct = scores_for("correct")
    wrong = scores_for("wrong")
    partial = scores_for("partial")
    ungraded = [r for r in results if r["judge_score"] is None]

    def mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    print(f"\n=== {label} (n={len(results)}) ===")
    print(f"graded={len(results) - len(ungraded)} ungraded={len(ungraded)}")
    print(f"mean(correct) n={len(correct)}: {mean(correct)}")
    print(f"mean(wrong)   n={len(wrong)}:   {mean(wrong)}")
    print(f"mean(partial) n={len(partial)}: {mean(partial)}")
    pr = _pair_rate(correct, wrong)
    print(f"correct>wrong pair rate (AUC) over {len(correct)}x{len(wrong)} pairs: {pr}")
    acc, n_acc = _threshold_accuracy(correct, wrong)
    print(f"threshold=0.5 accuracy (correct/wrong only, n={n_acc}): {acc}")
    if ungraded:
        print(f"UNGRADED ids: {[r['id'] for r in ungraded]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judge-model", required=True, help='e.g. "ollama/qwen3.8:latest"')
    parser.add_argument(
        "--split", choices=["all", "tune", "holdout"], default="all",
        help="Which half of the labelled set to grade.",
    )
    parser.add_argument("--raw-out", default=None, help="Optional path to dump raw per-case JSON.")
    parser.add_argument(
        "--variant", choices=["new", "old"], default="new",
        help='"old" swaps in the pre-PR prompt/parsing verbatim (see _build_judge_prompt_old) '
        "for a same-tool before/after A-B; "
        '"new" (default) uses whatever is currently in llm_router.judge.',
    )
    parser.add_argument(
        "--red-check-no-verify-first", action="store_true",
        help="Use the NEW weighted scale/parsing but with the verify-first instruction "
        "stripped from the prompt — isolates that one instruction's contribution to "
        "separation (see PR description's red-check).",
    )
    args = parser.parse_args()

    if args.variant == "old":
        judge_module._build_judge_prompt = _build_judge_prompt_old
        judge_module._parse_judge_score = _parse_judge_score_old
    elif args.red_check_no_verify_first:
        _new_prompt = judge_module._build_judge_prompt

        def _stripped(prompt: str, response: str, task_type: str) -> str:
            text = _new_prompt(prompt, response, task_type)
            # Remove exactly the verify-first paragraph, leaving the coarse
            # scale + explicit correctness criteria + weighting untouched.
            lines = text.splitlines()
            kept = [
                ln for ln in lines
                if not ln.startswith("STEP 1 — VERIFY FIRST")
                and "work out the correct answer or the" not in ln
                and "correct facts yourself" not in ln
                and "Do this internally" not in ln
            ]
            return "\n".join(kept)

        judge_module._build_judge_prompt = _stripped

    if args.split == "all":
        selected = ITEMS
    else:
        selected = [i for i in ITEMS if i["split"] == args.split]

    print(f"LLM_ROUTER_HOME (isolated) = {os.environ['LLM_ROUTER_HOME']}", file=sys.stderr)
    print(f"judge model = {args.judge_model}", file=sys.stderr)
    print(f"variant = {args.variant}  red_check_no_verify_first = {args.red_check_no_verify_first}", file=sys.stderr)
    print(f"n items = {len(selected)} (split={args.split})", file=sys.stderr)

    results = asyncio.run(run(args.judge_model, selected))
    report(results, f"split={args.split} judge={args.judge_model}")

    if args.raw_out:
        import json

        Path(args.raw_out).write_text(json.dumps(results, indent=2))
        print(f"\nraw per-case output written to {args.raw_out}")


if __name__ == "__main__":
    main()
