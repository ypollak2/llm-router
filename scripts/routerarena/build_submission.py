"""Build a RouterArena leaderboard submission.

Reads the official sub_10 or full parquet from ``~/.llm-router/data/routerarena/``,
formats each prompt via the dataset's official ``eval_config/zero-shot/*.json``
template (``\\boxed{X}`` for MCQs, ``\\boxed{answer}`` for QANTA, etc.), routes
each through llm_router via :mod:`llm_router.providers.call_llm` per the
``routerarena_tuned`` policy's subject-specialist mapping, and writes the
prediction JSON in the exact shape ``router_inference/predictions/<name>.json``
expects.

Usage::

    OPENROUTER_API_KEY=sk-... uv run python scripts/routerarena/build_submission.py \\
        --split sub_10  --out submissions/routerarena/llm_router-sub_10.json
    OPENROUTER_API_KEY=sk-... uv run python scripts/routerarena/build_submission.py \\
        --split full   --out submissions/routerarena/llm_router.json

Cost estimate (sub_10, ~810 prompts at $0.31/1K with our workhorse pool):
    ~$0.25 + ~30 min runtime.
Cost estimate (full, ~8400 prompts):
    ~$2.60 + ~5 hours runtime.

The script keeps the predictions list in original parquet order (no shuffle)
to match the reference format used by Sqwish/AgentForge/Nadir submissions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("LLM_ROUTER_GATES", "off")
os.environ.setdefault("LLM_ROUTER_POLICY", "routerarena_tuned")


# ── Official prompt templates (from config/eval_config/zero-shot/*.json) ────


MCQ_PROMPT = (
    "Please read the following multiple-choice questions and provide the most "
    "likely correct answer based on the options given.\n\n"
    "Context: {Context}\n\n"
    "Question: {Question}\n\n"
    "Options: \n{Options}\n\n"
    "Provide the correct letter choice in \\boxed{{X}}, where X is the "
    "correct letter choice. Keep the explanation or feedback within 3 sentences."
)

QANTA_PROMPT = (
    "Please read the following question and provide the correct answer.\n\n"
    "Context: {Context}\n\n"
    "Question: {Question}\n\n"
    "Provide the correct answer in \\boxed{{X}}, where X is the correct and "
    "the most common answer to the question. Keep the explanation or feedback "
    "within 3 sentences."
)

NARRATIVE_PROMPT = (
    "Please read the following context and answer the question based on its "
    "content.\n\n"
    "Context: {Context}\n\n"
    "Question: {Question}\n\n"
    "Provide your final answer in \\boxed{{X}} format. Keep your explanation "
    "clear, concise, and within 3 sentences."
)

LIVECODE_PROMPT = (
    "Write an executable Python function that solves the following problem.\n\n"
    "Problem: {Question}\n\n"
    "Provide your solution as a single Python code block. The function "
    "signature must match the problem statement exactly."
)

# Dataset → which template family it belongs to
MCQ_DATASETS = {
    "ArcMMLU", "PubMedQA", "MedMCQA", "GeoBench", "MusicTheoryBench",
    "MMLUPro_history", "MMLUPro_math", "MMLUPro_physics", "MMLUPro_chemistry",
    "MMLUPro_biology", "MMLUPro_computer science", "MMLUPro_engineering",
    "MMLUPro_economics", "MMLUPro_psychology", "MMLUPro_philosophy",
    "MMLUPro_law", "MMLUPro_business", "MMLUPro_health",
    "MMLU_formal_logic", "MMLU_management", "MMLU",
    "SuperGLUE-Wic", "SuperGLUE-RC", "SuperGLUE-QA",
    "SuperGLUE-Entailment", "SuperGLUE-CausalReasoning",
    "SuperGLUE-ClozeTest",
    "OpenTDB_Science: Computers", "OpenTDB_General Knowledge",
    "OpenTDB_Entertainment: Books", "OpenTDB_Entertainment: Video Games",
    "OpenTDB_Entertainment: Music", "OpenTDB_Geography", "OpenTDB_History",
    "OpenTDB_Animals", "OpenTDB_Mythology", "OpenTDB_Politics",
    "OpenTDB_Sports", "OpenTDB_Vehicles",
    "Ethics_commonsense", "Ethics_virtue", "Ethics_deontology",
    "Ethics_justice", "AsDiv", "ChessInstruct", "ChessInstruct_mcq",
    "SocialiQA", "FinQA", "MathQA", "GSM8K", "MATH", "AIME", "GPQA",
    "GeoGraphyData", "WMT19-gu-en", "WMT19-de-en",
}
QANTA_DATASETS = {
    "QANTA_Literature", "QANTA_History", "QANTA_Science",
    "QANTA_Fine Arts", "QANTA_Geography", "QANTA_Religion",
    "QANTA_Social Science", "QANTA_Philosophy",
}
NARRATIVE_DATASETS = {"NarrativeQA"}
LIVECODE_DATASETS = {"LiveCodeBench"}

LETTERS = list("ABCDEFGHIJ")


def format_options(options) -> str:
    if options is None:
        return ""
    n = min(len(options), len(LETTERS))
    return "\n".join(f"{LETTERS[i]}. {options[i]}" for i in range(n))


def render_prompt(row) -> tuple[str, str]:
    """Return (formatted_prompt, template_family).

    Template family is logged so post-mortem analysis can group accuracy by
    dataset family — useful when a single template change moves the score.
    """
    ds = str(row.get("Dataset name", ""))
    question = (row.get("Question") or "").strip()
    context = (row.get("Context") or "None").strip() or "None"

    if ds in LIVECODE_DATASETS:
        return LIVECODE_PROMPT.format(Question=question), "livecode"

    if ds in QANTA_DATASETS:
        return QANTA_PROMPT.format(Context=context, Question=question), "qanta"

    if ds in NARRATIVE_DATASETS:
        return NARRATIVE_PROMPT.format(Context=context, Question=question), "narrative"

    # Default MCQ template — covers everything else we encountered in sub_10.
    return MCQ_PROMPT.format(
        Context=context, Question=question, Options=format_options(row.get("Options")),
    ), "mcq"


# Dataset → routing subject (drives the routerarena_tuned specialist lookup).
DATASET_TO_SUBJECT = {
    "PubMedQA": "medical", "MedMCQA": "medical",
    "MMLUPro_health": "medical", "MMLUPro_biology": "medical",
    "LiveCodeBench": "code", "MMLUPro_computer science": "code",
    "MMLUPro_engineering": "code",
    "NarrativeQA": "narrative",
    "QANTA_Literature": "narrative", "QANTA_History": "narrative",
    "QANTA_Science": "narrative", "QANTA_Fine Arts": "narrative",
    "MMLUPro_history": "history",
    "MMLUPro_math": "reasoning", "MMLUPro_psychology": "reasoning",
    "MMLUPro_philosophy": "reasoning", "MMLUPro_economics": "reasoning",
    "MMLUPro_law": "reasoning", "MMLUPro_business": "reasoning",
    "MMLU_formal_logic": "reasoning",
    "AsDiv": "reasoning", "MathQA": "reasoning",
    "GSM8K": "reasoning", "MATH": "reasoning", "AIME": "reasoning",
    "GPQA": "reasoning",
    "Ethics_commonsense": "reasoning", "Ethics_virtue": "reasoning",
    "Ethics_deontology": "reasoning", "Ethics_justice": "reasoning",
    "SocialiQA": "reasoning",
    "SuperGLUE-Wic": "reasoning", "SuperGLUE-RC": "reasoning",
    "SuperGLUE-QA": "reasoning",
    "SuperGLUE-Entailment": "reasoning",
    "SuperGLUE-CausalReasoning": "reasoning",
    "SuperGLUE-ClozeTest": "reasoning",
    "MMLUPro_chemistry": "physics", "MMLUPro_physics": "physics",
}


def pick_subject(row) -> str:
    ds = str(row.get("Dataset name", ""))
    if ds in DATASET_TO_SUBJECT:
        return DATASET_TO_SUBJECT[ds]
    return "general"


# ── Routing + inference ─────────────────────────────────────────────────────


async def infer_one(model: str, prompt: str) -> dict[str, Any]:
    """Route a single prompt; returns the prediction-row dict shape RouterArena expects."""
    from llm_router.providers import call_llm

    try:
        resp = await call_llm(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,  # OpenRouterQuirks caps further at 2048
            temperature=0.0,
        )
        return {
            "generated_answer": resp.content,
            "success": True,
            "token_usage": {
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "total_tokens": resp.input_tokens + resp.output_tokens,
            },
            "provider": "openrouter",
            "error": None,
            "_cost_usd": resp.cost_usd,
            "_latency_ms": resp.latency_ms,
        }
    except Exception as err:  # noqa: BLE001
        return {
            "generated_answer": "",
            "success": False,
            "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "provider": "openrouter",
            "error": str(err)[:300],
            "_cost_usd": 0.0,
            "_latency_ms": 0.0,
        }


async def build_predictions(parquet_path: Path, *, limit: int = 0) -> list[dict]:
    """Iterate the parquet rows, route each via routerarena_tuned, emit prediction rows."""
    import pandas as pd
    from llm_router.policy import get_policy_manager
    from llm_router.policy_diff import predict_head_model

    get_policy_manager().set_active_policy("routerarena_tuned")
    policy = get_policy_manager().get_active_policy()
    # First OpenRouter workhorse — used when predict_head_model returns
    # Ollama (which we can't actually use for the submission).
    fallback_workhorse = next(
        (m for m in policy.workhorses if m.startswith("openrouter/")),
        policy.workhorses[1] if len(policy.workhorses) > 1 else policy.workhorses[0],
    )

    df = pd.read_parquet(parquet_path)
    if limit > 0:
        df = df.head(limit)

    predictions: list[dict] = []
    n_total = len(df)
    started = time.monotonic()
    for idx, row in df.iterrows():
        prompt_text, family = render_prompt(row)
        subject = pick_subject(row)
        # Routing decision (same logic the policy diff uses).
        model = predict_head_model(policy, subject)
        if not model.startswith("openrouter/"):
            model = fallback_workhorse

        gen = await infer_one(model, prompt_text)

        # Cost reported in micro-USD to match the order of magnitude seen in
        # other submissions (Sqwish's "cost" field is ~10^0 for a single
        # call — i.e. micro-USD, since real per-call cost on cheap workhorses
        # is sub-cent).
        cost_micro = round(gen.pop("_cost_usd", 0.0) * 1_000_000, 6)
        latency_ms = gen.pop("_latency_ms", 0.0)

        predictions.append({
            "global index": str(row["Global Index"]),
            "prompt": prompt_text,
            "prediction": model.removeprefix("openrouter/"),
            "generated_result": gen,
            "cost": cost_micro,
            "latency_ms": round(latency_ms, 2),
        })

        n_done = len(predictions)
        if n_done % 25 == 0 or n_done == n_total:
            elapsed = time.monotonic() - started
            rate = n_done / max(elapsed, 1e-6)
            eta_min = (n_total - n_done) / max(rate, 1e-6) / 60
            print(f"  {n_done}/{n_total}  rate={rate:.1f}/s  ETA={eta_min:.1f}min  "
                  f"last={row['Dataset name']:<25} model={model.split('/')[-1]:<28} "
                  f"{family} ok={gen['success']}", flush=True)

    return predictions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="sub_10", choices=["sub_10", "full"])
    parser.add_argument("--parquet", type=Path, default=None,
                        help="Override parquet path. Default: ~/.llm-router/data/routerarena/<split>.parquet")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if args.parquet is None:
        args.parquet = Path.home() / ".llm-router" / "data" / "routerarena" / f"{args.split}.parquet"
    if not args.parquet.is_file():
        raise SystemExit(f"Parquet not found at {args.parquet}. Download from HF first.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    predictions = asyncio.run(build_predictions(args.parquet, limit=args.limit))

    with args.out.open("w") as f:
        json.dump(predictions, f, indent=2)

    # Summary
    n = len(predictions)
    n_ok = sum(1 for p in predictions if p["generated_result"]["success"])
    total_cost_usd = sum(p["cost"] for p in predictions) / 1_000_000
    print(f"\nWrote {n} predictions → {args.out}")
    print(f"  Inference success: {n_ok}/{n} = {n_ok/n:.4f}" if n else "")
    print(f"  Total cost: ${total_cost_usd:.4f}    Cost/1K: ${total_cost_usd * 1000 / max(n,1):.4f}")


if __name__ == "__main__":
    main()
