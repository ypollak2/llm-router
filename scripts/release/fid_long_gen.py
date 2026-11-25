#!/usr/bin/env python3
"""fid_long_120: deterministic long-session fidelity items (D-352, owner D-619/D-620). REPORT-ONLY.

    python scripts/release/fid_long_gen.py --hash     # sha256 of the canonical JSON of all items
    python scripts/release/fid_long_gen.py --summary  # per-category counts and token-estimate range

120 synthetic Claude Code sessions (6 categories x 20), invented here from SEED_LONG; no real
prompt is read. Each session is CYCLES=40 tool cycles (~71k estimated tokens, above the 65536
cloud window of the P32 fidelity set), so the pack builder must choose ``pack`` mode, not ``full``.
One answer token is planted once per session; a late decoy token of the same shape is planted in
a late assistant note. Only g1_synthetic_evidence helpers are reused (that file is frozen, pin
b0d2e138); this module never edits them.

  category          where the token sits (cycle c of 40)
  first_user        first human request (control: the rolling summary keeps its first 200 chars)
  recent            last assistant note, c=39 (control: inside the last RECENT_N=7 messages)
  early_user_mid    human request that opens step 9 (~20%)
  early_tool        tool result of c=4 (~10%)
  early_assistant   assistant note of c=10 (~25%)
  mid_tool          tool result of c=20 (~50%)
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
G1_REL = "scripts/g1_synthetic_evidence.py"
SEED_LONG = 20261125
CYCLES = 40
PER_CATEGORY = 20
CATEGORIES = ("first_user", "recent", "early_user_mid", "early_tool", "early_assistant", "mid_tool")
CONTROLS = ("first_user", "recent")
N_ITEMS = PER_CATEGORY * len(CATEGORIES)
#: PREREG-P32-AMEND2 production cloud window; whole must exceed 0.8 x this to force pack mode.
MIN_WHOLE_TOKENS = 65535
#: cycle at which each test category plants its token
POS = {"early_user_mid": 8, "early_tool": 4, "early_assistant": 10, "mid_tool": 20}
REQUESTS = {
    "first_user": "Before I deploy: which staging port did I ask for at the very start?",
    "recent": "What run id did you report in your last message?",
    "early_user_mid": "Which debug port did I ask you to keep a few steps in?",
    "early_tool": "What value did ingest_salt have in the config you read earlier?",
    "early_assistant": "What name did you say you would give the new module?",
    "mid_tool": "What was the build stamp in the output of the check you ran halfway through?",
}


def load_g1(root: Path = ROOT):
    """g1 as a module, loaded by path (same trick as run_p32.load_g1; one shared instance)."""
    if "_p32_g1" in sys.modules:
        return sys.modules["_p32_g1"]
    spec = importlib.util.spec_from_file_location("_p32_g1", Path(root) / G1_REL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_p32_g1"] = mod  # dataclasses resolves cls.__module__ here
    spec.loader.exec_module(mod)
    return mod


def category_of(index: int) -> str:
    return CATEGORIES[index // PER_CATEGORY]


def item_id(index: int) -> str:
    return f"fidl-{index}"


def build_item(index: int, root: Path = ROOT):
    """One session as a g1.Item (item_id fidl-<index>, 40 cycles, one planted token, one decoy)."""
    g1 = load_g1(root)
    category = category_of(index)
    rng = random.Random(SEED_LONG * 1000 + index)
    token = g1._answer_token(rng)
    decoy = g1._answer_token(rng)
    while decoy == token:
        decoy = g1._answer_token(rng)
    entries: list[dict] = []

    def add(role: str, content) -> None:
        entries.append(g1._cc(role, content, len(entries)))

    first = "Set up the ledger ingest service in this repo and keep the existing tests green."
    if category == "first_user":
        first = (f"Set up the ledger ingest service; the staging port must be {token}. "
                 "Keep the existing tests green.")
    add("user", [{"type": "text", "text": first}])
    for c in range(CYCLES):
        tool = "Read" if c % 2 == 0 else "Bash"
        inp = ({"file_path": f"/work/svc/{rng.choice(g1._IDENT)}_{c}.py"} if tool == "Read"
               else {"command": f"pytest -q tests/test_{rng.choice(g1._IDENT)}_{c}.py"})
        add("assistant", [{"type": "text", "text": f"Checking step {c + 1}."},
                          {"type": "tool_use", "id": f"t{c}", "name": tool, "input": inp}])
        body = g1._filler(rng, "code" if tool == "Read" else "log")
        if category == "early_tool" and c == POS["early_tool"]:
            body += f'\n\n# config\ningest_salt = "{token}"\nretry_limit = 5'
        if category == "mid_tool" and c == POS["mid_tool"]:
            body += f"\n\nbuild stamp: {token} (check ok)"
        add("user", [{"type": "tool_result", "tool_use_id": f"t{c}", "content": body}])
        note = f"Step {c + 1} done; moving on."
        if category == "early_assistant" and c == POS["early_assistant"]:
            note = f"I'll name the new module {token} so it does not clash with the old one."
        if category == "recent" and c == CYCLES - 1:
            note = f"Step {c + 1} done; the run id is {token}."
        if c == CYCLES - 3:  # late decoy: same shape, different value, never the answer
            note += f" (An older run used {decoy}.)"
        add("assistant", [{"type": "text", "text": note}])
        if c < CYCLES - 1:
            ask = f"Good. Continue with step {c + 2}."
            if category == "early_user_mid" and c + 1 == POS["early_user_mid"]:
                ask += f" Keep the debug port at {token}."
            add("user", [{"type": "text", "text": ask}])
    return g1.Item(item_id(index), category, token, entries, REQUESTS[category])


def items(root: Path = ROOT, indices: list[int] | None = None) -> list:
    """The set (or the chosen indices), every structural property asserted on the way out."""
    g1 = load_g1(root)
    chosen = range(N_ITEMS) if indices is None else indices
    out = []
    for i in chosen:
        it = build_item(i, root)
        full = g1.full_context(it)
        est = len(full) // 4  # token_budget.estimate_tokens is len // 4
        assert est >= MIN_WHOLE_TOKENS, f"{it.item_id}: whole={est} est tokens < {MIN_WHOLE_TOKENS}"
        assert full.count(it.token) == 1, f"{it.item_id}: token not unique in FULL"
        out.append(it)
    if indices is None:
        assert len(out) == N_ITEMS
        assert len({it.token for it in out}) == N_ITEMS, "token reused across items"
        for cat in CATEGORIES:
            assert sum(1 for it in out if it.category == cat) == PER_CATEGORY
    return out


def canonical(item) -> dict:
    return {"id": item.item_id, "category": item.category, "token": item.token,
            "request": item.request, "entries": item.entries}


def set_hash(chosen: list) -> str:
    blob = json.dumps([canonical(it) for it in chosen], sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hash", action="store_true", help="print the canonical-JSON sha256 of all items")
    ap.add_argument("--summary", action="store_true", help="print per-category counts and size range")
    a = ap.parse_args(argv)
    chosen = items()
    if a.hash:
        print(set_hash(chosen))
    if a.summary or not a.hash:
        g1 = load_g1()
        est = [len(g1.full_context(it)) // 4 for it in chosen]
        print(f"n={len(chosen)} categories=" + ", ".join(
            f"{c}={sum(1 for it in chosen if it.category == c)}" for c in CATEGORIES)
            + f" whole_est_tokens min={min(est)} median={sorted(est)[len(est) // 2]} max={max(est)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
