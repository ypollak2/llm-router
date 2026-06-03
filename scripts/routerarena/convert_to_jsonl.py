"""V2 converter — normalize all multiple-choice references to letters A-J.

Most RouterArena datasets (MMLUPro_*, PubMedQA, GeoBench, MedMCQA) ship
numeric answer indices (0-9). The model gets asked for "the letter" in
the prompt so the reference must match. We:

* Skip LiveCodeBench (code-execution scoring out of scope for now).
* Skip NarrativeQA (literal text answers — needs a different evaluator).
* Convert numeric refs (0-9) → letters (A-J).
* Keep letter refs (A-J) untouched.
* Shuffle deterministically for representative subject distribution.
"""

import json
import random
import pandas as pd
from pathlib import Path

SRC = '/tmp/routerarena_sub10.parquet'
DST = Path.home() / '.llm-router' / 'data' / 'routerarena' / 'sub_10_letters.jsonl'

SKIP_DATASETS = {'LiveCodeBench', 'NarrativeQA'}

DATASET_TO_SUBJECT = {
    'PubMedQA': 'medical', 'MedMCQA': 'medical', 'MMLUPro_health': 'medical',
    'LiveCodeBench': 'code', 'MMLUPro_computer science': 'code',
    'MMLUPro_engineering': 'code',
    'NarrativeQA': 'narrative', 'QANTA_Literature': 'narrative',
    'MMLUPro_history': 'history',
    'MMLUPro_math': 'reasoning', 'MMLUPro_psychology': 'reasoning',
    'MMLUPro_philosophy': 'reasoning', 'MMLUPro_economics': 'reasoning',
    'MMLUPro_law': 'reasoning', 'MMLUPro_business': 'reasoning',
    'MMLUPro_chemistry': 'physics', 'MMLUPro_physics': 'physics',
    'MMLUPro_biology': 'medical',
    'MusicTheoryBench': 'general', 'GeoBench': 'general', 'ArcMMLU': 'general',
}
DOMAIN_TO_SUBJECT = {
    '0 Computer science, information, and general works': 'code',
    '1 Philosophy and psychology': 'reasoning',
    '3 Social Science': 'reasoning',
    '4 Language': 'narrative',
    '5 Science': 'physics',
    '6 Technology': 'code',
    '7 Arts & recreation': 'general',
    '8 Literature': 'narrative',
    '9 History': 'history',
}
LETTERS = list("ABCDEFGHIJ")


def normalize_reference(raw, options) -> str | None:
    """Convert a raw answer to a letter A-J. Returns None for skip."""
    s = str(raw).strip()
    if not s:
        return None
    # Already a letter
    if len(s) == 1 and s.upper() in LETTERS:
        return s.upper()
    # Numeric index → letter
    if s.isdigit():
        idx = int(s)
        if 0 <= idx < len(LETTERS):
            return LETTERS[idx]
    # Multi-line literal answers (NarrativeQA-style) — skip
    return None


def format_prompt(row, normalized_ref: str) -> str:
    q = (row.get('Question') or '').strip()
    options = row.get('Options')
    ctx = (row.get('Context') or '').strip()
    parts = []
    if ctx:
        # Trim very long contexts to keep token cost bounded
        ctx_trim = ctx[:1500] + ("…" if len(ctx) > 1500 else "")
        parts.append(f"Context: {ctx_trim}")
    parts.append(f"Question: {q}")
    if options is not None and len(options) > 0:
        n_opts = min(len(options), len(LETTERS))
        opts_str = "\n".join(f"{LETTERS[i]}. {options[i]}" for i in range(n_opts))
        parts.append(f"Options:\n{opts_str}")
    parts.append("Respond with ONLY the single letter of the correct answer. No explanation. No period.")
    return "\n\n".join(parts)


def pick_subject(row) -> str:
    ds = str(row.get('Dataset name', ''))
    if ds in DATASET_TO_SUBJECT:
        return DATASET_TO_SUBJECT[ds]
    dom = str(row.get('Domain', ''))
    if dom in DOMAIN_TO_SUBJECT:
        return DOMAIN_TO_SUBJECT[dom]
    return 'general'


def main():
    df = pd.read_parquet(SRC)
    DST.parent.mkdir(parents=True, exist_ok=True)

    rows_out = []
    skipped = {'dataset': 0, 'reference': 0, 'no_options': 0}
    for _, row in df.iterrows():
        ds = str(row.get('Dataset name', ''))
        if ds in SKIP_DATASETS:
            skipped['dataset'] += 1
            continue
        options = row.get('Options')
        if options is None or len(options) == 0:
            skipped['no_options'] += 1
            continue
        ref = normalize_reference(row.get('Answer'), options)
        if ref is None:
            skipped['reference'] += 1
            continue
        rows_out.append({
            'id': str(row['Global Index']),
            'text': format_prompt(row, ref),
            'reference': ref,
            'subject': pick_subject(row),
            'task_type': 'query',
            'dataset': ds,
            'difficulty': str(row['Difficulty']),
        })

    random.Random(42).shuffle(rows_out)

    with DST.open('w') as f:
        for r in rows_out:
            f.write(json.dumps(r) + '\n')

    from collections import Counter
    print(f'Wrote {len(rows_out)} prompts to {DST}')
    print(f'Skipped: {skipped}')
    print(f'\nSubject distribution:')
    for s, n in Counter(r['subject'] for r in rows_out).most_common():
        print(f'  {s:<10} {n}')
    print(f'\nReference letter distribution:')
    for s, n in Counter(r['reference'] for r in rows_out).most_common():
        print(f'  {s} {n}')


if __name__ == '__main__':
    main()
