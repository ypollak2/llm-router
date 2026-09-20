#!/usr/bin/env python3
"""Turn corpus prompts into a task-authoring queue.

    python3 scripts/groundtruth/author_tasks.py \
        --corpus data/groundtruth/corpus.jsonl \
        --out data/groundtruth/tasks/draft.jsonl

Every corpus row becomes a task stub marked SUBJECTIVE with
`ambiguity_reason: "unreviewed"`. A person then either

  * writes a `verifier` snippet and flips `verifier_kind` to "mechanical", or
  * replaces the reason with the real one and leaves it subjective.

Nothing here decides acceptability. It cannot: the repo has no verifier that
derives a check from a prompt (see `dataset.py`), so the authoring step is
irreducibly human and this script only makes the backlog countable.

`--suggest` adds a non-binding hint about which verifier helper *might* apply,
based on the shape of the prompt. A hint is not a label; a stub keeps
`verifier_kind: subjective` until a person changes it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth.dataset import EDIT, QA, SUBJECTIVE, Task  # noqa: E402

# Shapes that hint at a mechanical check. Purely advisory.
_HINTS: list[tuple[str, re.Pattern[str], str]] = [
    ("num", re.compile(r"\b(how many|what is the (default |current )?value|"
                       r"answer with (just )?the number)\b", re.I),
     'num(<expected>)  # answer must contain that integer'),
    ("words", re.compile(r"\b(which file|what file|answer with the (file )?path|"
                         r"which function|name the)\b", re.I),
     'words("<expected>")  # answer must mention this token'),
    ("yesno", re.compile(r"^\s*(is|are|does|do|can|should|will|has|have)\b", re.I),
     'yesno("yes"|"no")'),
    ("run", re.compile(r"\b(fix|change|make|implement|add|remove|rename|update|"
                       r"refactor|correct)\b", re.I),
     "run('''from <mod> import <sym>; assert ...''')  # behavioural check"),
    ("pytest", re.compile(r"\b(test|tests|suite|pytest|failing)\b", re.I),
     "pytest_passes('tests')"),
]


def hint_for(text: str) -> str:
    for _name, pattern, helper in _HINTS:
        if pattern.search(text):
            return helper
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=Path("data/groundtruth/corpus.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("data/groundtruth/tasks/draft.jsonl"))
    ap.add_argument("--prefix", default="gt")
    ap.add_argument("--suggest", action="store_true", help="add advisory verifier hints")
    ap.add_argument("--triage", default="standalone,repo-bound",
                    help="which corpus triage kinds to queue")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not args.corpus.exists():
        raise SystemExit(f"corpus not found: {args.corpus} (run extract_corpus.py first)")

    keep = {k.strip() for k in args.triage.split(",") if k.strip()}
    rows = [json.loads(ln) for ln in args.corpus.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = [r for r in rows if r.get("triage") in keep]
    rows.sort(key=lambda r: (-r.get("duplicate_count", 1), r.get("content_sha", "")))
    if args.limit:
        rows = rows[: args.limit]

    stubs: list[Task] = []
    for i, r in enumerate(rows, start=1):
        text = " ".join(r["text"].split())
        # repo-bound work changes files; standalone work answers a question.
        kind = EDIT if r.get("triage") == "repo-bound" else QA
        stubs.append(Task(
            task_id=f"{args.prefix}-{i:04d}",
            prompt=text,
            kind=kind,
            verifier_kind=SUBJECTIVE,
            verifier=None,
            ambiguity_reason="unreviewed",
            sandbox="TODO" if kind == EDIT else None,
            origin_sha=r.get("content_sha"),
            origin_file=r.get("source_file"),
            origin_index=r.get("source_index"),
            triage=r.get("triage"),
            authored_by="",
            authored_at="",
            notes=(f"hint: {hint_for(text)}" if args.suggest and hint_for(text) else ""),
        ))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for t in stubs:
            fh.write(json.dumps(t.to_json(), ensure_ascii=False, sort_keys=True) + "\n")

    by_kind: dict[str, int] = {}
    hinted = 0
    for t in stubs:
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
        if t.notes:
            hinted += 1
    print(f"wrote {args.out} ({len(stubs)} stubs)")
    print(f"  by kind      : {by_kind}")
    if args.suggest:
        print(f"  with a hint  : {hinted}  (advisory only — still subjective)")
    print()
    print("All stubs are SUBJECTIVE until a person writes a verifier.")
    print("Authoring backlog is the bottleneck, and it is now countable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
