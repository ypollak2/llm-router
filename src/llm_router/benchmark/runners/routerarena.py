"""RouterArena adapter — minimal in-package runner.

Plan 07 Cat G.1 ships the *plug-in scaffolding*. RouterArena is the first
concrete benchmark to land because (a) we already run it manually for
sub_10 evaluation, and (b) it exercises the contract end-to-end (load,
format, evaluate, submit) so the Protocol gets tested in anger.

This adapter is intentionally minimal:

* ``load_dataset`` reads JSONL from ``~/.llm-router/data/routerarena/<split>.jsonl``
  if present. Each line is ``{"id", "text", "reference", "subject"?, "task_type"?}``.
  Returns ``[]`` when no dataset is available so the CLI degrades to a
  helpful "no dataset found" message rather than crashing.
* ``evaluate`` does normalized exact-match (case/whitespace-insensitive)
  against the ``reference`` field, with optional ``per_subject`` breakdowns.
  That's the minimum useful score; richer metrics (BLEU, ROUGE, judge LLM)
  can layer on by sub-classing or shipping a sibling runner.
* ``submit`` is a stub — the actual RouterArena submission workflow lives
  in ``scripts/`` and uses the official RouterArena CLI; running it from
  here requires credentials we don't want to ship by default.

The point is to prove the contract works on a real benchmark. A "no
dataset, no submit" runner is still useful: the regression detector can
re-run a cached dataset on every release.
"""

from __future__ import annotations

import json
from pathlib import Path

from llm_router.benchmark import (
    BenchmarkResult,
    Prediction,
    Prompt,
    SubmissionResult,
    register_runner,
)

__all__ = ["RouterArenaRunner"]


def _default_dataset_root() -> Path:
    """Where the runner looks for cached RouterArena prompts.

    Per-user (not per-project) because the same dataset serves every llm_router
    workspace on this machine — no need to re-download or duplicate.
    """
    return Path.home() / ".llm-router" / "data" / "routerarena"


def _normalize(text: str) -> str:
    """Casefold + collapse whitespace for forgiving exact-match scoring."""
    return " ".join(text.casefold().split())


class RouterArenaRunner:
    """Plug-in implementation for the RouterArena leaderboard."""

    name = "routerarena"

    def __init__(self, dataset_root: Path | None = None) -> None:
        self._dataset_root = dataset_root or _default_dataset_root()

    def load_dataset(self, split: str) -> list[Prompt]:
        """Load prompts from ``<dataset_root>/<split>.jsonl``.

        Returns ``[]`` when the file is missing — callers translate that
        into a "fetch the dataset first" hint rather than a stack trace.
        """
        path = self._dataset_root / f"{split}.jsonl"
        if not path.is_file():
            return []

        prompts: list[Prompt] = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    # Skip corrupt lines; surfacing them would block evaluation.
                    # A regression detector run is more useful with N-1 prompts
                    # than zero, and the CLI logs a count for visibility.
                    continue
                metadata = {
                    k: v
                    for k, v in row.items()
                    if k not in {"id", "text", "prompt", "reference", "answer", "subject", "task_type"}
                }
                # Stamp the split onto each prompt's metadata so ``evaluate``
                # can produce a fully-populated ``BenchmarkResult`` without an
                # extra parameter. The regression detector keys on ``split``,
                # so omitting this caused ``store_result`` + ``load_history``
                # to disagree on the bucket and lose persisted rows.
                metadata.setdefault("split", split)
                prompts.append(
                    Prompt(
                        id=str(row.get("id", f"{split}:{line_no}")),
                        text=str(row.get("text") or row.get("prompt") or ""),
                        reference=row.get("reference") or row.get("answer"),
                        subject=row.get("subject"),
                        task_type=row.get("task_type"),
                        metadata=metadata,
                    )
                )
        return prompts

    def format_prediction(self, prompt: Prompt, prediction: Prediction) -> dict:
        """Serialise one prediction to the RouterArena submission row shape."""
        return {
            "id": prompt.id,
            "model": prediction.model,
            "response": prediction.response,
            "cost_usd": prediction.cost_usd,
            "latency_ms": prediction.latency_ms,
            "subject": prompt.subject,
        }

    def evaluate(self, predictions: list[Prediction], dataset: list[Prompt]) -> BenchmarkResult:
        """Normalized exact-match accuracy with per-subject breakdown."""
        by_id: dict[str, Prompt] = {p.id: p for p in dataset}
        n_total = 0
        n_correct = 0
        subject_totals: dict[str, list[int]] = {}  # subject → [n_correct, n_total]

        for pred in predictions:
            prompt = by_id.get(pred.prompt_id)
            if prompt is None or prompt.reference is None:
                continue
            n_total += 1
            ok = 1 if _normalize(pred.response) == _normalize(prompt.reference) else 0
            n_correct += ok
            subj = prompt.subject or "general"
            slot = subject_totals.setdefault(subj, [0, 0])
            slot[0] += ok
            slot[1] += 1

        per_subject = {
            subj: round(c / max(t, 1), 4)
            for subj, (c, t) in subject_totals.items()
        }
        score = round(n_correct / max(n_total, 1), 4)
        return BenchmarkResult(
            benchmark=self.name,
            split=dataset[0].metadata.get("split", "unknown") if dataset else "unknown",
            score=score,
            n_samples=n_total,
            per_subject=per_subject,
            metadata={"n_correct": n_correct},
        )

    def submit(self, predictions: list[Prediction]) -> SubmissionResult | None:
        """Submission stub — the live workflow lives in scripts/.

        Returning a ``SubmissionResult(submitted=False, ...)`` instead of
        ``None`` so CLI output stays informative; ``None`` is reserved for
        benchmarks that genuinely don't have a leaderboard.
        """
        return SubmissionResult(
            submitted=False,
            message=(
                "RouterArena submission is not automated yet. Save predictions and "
                "use the official RouterArena CLI; see scripts/ for the manual flow."
            ),
        )


# Side-effect registration: importing this module makes the runner available
# via ``llm_router.benchmark.get_runner("routerarena")``.
register_runner(RouterArenaRunner())
