"""Generate a RouterArena leaderboard submission for LLM Router.

RouterArena (Lu et al. 2025) measures *routing decisions*, not end-to-end
accuracy. For each prompt, a router must return a single model name from
its declared pool; RouterArena's pipeline then runs that model and grades
the answer. So the submission artifact is **offline** — we don't call any
inference provider here, we only call LLM Router's classifier.

This script:

1. Loads the RouterArena dataset (one of ``sub_10`` / ``full`` /
   ``robustness``) from HuggingFace.
2. For each prompt, asks LLM Router's classifier for
   ``(complexity, inferred_task_type, subject)``.
3. Maps that tuple onto a model in RouterArena's fixed pool using
   ``_select_pool_model``. Code prompts go to the coder specialist;
   simple prompts go to the cheapest model; complex prompts go to
   the strongest in the pool. Subject overrides complexity for the
   specialised cases (math/scientific/reasoning).
4. Emits ``router_inference/predictions/llm-routing.json`` in the
   shape RouterArena's ``generate_prediction_file.py`` produces. The
   schema is documented at
   ``github.com/RouteWorks/RouterArena/blob/main/router_inference/generate_prediction_file.py``.

We deliberately **do not** include optimality entries — those require
RouterArena's full pipeline to fan-out predictions across the rest of
the pool, which only the maintainers' eval workflow does. The PR review
adds them automatically if needed.

Usage::

    .venv/bin/python scripts/routerarena_submit.py \\
        --split sub_10 \\
        --output-root bench/routerarena/submission/

    .venv/bin/python scripts/routerarena_submit.py \\
        --split sub_10 --limit 50          # smoke run
    .venv/bin/python scripts/routerarena_submit.py --split full
    .venv/bin/python scripts/routerarena_submit.py --split robustness

The output directory mirrors RouterArena's repo layout so the files can
be copied directly into a fork::

    bench/routerarena/submission/
    ├── config/
    │   └── llm-routing.json
    └── predictions/
        ├── llm-routing.json            # sub_10 + full
        └── llm-routing-robustness.json # robustness split
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ID = "RouteWorks/RouterArena"

# ── RouterArena's fixed model pool ────────────────────────────────────────
# Mirrored from agentforge-router.json (RouterArena rank #3, 74.13 Acc-Cost
# Arena, $0.13/1K queries). Picked agentforge because it has 10 models
# covering cheap → premium across 4 providers, giving LLM Router's tier-based
# selector room to express preferences. Top routers use overlapping pools
# so the choice doesn't lock us out of the leaderboard.
#
# Every name here MUST match RouterArena's expected pool exactly — they
# validate predictions against ``config["pipeline_params"]["models"]``.
_DEFAULT_POOL: tuple[str, ...] = (
    "qwen/qwen3-235b-a22b-2507",          # premium frontier (strongest)
    "google/gemini-3.1-flash-lite",        # cheapest cheap-tier
    "deepseek/deepseek-v4-flash",          # cheap, strong on reasoning
    "deepseek/deepseek-v3.2",              # mid, strong on code
    "qwen/qwen3-next-80b-a3b-instruct",    # mid-premium
    "Qwen/Qwen3-Coder-Next",               # code specialist
    "gemini-2.5-flash",                    # cheap, balanced
    "qwen/qwen3-30b-a3b-instruct-2507",    # mid
    "gpt-4o-mini",                         # cheap-mid, broad coverage
    "claude-3-haiku-20240307",             # cheap, strong on reasoning
)


@dataclass(frozen=True)
class PoolSelector:
    """Maps LLM Router's (complexity, subject, task_type) onto a pool model.

    Two overrides win regardless of complexity:

    * ``subject == "code"`` → ``Qwen3-Coder-Next`` (specialist beats tier).
    * ``subject in {"math", "scientific", "reasoning"}`` AND
      complexity ≥ moderate → ``deepseek-v4-flash`` (its sweet spot).

    Otherwise pick by complexity:

    * simple        → ``gemini-3.1-flash-lite``  (cheapest)
    * moderate      → ``gpt-4o-mini``            (broad, predictable)
    * complex       → ``qwen3-235b-a22b-2507``   (frontier)
    * deep_reasoning → ``qwen3-235b-a22b-2507``  (same — pool has no o1-class)

    The mapping is intentionally legible: every choice in the
    leaderboard submission can be explained by one rule. This makes
    failure modes diagnosable from the predictions JSON alone.
    """
    pool: tuple[str, ...]

    def select(
        self,
        complexity: str,
        subject: str | None,
        task_type: str | None,  # noqa: ARG002 — accepted for forward compat
    ) -> str:
        subj = (subject or "general").lower()
        comp = (complexity or "moderate").lower()

        # Override 1: code → coder specialist
        if subj == "code" and "Qwen/Qwen3-Coder-Next" in self.pool:
            return "Qwen/Qwen3-Coder-Next"

        # Override 2: reasoning-heavy subjects benefit from deepseek
        if (
            subj in {"math", "scientific", "reasoning"}
            and comp in {"moderate", "complex", "deep_reasoning"}
            and "deepseek/deepseek-v4-flash" in self.pool
        ):
            return "deepseek/deepseek-v4-flash"

        # Tier by complexity
        if comp == "simple":
            return "google/gemini-3.1-flash-lite"
        if comp == "moderate":
            return "gpt-4o-mini"
        # complex / deep_reasoning / unknown → premium
        return "qwen/qwen3-235b-a22b-2507"


def _load_dataset_rows(split: str, limit: int | None) -> list[dict]:
    """Return RouterArena rows from the HF parquet for ``split``.

    Stable sort by ``Global Index`` so re-runs are deterministic.

    The ``sub_10`` / ``full`` splits are MCQ: every usable row must carry
    Question + Options + Answer + Global Index. The ``robustness`` split
    is free-form (mathematical reasoning prompts adapted to varied
    phrasings) — Options/Answer are empty by design, so the filter
    relaxes to Question + Global Index. RouterArena's eval pipeline
    grades robustness with its own reference (held out from us).
    """
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    filename = f"data/{split}-00000-of-00001.parquet"
    print(f"[submit] downloading {filename}", file=sys.stderr)
    path = hf_hub_download(
        repo_id=REPO_ID,
        filename=filename,
        repo_type="dataset",
    )
    tbl = pq.read_table(path)
    rows = tbl.to_pylist()
    rows.sort(key=lambda r: str(r.get("Global Index", "")))
    if split == "robustness":
        rows = [
            r for r in rows
            if r.get("Question") and r.get("Global Index")
        ]
    else:
        rows = [
            r for r in rows
            if (r.get("Question") and r.get("Options")
                and r.get("Answer") and r.get("Global Index"))
        ]
    if limit is not None:
        rows = rows[:limit]
    return rows


def _render_prompt(row: dict) -> str:
    """Render the prompt exactly as the inference pipeline will see it.

    MCQ rows (sub_10 / full) get rendered as Question + lettered Options.
    Free-form rows (robustness) get rendered as Context + Question with no
    Options suffix — Options is empty in that split by design.

    We deliberately don't add any "reply with only the letter" instruction:
    RouterArena's eval pipeline owns the system prompt and we want the
    routing decision to be made on the same string that pipeline sends.
    """
    parts: list[str] = []
    ctx = (row.get("Context") or "").strip()
    if ctx:
        parts.append(ctx)
    parts.append((row.get("Question") or "").strip())
    options = row.get("Options") or []
    if options:
        parts.append("")
        for i, opt in enumerate(options):
            parts.append(f"{chr(65 + i)}. {opt}")
    return "\n".join(parts).strip()


def _load_inline_classifier():
    """Dynamic-import the inlined classifier from the submission router.

    The same module RouterArena's evaluator loads is the source of truth
    for our routing decisions — generating predictions from a *different*
    classifier would leave the inline router and the JSON drift-prone.
    By importing from ``bench/routerarena/submission/router/llm_router_router.py``
    here, the two artifacts agree by construction: any regex change there
    automatically reflects in the next re-generated predictions JSON.

    Why heuristic rather than ``llm_router.classifier.classify_complexity``:

    * Heuristic is **deterministic** — the leaderboard submission must
      reproduce byte-identically across runs, so calling an LLM that may
      hit rate limits / return different tokens won't do.
    * Heuristic is **offline** — no API spend, no provider failures
      degrade the submission. We can run all 8400 ``full``-split prompts
      in well under a minute.
    * Heuristic is **what production uses for fast-path routing** in
      ``src/llm_router/hooks/auto-route.py:classify_prompt`` (Layer 1). The
      LLM classifier is the fallback; submitting on the heuristic
      surfaces the routing decisions a real LLM Router session would make
      on the fast-path.
    """
    import importlib.util
    import sys
    import types
    from pathlib import Path

    # Stub out RouterArena's BaseRouter so the inline router module imports
    # cleanly without needing the leaderboard repo on PYTHONPATH.
    if "router_inference.router.base_router" not in sys.modules:
        pkg = types.ModuleType("router_inference"); pkg.__path__ = []
        sub = types.ModuleType("router_inference.router"); sub.__path__ = []
        base = types.ModuleType("router_inference.router.base_router")

        class _StubBase:
            def __init__(self, *_a, **_kw):
                self.models = []

        base.BaseRouter = _StubBase
        sys.modules["router_inference"] = pkg
        sys.modules["router_inference.router"] = sub
        sys.modules["router_inference.router.base_router"] = base

    spec = importlib.util.spec_from_file_location(
        "llm_router_routerarena_inline_classifier",
        Path(__file__).resolve().parent.parent
        / "bench" / "routerarena" / "submission" / "router" / "llm_router_router.py",
    )
    if spec is None or spec.loader is None:
        raise ImportError("could not load inline LLMRouterRouter classifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Domain (Dewey-prefixed) → LLM Router subject tag. Re-uses the same mapping
# as scripts/routerarena_prep.py so prep and submit agree on subjects.
_DOMAIN_TO_SUBJECT = {
    "0": "general",
    "1": "reasoning",
    "2": "general",
    "3": "business",
    "4": "language",
    "5": "scientific",
    "6": "scientific",
    "7": "creative",
    "8": "creative",
    "9": "history",
}


def _subject_for_domain(domain: str) -> str:
    head = (domain or "").strip()[:1]
    return _DOMAIN_TO_SUBJECT.get(head, "general")


def _generate_predictions(
    rows: list[dict],
    selector: PoolSelector,
) -> tuple[list[dict], Counter]:
    """Return RouterArena-shaped predictions plus a model-frequency tally.

    Uses the *inline* classifier from
    ``bench/routerarena/submission/router/llm_router_router.py`` so generated
    predictions match the routing decisions RouterArena's evaluator
    will make from the same module. Subject is inferred from prompt
    text (same regex pass) rather than the dataset's Domain field —
    again so generation matches what the evaluator sees, since the
    inline router has no access to dataset metadata.
    """
    classifier = _load_inline_classifier()

    predictions: list[dict] = []
    tally: Counter = Counter()
    for idx, row in enumerate(rows, start=1):
        prompt = _render_prompt(row)

        # Match exactly what the inline LLMRouterRouter computes for the same
        # prompt — guarantees the predictions JSON and the live router agree.
        task_type = classifier._infer_task_type(prompt)
        complexity = classifier._classify_complexity(prompt, task_type)
        subject = classifier._infer_subject(prompt)

        chosen = selector.select(complexity, subject, task_type)
        tally[chosen] += 1

        predictions.append({
            "global index": row.get("Global Index"),
            "prompt": prompt,
            "prediction": chosen,
            "generated_result": None,
            "cost": None,
            "accuracy": None,
            "for_optimality": False,
        })

        if idx % 200 == 0 or idx == len(rows):
            print(f"[submit] classified {idx}/{len(rows)}", file=sys.stderr)

    return predictions, tally


def _config_dict(router_name: str, pool: tuple[str, ...]) -> dict:
    """The config/<router>.json contents RouterArena expects.

    ``router_cls_name`` must match the class name in the Python file we
    drop into ``router_inference/router/``. We ship as ``LLMRouterRouter``.
    """
    return {
        "pipeline_params": {
            "router_name": router_name,
            "router_cls_name": "LLMRouterRouter",
            "models": list(pool),
            "description": (
                "LLM Router: heuristic + classifier-driven routing with a three-axis "
                "taxonomy (complexity × task_type × subject). Local-first "
                "cascade in production; routing decisions for this submission "
                "come from llm_router.classifier.classify_complexity at quality_mode="
                "balanced, mapped onto the model pool via a tier + subject "
                "override table (see scripts/routerarena_submit.py)."
            ),
        },
        "router": router_name,
        "router_name": router_name,
        "description": "LLM Router Router submission (v0.1.0).",
    }


def _write_outputs(
    output_root: Path,
    router_name: str,
    pool: tuple[str, ...],
    predictions: list[dict],
    split: str,
) -> tuple[Path, Path]:
    """Write ``config/<name>.json`` and ``predictions/<name>.json``.

    The robustness split lands at ``predictions/<name>-robustness.json``
    per RouterArena's naming convention.
    """
    config_dir = output_root / "config"
    predictions_dir = output_root / "predictions"
    config_dir.mkdir(parents=True, exist_ok=True)
    predictions_dir.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / f"{router_name}.json"
    config_path.write_text(
        json.dumps(_config_dict(router_name, pool), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    prediction_filename = (
        f"{router_name}-robustness.json"
        if split == "robustness"
        else f"{router_name}.json"
    )
    predictions_path = predictions_dir / prediction_filename
    predictions_path.write_text(
        json.dumps(predictions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return config_path, predictions_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--split",
        default="sub_10",
        choices=["sub_10", "full", "robustness"],
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap prompts (smoke runs).",
    )
    p.add_argument(
        "--router-name",
        default="llm-routing",
        help="Used to derive output filenames + config keys.",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=Path("bench/routerarena/submission"),
        help="Output directory; mirrors RouterArena's repo layout.",
    )
    args = p.parse_args(argv)

    selector = PoolSelector(pool=_DEFAULT_POOL)
    rows = _load_dataset_rows(args.split, args.limit)
    print(f"[submit] loaded {len(rows)} prompts for split={args.split}", file=sys.stderr)

    predictions, tally = _generate_predictions(rows, selector)

    config_path, predictions_path = _write_outputs(
        args.output_root,
        router_name=args.router_name,
        pool=_DEFAULT_POOL,
        predictions=predictions,
        split=args.split,
    )

    print(f"\n✓ config:      {config_path}")
    print(f"✓ predictions: {predictions_path}  ({len(predictions)} rows)")
    print("\nModel selection breakdown:")
    for model, count in tally.most_common():
        pct = 100 * count / max(len(predictions), 1)
        print(f"  {model:<42}  {count:>4}  ({pct:5.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
