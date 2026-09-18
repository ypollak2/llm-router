#!/usr/bin/env python3
"""Question sets that are not rigged in the index's favour.

The first benchmark asked "which file defines `post_entry`?" and the semantic
layer scored 58/60 against the corrected baseline's 41/60. That result is real
and it is also close to a best case: the query names the symbol verbatim, an
`ast` index looks it up exactly, and lexical matching over prose documents is
at its worst. The blueprint warns about precisely this reading — a
graph-favourable diagnostic set cannot establish general product value.

So these are the strata where it should struggle, derived the same way: from
the repository, by grep and `ast`, never authored. An authored question set is
a question set shaped by whoever knows what the system does well.

    symbol   the original. The query names the identifier.
    concept  a function's own docstring, with the identifier NEVER mentioned.
             `retrieve.seeds_from` extracts identifiers from the query, so on
             these it has nothing to seed from and should return empty — while
             OKF's lexical matching over stored prose has something to work
             with. The prediction is that the layer LOSES here.
    decoy    symbols whose file basename occurs in two or more directories, so
             naming the file is not enough and the directory has to be right.
    absent   symbols that do not exist anywhere. The correct answer is to say
             so. Retrieval that invents a plausible file is worse than no
             retrieval, and nothing in the first benchmark could detect that.

Ground truth for every stratum comes from the source tree, not from the index
being evaluated — grading an index with its own output measures nothing but its
self-consistency.
"""
from __future__ import annotations

import ast
import collections
import random
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)", re.MULTILINE)
_CLASS_RE = re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)

STRATA = ("symbol", "concept", "decoy", "absent")


def _tracked_python() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.py"],
                         capture_output=True, text=True, timeout=60)
    paths = []
    for rel in out.stdout.split():
        # The plugin bundle duplicates hook files verbatim, so a symbol defined
        # "once" in src/ also exists under .claude/ and .codex-plugin/. Ground
        # truth has to be unambiguous, and a duplicated file makes it not.
        if rel.startswith((".claude/", ".codex-plugin/", ".factory-plugin/")):
            continue
        paths.append(REPO / rel)
    return paths


def _definitions() -> dict[str, list[Path]]:
    where: dict[str, list[Path]] = collections.defaultdict(list)
    for path in _tracked_python():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in (_DEF_RE, _CLASS_RE):
            for name in set(pattern.findall(text)):
                where[name].append(path)
    return where


def _unique_symbols(where: dict[str, list[Path]]) -> list[str]:
    return sorted(n for n, paths in where.items()
                  if len(paths) == 1
                  and not n.startswith("test_") and not n.startswith("__"))


def derive_symbol(n: int, seed: int) -> list[dict]:
    """The original stratum: the query names the identifier."""
    where = _definitions()
    names = _unique_symbols(where)
    random.Random(seed).shuffle(names)
    out = []
    for name in names[:n]:
        target = where[name][0].relative_to(REPO)
        out.append({
            "stratum": "symbol", "symbol": name, "answer": str(target),
            "basename": target.name,
            "prompt": (f"Which file in this project defines `{name}`? "
                       f"Answer with the file path and nothing else."),
        })
    return out


def derive_concept(n: int, seed: int) -> list[dict]:
    """A function's own docstring, with its name never mentioned.

    The hard part is uniqueness. A docstring line that appears in two files
    cannot have a single right answer, and this repository has plenty of
    near-duplicates, so any first line seen more than once is discarded
    outright rather than resolved by a tiebreak nobody can audit.
    """
    seen: dict[str, list[tuple[Path, str]]] = collections.defaultdict(list)
    for path in _tracked_python():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError, RecursionError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            first = doc.strip().splitlines()[0].strip()
            words = first.split()
            if not (6 <= len(words) <= 28):
                continue
            # The identifier must not leak into the question in any form — not
            # as the name, not split on underscores. Otherwise this is the
            # symbol stratum wearing a sentence.
            parts = [p for p in node.name.split("_") if len(p) > 3]
            lowered = first.lower()
            if node.name.lower() in lowered or any(p in lowered for p in parts):
                continue
            seen[first].append((path, node.name))

    unique = sorted(k for k, v in seen.items() if len(v) == 1)
    random.Random(seed).shuffle(unique)
    out = []
    for line in unique[:n]:
        path, name = seen[line][0]
        target = path.relative_to(REPO)
        out.append({
            "stratum": "concept", "symbol": name, "answer": str(target),
            "basename": target.name,
            "prompt": (f'Which file in this project implements this? "{line}" '
                       f"Answer with the file path and nothing else."),
        })
    return out


