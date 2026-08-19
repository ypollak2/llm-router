#!/usr/bin/env python3
"""Phase 1 — enumerate the G-F mutation universe, split it, seal the holdout.

Protocol doc 20 §2. Run AFTER `gf_mutmut.py` has generated `mutants/`, and
BEFORE any test is written. The split must be fixed while its outcome is still
unknown; that is the entire mechanism.

ENUMERATION IS FROM THE GENERATED TREE, NOT `mutmut results`
------------------------------------------------------------
`mutmut results` listed only `execution_ledger` (642 entries, all "not checked")
even though all eight modules were mutated. Rather than rely on a listing whose
scope I could not explain, the universe is parsed directly from the mutant
definitions mutmut wrote into `mutants/src/llm_router/*.py`. That is the artefact the
run will actually execute, so it is the right ground truth — and it is
independently checkable by anyone re-running the parse.

Mutant names follow the scheme observed in `mutmut results`:
`llm_router.<module>.<def name>`, e.g. `llm_router.execution_ledger.x__db_path__mutmut_1`.

THE SPLIT
---------
    seed = sha256("llm_router-gf-v1" + baseline_sha + universe_sha)
    stratified by (module, mutated function) so no split is dominated by one
    file or one heavily-mutated function
    60% TRAIN · 20% VALIDATION · 20% HOLDOUT

WHAT IS COMMITTED
-----------------
Under `.llm_router/zero-tolerance-audit/gf/`, where G-C's manifest seals it:
  universe.txt          every mutant name, sorted — the auditable ground truth
  train.txt             inspect freely
  validation.txt        score periodically
  holdout.sha256        THE HASH ONLY. Never the names.
  split_metadata.json   seed inputs, counts, tool versions, commit SHA

The holdout list is written to `.llm_router/gf/` (gitignored) so a run can execute
it, but it never enters version control. Anyone can verify the holdout by
re-deriving the split from `universe.txt` and the seed and hashing the result —
which is what makes the seal checkable rather than merely asserted.
"""

from __future__ import annotations

import hashlib
import json
import platform
import random
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MUTANTS = REPO / "mutants" / "src" / "llm_router"
SEALED = REPO / ".llm-router" / "zero-tolerance-audit" / "gf"
WORKING = REPO / ".llm-router" / "gf"

MODULES = [
    "cost", "savings", "execution_ledger",          # money
    "router", "tool_surface", "classify",           # routing
    "budget", "coverage",                           # verification
]

_DEF = re.compile(r"^def (x_.*?__mutmut_\d+)\(", re.M)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _git(*a: str) -> str:
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True).stdout.strip()


def enumerate_universe() -> list[tuple[str, str, str]]:
    """(mutant_name, module, base_function) for every generated mutant."""
    out: list[tuple[str, str, str]] = []
    for mod in MODULES:
        path = MUTANTS / f"{mod}.py"
        if not path.exists():
            sys.exit(f"missing generated module {path} — run gf_mutmut.py first")
        for defname in _DEF.findall(path.read_text()):
            base = defname.rsplit("__mutmut_", 1)[0]
            out.append((f"llm_router.{mod}.{defname}", mod, base))
    if not out:
        sys.exit("enumerated ZERO mutants — the parse is broken, not the tree")
    return sorted(out)


def main() -> int:
    universe = enumerate_universe()
    names = [n for n, _, _ in universe]
    universe_text = "\n".join(names) + "\n"
    universe_sha = _sha256(universe_text)
    baseline_sha = _git("rev-parse", "HEAD")

    seed_material = f"llm_router-gf-v1{baseline_sha}{universe_sha}"
    seed_hex = _sha256(seed_material)
    rng = random.Random(int(seed_hex, 16))

    # Stratify by (module, base function). Shuffling within each stratum and
    # dealing round-robin keeps every split representative: without this, one
    # heavily-mutated function could dominate the holdout and the score would
    # measure that function rather than the modules.
    strata: dict[tuple[str, str], list[str]] = {}
    for name, mod, base in universe:
        strata.setdefault((mod, base), []).append(name)

    train: list[str] = []
    validation: list[str] = []
    holdout: list[str] = []
    for key in sorted(strata):
        bucket = sorted(strata[key])
        rng.shuffle(bucket)
        for i, name in enumerate(bucket):
            # deal 3-of-5 → train, 1 → validation, 1 → holdout ⇒ 60/20/20
            slot = i % 5
            (train if slot < 3 else validation if slot == 3 else holdout).append(name)

    SEALED.mkdir(parents=True, exist_ok=True)
    WORKING.mkdir(parents=True, exist_ok=True)

    (SEALED / "universe.txt").write_text(universe_text)
    (SEALED / "train.txt").write_text("\n".join(sorted(train)) + "\n")
    (SEALED / "validation.txt").write_text("\n".join(sorted(validation)) + "\n")

    holdout_text = "\n".join(sorted(holdout)) + "\n"
    (SEALED / "holdout.sha256").write_text(_sha256(holdout_text) + "\n")
    # The list itself lives OUTSIDE version control. It must exist for the final
    # run to execute, and must never be readable from the repo history.
    (WORKING / "holdout.txt").write_text(holdout_text)

    meta = {
        "protocol": ".llm_router/zero-tolerance-audit/20_GF_QUALIFICATION_PROTOCOL.md",
        "baseline_sha": baseline_sha,
        "git_dirty": bool(_git("status", "--porcelain")),
        "universe_sha256": universe_sha,
        "universe_size": len(names),
        "seed_material": "sha256('llm_router-gf-v1' + baseline_sha + universe_sha256)",
        "seed_sha256": seed_hex,
        "strata": len(strata),
        "counts": {
            "train": len(train), "validation": len(validation), "holdout": len(holdout),
        },
        "holdout_sha256": _sha256(holdout_text),
        "holdout_list_location": "UNVERSIONED (.llm_router/gf/holdout.txt)",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "modules": MODULES,
    }
    (SEALED / "split_metadata.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(json.dumps(meta["counts"], indent=2))
    print(f"universe   {len(names)}  sha {universe_sha[:16]}")
    print(f"strata     {len(strata)}")
    print(f"holdout    SEALED, sha {meta['holdout_sha256'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
