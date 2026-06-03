"""Build the RouterArena robustness predictions file (routing-only).

The README explicitly says::

    predictions/<router_name>-robustness.json — no generated_result fields needed.

So this file just records *which model the router would pick* for each
robustness prompt. No actual inference, no OpenRouter spend, runs in seconds.
The submission evaluator then re-runs inference on its own infrastructure
to measure how routing decisions hold up under perturbed prompts.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("LLM_ROUTER_POLICY", "routerarena_tuned")

# Reuse the dataset→subject mapping from the main submission builder
from build_submission import DATASET_TO_SUBJECT  # noqa: E402


def _dataset_from_global_index(global_index: str) -> str:
    """Robustness rows leave the ``Dataset name`` column empty, but the
    ``Global Index`` carries the source dataset as a prefix (e.g.
    ``"AIME_112"``, ``"MMLUPro_history_4582"``). Split on the LAST underscore
    + digits so the dataset name retains its own underscores."""
    parts = global_index.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return ""


def _subject_for(global_index: str) -> str:
    """Map robustness row to a Subject the routerarena_tuned specialists know."""
    ds = _dataset_from_global_index(global_index)
    return DATASET_TO_SUBJECT.get(ds, "general")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parquet", type=Path,
                        default=Path.home() / ".llm-router" / "data" / "routerarena" / "robustness.parquet")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import pandas as pd
    from llm_router.policy import get_policy_manager
    from llm_router.policy_diff import predict_head_model

    get_policy_manager().set_active_policy("routerarena_tuned")
    policy = get_policy_manager().get_active_policy()
    fallback_workhorse = next(
        (m for m in policy.workhorses if m.startswith("openrouter/")),
        policy.workhorses[1] if len(policy.workhorses) > 1 else policy.workhorses[0],
    )

    df = pd.read_parquet(args.parquet)
    predictions = []
    subjects: list[str] = []
    for _, row in df.iterrows():
        global_index = str(row["Global Index"])
        # Robustness prompts ship pre-perturbed; we must NOT re-template or
        # the perturbation under test gets erased. Use the parquet field as-is.
        prompt_text = (row.get("Question") or "").strip()
        subject = _subject_for(global_index)
        subjects.append(subject)
        model = predict_head_model(policy, subject)
        if not model.startswith("openrouter/"):
            model = fallback_workhorse
        predictions.append({
            "global index": global_index,
            "prompt": prompt_text,
            "prediction": model.removeprefix("openrouter/"),
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        json.dump(predictions, f, indent=2)

    from collections import Counter
    by_subj: Counter[str] = Counter(subjects)
    print(f"Wrote {len(predictions)} robustness predictions → {args.out}")
    print("Subject distribution:")
    for s, n in by_subj.most_common():
        print(f"  {s:<10} {n}")


if __name__ == "__main__":
    main()
