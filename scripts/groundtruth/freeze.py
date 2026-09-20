#!/usr/bin/env python3
"""Freeze a versioned ground-truth dataset with a manifest and a split.

    python3 scripts/groundtruth/freeze.py --version v1 \
        --corpus data/groundtruth/corpus.jsonl \
        --tasks  data/groundtruth/tasks/draft.jsonl

Produces data/groundtruth/<version>/ containing

    corpus.jsonl      the scrubbed prompts, as extracted
    tune.jsonl        tasks you may iterate against
    test.jsonl        held out; reading it is logged
    ambiguous.jsonl   tasks with no mechanical verifier, kept separate
    manifest.json     everything needed to reproduce or distrust this dataset

A frozen version is immutable. Re-running against an existing version fails
unless --force is given, because a mutable eval set makes every historical
comparison meaningless — the number you published last month would no longer
refer to anything.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import dataset as ds  # noqa: E402
from groundtruth.scrub import salt_fingerprint  # noqa: E402

SCHEMA_VERSION = 1


def _tier_models() -> dict:
    """The model behind each tier, as run_matrix would resolve it today."""
    try:
        from groundtruth.run_matrix import TIERS
        return {name: cfg["model"] for name, cfg in
                sorted(TIERS.items(), key=lambda kv: kv[1]["order"])}
    except Exception:  # noqa: BLE001
        return {}


def _verifier_types(tasks: list) -> dict:
    out: dict[str, int] = {}
    for t in tasks:
        key = getattr(t, "verification_type", None) or ds.V_MECHANICAL
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _ambiguity_reasons(tasks: list) -> dict:
    out: dict[str, int] = {}
    for t in tasks:
        out[t.ambiguity_reason or "unstated"] = out.get(t.ambiguity_reason or "unstated", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="e.g. v1")
    ap.add_argument("--corpus", type=Path, default=Path("data/groundtruth/corpus.jsonl"))
    ap.add_argument("--tasks", type=Path, default=Path("data/groundtruth/tasks/draft.jsonl"))
    ap.add_argument("--root", type=Path, default=Path("data/groundtruth"))
    ap.add_argument("--test-fraction", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=20260920)
    ap.add_argument("--note", default="", help="one line on why this version exists")
    ap.add_argument("--force", action="store_true", help="overwrite an existing version")
    args = ap.parse_args()

    out = args.root / args.version
    if out.exists() and not args.force:
        raise SystemExit(
            f"{out} already exists. A frozen dataset is immutable — cut a new "
            f"version instead, or pass --force if you are certain nothing has "
            f"cited this one.")

    if not args.corpus.exists():
        raise SystemExit(f"corpus not found: {args.corpus}")
    corpus_rows = [json.loads(ln) for ln in
                   args.corpus.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not corpus_rows:
        raise SystemExit("corpus is empty — refusing to freeze nothing")

    tasks = ds.load_tasks(args.tasks)
    errs = ds.validate_all(tasks)
    if errs:
        print("TASK VALIDATION FAILED:", file=sys.stderr)
        for e in errs[:40]:
            print(f"  {e}", file=sys.stderr)
        return 2

    mechanical = [t for t in tasks if t.verifier_kind == ds.MECHANICAL]
    subjective = [t for t in tasks if t.verifier_kind == ds.SUBJECTIVE]

    # Only mechanically-verifiable tasks are split; a subjective task cannot
    # contribute to a measured number, so holding half of them out buys
    # nothing and would only make the mechanical split smaller.
    tune, test = ds.split_tasks(mechanical, test_fraction=args.test_fraction,
                                seed=args.seed)

    out.mkdir(parents=True, exist_ok=True)
    (out / "corpus.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                for r in corpus_rows), encoding="utf-8")

    def dump(path: Path, rows: list[ds.Task], sentinel: dict | None = None) -> None:
        with path.open("w", encoding="utf-8") as fh:
            if sentinel:
                fh.write(json.dumps(sentinel) + "\n")
            for t in rows:
                fh.write(json.dumps(t.to_json(), ensure_ascii=False, sort_keys=True) + "\n")

    dump(out / "tune.jsonl", tune)
    dump(out / "test.jsonl", test, sentinel=ds.TEST_SENTINEL)
    dump(out / "ambiguous.jsonl", subjective)

    ts = [r.get("ts") for r in corpus_rows if isinstance(r.get("ts"), (int, float))]
    window = {
        "earliest": time.strftime("%Y-%m-%d", time.localtime(min(ts))) if ts else None,
        "latest": time.strftime("%Y-%m-%d", time.localtime(max(ts))) if ts else None,
        "rows_with_timestamp": len(ts),
        "rows_without_timestamp": len(corpus_rows) - len(ts),
        "note": ("llm-router transcripts carry no timestamp, so the window "
                 "covers only the Claude Code portion of the corpus."),
    }

    funnel_path = args.corpus.with_suffix(".funnel.json")
    funnel = json.loads(funnel_path.read_text()) if funnel_path.exists() else {}

    scrub_counts: Counter[str] = Counter()
    for r in corpus_rows:
        for k, v in (r.get("scrub_counts") or {}).items():
            scrub_counts[k] += v

    frozen = [out / "corpus.jsonl", out / "tune.jsonl",
              out / "test.jsonl", out / "ambiguous.jsonl"]

    manifest = {
        "dataset_version": args.version,
        "schema_version": SCHEMA_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": args.note,
        "content_hash": ds.content_hash(frozen),
        "file_hashes": {p.name: ds.sha256_file(p) for p in frozen},

        "source_time_window": window,
        "sample_counts": {
            "corpus_rows": len(corpus_rows),
            "tasks_total": len(tasks),
            "tasks_mechanical": len(mechanical),
            "tasks_subjective": len(subjective),
            "tune": len(tune),
            "test": len(test),
        },

        "sampling_methodology": {
            "description": (
                "Census, not a sample: every prompt surviving the exclusion "
                "filters is included. The surviving population is far smaller "
                "than any target sample size, so there is nothing to sample "
                "down to and stratified sampling would only discard data."),
            "stratification": (
                "Not applied at extraction. The split is stratified by "
                "(triage, kind, verifier_kind)."),
            "split_seed": args.seed,
            "test_fraction": args.test_fraction,
        },

        "exclusion_filters": funnel.get("funnel", {}),
        "source_provenance": funnel.get("by_source", {}),
        "task_distribution": {
            "by_triage": dict(Counter(r.get("triage") for r in corpus_rows)),
            "by_source_kind": dict(Counter(r.get("source_kind") for r in corpus_rows)),
            "by_task_kind": dict(Counter(t.kind for t in tasks)),
            "by_verifier_kind": dict(Counter(t.verifier_kind for t in tasks)),
        },

        "deduplication": {
            "exact": "sha1 over punctuation-stripped, casefolded, lightly stemmed tokens",
            "near": "token-set Jaccard >= 0.9, first occurrence kept",
            "collapsed_into_survivors": sum(
                max(0, r.get("duplicate_count", 1) - 1) for r in corpus_rows),
        },

        "anonymisation": {
            "policy": "scrub-at-write-time; unscrubbed text is never persisted",
            "salt_fingerprint": salt_fingerprint(),
            "redaction_counts": dict(sorted(scrub_counts.items())),
            "rows_flagged_for_review": sum(
                1 for r in corpus_rows if r.get("needs_review")),
            "known_limitation": (
                "Regexes cannot detect person or customer names. The denylist "
                "is the only mechanism for those, and it is only as complete "
                "as whoever wrote it."),
        },

        "model_set": {
            "note": ("The tiers a matrix run would use. Recorded even when no "
                     "matrix has run, so a later run against different models "
                     "is visibly a different measurement."),
            "tiers": _tier_models(),
        },

        "verifier_coverage": {
            "tasks_total": len(tasks),
            "with_mechanical_verifier": len(mechanical),
            "without_verifier": len(subjective),
            "coverage_fraction": (round(len(mechanical) / len(tasks), 4)
                                  if tasks else 0.0),
            "by_verification_type": _verifier_types(mechanical),
        },

        "ambiguous_tasks": {
            "count": len(subjective),
            "reasons": _ambiguity_reasons(subjective),
        },

        "environment": {
            "git_rev": git_rev(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },

        "caveats": [
            "No routing metadata is joined to any historical row: prompt text "
            "and routing decisions live in separate stores with no shared key. "
            "Only rows with source_kind='captured' carry both.",
            "The corpus is agentic-session traffic. Most prompts are not "
            "self-contained tasks, which is why the surviving count is small.",
            "A task is only ground truth once a person has written its "
            "acceptance assertion. Unreviewed stubs are subjective by default "
            "and are excluded from tune/test.",
        ],
    }

    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"froze {out}")
    print(f"  corpus rows        {len(corpus_rows)}")
    print(f"  tasks              {len(tasks)}  "
          f"(mechanical {len(mechanical)}, subjective {len(subjective)})")
    print(f"  tune / test        {len(tune)} / {len(test)}")
    print(f"  content_hash       {manifest['content_hash'][:32]}…")
    if not mechanical:
        print("\n  NOTE: no mechanically-verifiable tasks. tune/test are empty, "
              "and no cheapest_acceptable_model label can be derived until "
              "someone authors verifiers in the tasks file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
