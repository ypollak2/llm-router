#!/usr/bin/env python3
"""What changes if router.py attaches context through the shared choke point?

router.py is the last execution path that injects OKF itself, with its own
`find_relevant` + `inject_context` pair. `tests/test_okf_choke_point.py`
exempts it by name. Removing that exemption looks like deduplication and is
not: `context_injection.inject()` composes THREE sources where router.py
composes one.

    OKF concepts      both
    repo facts        inject() only — branch, dirty state, read from git
    session context   inject() only, and only when a session_id is passed

So every routed prompt would gain material it has never carried, changing the
content and the token budget of every call. That could move grounding accuracy
in either direction, and "it's just a refactor" is exactly the sentence that
ships it unmeasured.

This script measures the delta before the swap, without calling a model: it
builds both prompt forms for a set of prompts and reports what differs. No
model judges anything; the comparison is over bytes.

    python3 scripts/shadow_diff_router_injection.py --root . --n 12

Report per prompt: bytes before, bytes after, which blocks were added, and
whether the OKF portion is identical. The last one is the real question — if
the OKF half is byte-identical then the swap's risk is entirely in the two new
blocks, which is a much smaller thing to argue about.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Prompts shaped like the ones the router actually sees. Kept here rather than
# derived, because the point is the CONTEXT ASSEMBLY, not the answer — and a
# fixed set makes two runs comparable.
PROMPTS = [
    "what does find_relevant do?",
    "add a test for the scope resolver",
    "why is the semantic cache returning a stale answer?",
    "explain the routing policy",
    "fix the failing test in tests/okf/",
    "what is the capital of Portugal?",
    "refactor project_knowledge_dir to take an explicit root",
    "summarise the last release",
    "how do I run the benchmark?",
    "where is inject_context called from?",
    "write a haiku",
    "is CI green?",
]


def router_form(prompt: str, root: str | None) -> str:
    """What router.py builds today: OKF concepts, attached by okf itself."""
    from llm_router import okf

    concepts = okf.find_relevant(prompt, root=root)
    return okf.inject_context(prompt, concepts) if concepts else prompt


def choke_point_form(prompt: str, root: str | None, session_id: str | None) -> str:
    """What context_injection.inject() builds: OKF + repo facts + session."""
    from llm_router.context_injection import inject

    return inject(prompt, root=root, session_id=session_id)


def blocks_added(before: str, after: str) -> list[str]:
    """Which named blocks appear in *after* and not in *before*."""
    known = {
        "<knowledge_context>": "okf",
        "<repo_state>": "repo_facts",
        "<session_context>": "session",
        "[session ": "session",
    }
    out = []
    for marker, name in known.items():
        if marker in after and marker not in before and name not in out:
            out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(REPO))
    ap.add_argument("--session-id", default="",
                    help="pass one to measure the session block too; the swap "
                         "should NOT start passing a session_id by accident")
    ap.add_argument("--n", type=int, default=len(PROMPTS))
    ap.add_argument("--out", default="")
    ap.add_argument("--show-diff", action="store_true")
    args = ap.parse_args()

    from llm_router.semantic.scope import resolve_scope
    root = str(resolve_scope(args.root))
    sid = args.session_id or None

    print(f"root = {root}")
    print(f"session_id = {sid!r}  (None means the session block cannot appear)")
    print(f"n = {min(args.n, len(PROMPTS))} prompts\n")

    rows = []
    identical_okf = changed = 0
    for prompt in PROMPTS[:args.n]:
        before = router_form(prompt, root)
        after = choke_point_form(prompt, root, sid)
        added = blocks_added(before, after)
        # The OKF half on its own: strip everything inject() prepends, then
        # compare. If these match, the swap does not change retrieval at all.
        okf_same = before in after or before == after
        identical_okf += okf_same
        if before != after:
            changed += 1
        rows.append({
            "prompt": prompt,
            "before_bytes": len(before),
            "after_bytes": len(after),
            "delta_bytes": len(after) - len(before),
            "blocks_added": added,
            "okf_half_identical": okf_same,
        })
        print(f"{len(before):7d} → {len(after):7d} "
              f"({len(after) - len(before):+6d})  "
              f"okf={'same' if okf_same else 'DIFFERENT'}  "
              f"added={','.join(added) or '-'}  {prompt[:44]}")
        if args.show_diff and before != after:
            for line in difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                "router", "choke_point", lineterm="", n=1,
            ):
                print("    " + line)

    n = len(rows)
    total_delta = sum(r["delta_bytes"] for r in rows)
    print(f"\nn = {n}")
    print(f"prompts whose text changes at all: {changed}/{n}")
    print(f"prompts where the OKF half is byte-identical: {identical_okf}/{n}")
    print(f"total added bytes: {total_delta:+d} "
          f"(mean {total_delta / n:+.0f} per prompt)")
    deltas = {r["delta_bytes"] for r in rows}
    if len(deltas) == 1:
        print(f"the delta is the same for every prompt in this run "
              f"({deltas.pop():+d} bytes)")
    else:
        print(f"** the delta VARIES across prompts: {sorted(deltas)} **")
    print("NOTE: that magnitude is a property of the working tree, not of the "
          "change. <repo_state> renders the branch name, the last commit "
          "subject and the dirty-file list, so it grows and shrinks as you "
          "work — measured ~211 bytes clean and ~260 dirty on one machine. "
          "Quote 'the same for every prompt', never a fixed byte count.")

    if identical_okf == n:
        print("\nretrieval is unchanged by the swap — the entire delta is the "
              "blocks inject() adds, and those are listed above")
    else:
        print("\n** the OKF half DIFFERS on some prompts: the swap changes "
              "retrieval itself, not just what is attached alongside it **")

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nper-prompt detail: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