def derive_decoy(n: int, seed: int) -> list[dict]:
    """Symbols whose file basename lives in two or more directories.

    Naming the file is not enough here; the directory has to be right. The
    strict scorer already rejects a wrong directory — this is the stratum that
    makes it matter.
    """
    where = _definitions()
    basenames = collections.Counter(p.name for p in _tracked_python())
    ambiguous = {b for b, count in basenames.items() if count >= 2}

    names = [n_ for n_ in _unique_symbols(where)
             if where[n_][0].name in ambiguous]
    random.Random(seed).shuffle(names)
    out = []
    for name in names[:n]:
        target = where[name][0].relative_to(REPO)
        out.append({
            "stratum": "decoy", "symbol": name, "answer": str(target),
            "basename": target.name,
            "prompt": (f"Which file in this project defines `{name}`? "
                       f"Answer with the file path and nothing else."),
        })
    return out


def derive_absent(n: int, seed: int) -> list[dict]:
    """Symbols that do not exist. The right answer is to say so.

    Built by mangling real names so they stay plausible — a question about
    `zzqqxx` tests nothing. Retrieval that invents a confident file path for a
    symbol nobody wrote is worse than no retrieval at all, and the first
    benchmark had no way to see it happening.
    """
    where = _definitions()
    real = set(where)
    names = _unique_symbols(where)
    rng = random.Random(seed)
    rng.shuffle(names)

    out = []
    for name in names:
        if len(out) >= n:
            break
        # Shuffled per question. Taking the first workable suffix every time
        # made every absent question end in `_for_batch`, which is a tell a
        # model can learn without knowing anything about the repository.
        suffixes = ["_for_batch", "_v2", "_internal", "_async", "_legacy",
                    "_impl", "_cached"]
        rng.shuffle(suffixes)
        for suffix in suffixes:
            candidate = name + suffix
            if candidate not in real:
                out.append({
                    "stratum": "absent", "symbol": candidate, "answer": "",
                    "basename": "",
                    "prompt": (f"Which file in this project defines "
                               f"`{candidate}`? Answer with the file path and "
                               f"nothing else, or say NOT FOUND if it does not "
                               f"exist."),
                })
                break
    return out


_DERIVERS = {
    "symbol": derive_symbol,
    "concept": derive_concept,
    "decoy": derive_decoy,
    "absent": derive_absent,
}


def derive(stratum: str, n: int, seed: int) -> list[dict]:
    if stratum not in _DERIVERS:
        raise ValueError(f"unknown stratum {stratum!r}; valid: {list(STRATA)}")
    return _DERIVERS[stratum](n, seed)


def score_absent(answer: str) -> bool:
    """Correct means declining to name a file for something that is not there.

    Deliberately generous about the phrasing and strict about the substance: any
    plausible path-shaped token in the reply is a fabrication, whatever hedging
    surrounds it.
    """
    text = (answer or "").strip()
    if not text:
        return False
    if re.search(r"[\w./-]+\.(?:py|pyi|ts|js|go|rs|java)", text):
        return False
    return bool(re.search(
        r"not\s+found|does\s*n[o']?t\s+exist|no\s+such|cannot\s+find|"
        r"could\s+not\s+find|unable\s+to\s+find|not\s+defined|no\s+file",
        text, re.IGNORECASE))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--stratum", default="concept", choices=STRATA)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    for q in derive(args.stratum, args.n, args.seed):
        print(f"{q['answer'] or '(absent)':50s} {q['prompt'][:110]}")
