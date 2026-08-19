"""Convert the RouterArena parquet dataset to LLM Router's JSONL format.

RouterArena (Lu et al. 2025, arXiv:2510.00202) ships multiple-choice
questions in parquet with columns ``{Category, Domain, Question,
Context, Options, Answer, Difficulty, ...}``. LLM Router's
``llm_router.benchmark.runners.routerarena.RouterArenaRunner`` expects
JSONL at ``~/.llm-router/data/routerarena/<split>.jsonl`` with the shape
``{id, text, reference, subject?, task_type?}``.

This script bridges the two:

* Downloads the requested split via huggingface_hub (no token needed
  for the public RouterArena dataset; throttled requests are fine for
  smoke runs).
* Renders each MCQ as a prompt that instructs the model to reply with
  *only* the option letter — keeps grading simple (exact-match on the
  ``A``/``B``/``C``/``D`` letter).
* Writes one JSON object per line, sorted by ``Global Index`` for
  deterministic re-runs.

Usage::

    .venv/bin/python scripts/routerarena_prep.py --split sub_10
    .venv/bin/python scripts/routerarena_prep.py --split sub_10 --limit 20
    .venv/bin/python scripts/routerarena_prep.py --split full

Designed to be safe to re-run — overwrites the existing JSONL atomically
via a temp file rename. Use ``--dry-run`` to count without writing.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ID = "RouteWorks/RouterArena"
DEFAULT_OUTPUT_ROOT = Path.home() / ".llm-router" / "data" / "routerarena"

# Map RouterArena's "9 Domain" Dewey-style labels into terse subject
# tags LLM Router's classifier already understands. Domains start with a
# digit (e.g. "0 Computer science...") so we key by the leading digit
# for stability if the textual label drifts.
_DOMAIN_TO_SUBJECT = {
    "0": "general",        # Computer science, information, general
    "1": "reasoning",      # Philosophy & psychology
    "2": "general",        # Religion
    "3": "business",       # Social sciences (econ, law, politics)
    "4": "language",       # Language
    "5": "scientific",     # Pure sciences (math/physics/chem/bio)
    "6": "scientific",     # Technology (applied sciences, medicine)
    "7": "creative",       # Arts & recreation
    "8": "creative",       # Literature
    "9": "history",        # History & geography
}


@dataclass(frozen=True)
class Prompt:
    """One JSONL line the llm_router RouterArena runner can consume."""
    id: str
    text: str
    reference: str
    subject: str
    task_type: str = "query"
    difficulty: str = "unknown"
    domain: str = ""
    category: str = ""


def _subject_for_domain(domain: str) -> str:
    """Map a Dewey-prefixed domain string to a LLM Router subject tag.

    Defensive: falls back to ``general`` for any unknown shape.
    """
    head = (domain or "").strip()[:1]
    return _DOMAIN_TO_SUBJECT.get(head, "general")


def _render_mcq(question: str, options: Iterable[str], context: str) -> str:
    """Render an MCQ as a prompt that instructs the model to reply with the
    option letter only.

    Keeping the format consistent across all 809+ prompts is what makes
    exact-match grading work. We deliberately don't number options ourselves
    when the question already references "( )" — RouterArena questions
    sometimes have the blank inline. We always append the lettered options
    after a separator so the model sees a uniform shape.
    """
    parts: list[str] = []
    if context.strip():
        parts.append(context.strip())
    parts.append(question.strip())
    parts.append("")
    for i, opt in enumerate(options):
        # 65 = 'A'
        letter = chr(65 + i)
        parts.append(f"{letter}. {opt}")
    parts.append("")
    parts.append("Reply with only the single letter (A, B, C, or D) of the correct option. No explanation.")
    return "\n".join(parts)


def convert(
    split: str,
    output_root: Path,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> int:
    """Convert ``split`` of the RouterArena dataset to JSONL.

    Returns the number of rows written (or that would be written when
    ``dry_run``). Raises if the dataset can't be downloaded or the split
    is unknown.
    """
    try:
        from huggingface_hub import hf_hub_download
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit(
            f"Missing dependency: {exc}. Run `uv pip install pyarrow huggingface_hub`."
        ) from exc

    filename = f"data/{split}-00000-of-00001.parquet"
    print(f"[routerarena] downloading {filename} from {REPO_ID}", file=sys.stderr)
    path = hf_hub_download(
        repo_id=REPO_ID,
        filename=filename,
        repo_type="dataset",
    )
    tbl = pq.read_table(path)
    rows = tbl.to_pylist()
    # Stable ordering so re-runs produce byte-identical JSONL — important
    # for diffing and for cache-key consistency in the bench harness.
    rows.sort(key=lambda r: str(r.get("Global Index", "")))
    if limit is not None:
        rows = rows[:limit]

    prompts: list[Prompt] = []
    skipped = 0
    for row in rows:
        question = row.get("Question") or ""
        options = row.get("Options") or []
        answer = (row.get("Answer") or "").strip()
        gid = row.get("Global Index") or ""
        if not (question.strip() and options and answer and gid):
            skipped += 1
            continue
        domain = row.get("Domain") or ""
        category = row.get("Category") or ""
        prompts.append(Prompt(
            id=str(gid),
            text=_render_mcq(question, options, row.get("Context") or ""),
            reference=answer.upper(),
            subject=_subject_for_domain(domain),
            task_type="query",
            difficulty=(row.get("Difficulty") or "unknown").lower(),
            domain=domain,
            category=category,
        ))

    print(
        f"[routerarena] {len(prompts)} prompts ready  ({skipped} skipped — "
        f"missing question/options/answer/id)",
        file=sys.stderr,
    )

    if dry_run:
        return len(prompts)

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{split}.jsonl"
    tmp_path = output_root / f".{split}.jsonl.tmp"
    with tmp_path.open("w", encoding="utf-8") as f:
        for p in prompts:
            # Keep extra columns alongside the contract (id/text/reference)
            # so the bench harness can group by difficulty/domain later.
            f.write(json.dumps({
                "id": p.id,
                "text": p.text,
                "reference": p.reference,
                "subject": p.subject,
                "task_type": p.task_type,
                "difficulty": p.difficulty,
                "domain": p.domain,
                "category": p.category,
            }, ensure_ascii=False))
            f.write("\n")
    tmp_path.replace(output_path)
    print(f"[routerarena] wrote {output_path}", file=sys.stderr)
    return len(prompts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--split",
        default="sub_10",
        choices=["sub_10", "full", "robustness"],
        help="Which split to download.",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Where the JSONL lands (default ~/.llm-router/data/routerarena/).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of prompts (smoke runs).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Count prompts; don't write.",
    )
    args = p.parse_args(argv)

    n = convert(
        split=args.split,
        output_root=args.output_root,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {n} prompts for split={args.split}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
