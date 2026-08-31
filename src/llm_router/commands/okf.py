"""``llm_router okf`` — inspect and garbage-collect the OKF knowledge store.

Why this exists (CHZ-OKF-02)
────────────────────────────
OKF injects stored docs as context before routing. That makes the store an input
to every routed answer, which makes a wrong doc worse than no doc: it is quietly
presented to the model as background fact.

The verified-only policy (v0.8.4) stopped new model prose from being written, but
it never removed what was already there. Docs written before it are still scored,
still retrieved, still injected. In the field one such doc — titled ``README.md``,
so it matched almost any README-ish prompt — carried a filename the model had
invented outright.

``gc`` finds those and moves them to ``knowledge/quarantine/``, out of retrieval
but fully recoverable. It never deletes: withdrawing a user's knowledge is a call
they get to make, and a mistaken purge is unrecoverable.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _b(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"


def _g(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _y(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def _print_help() -> None:
    print(f"""
{_b('llm-router okf')} — inspect the knowledge store that feeds routed prompts

  {_b('llm-router okf status')}        what is stored, per project, and what gets injected
  {_b('llm-router okf gc')}            report docs that are model prose rather than verified structure
  {_b('llm-router okf gc --apply')}    move those to knowledge/quarantine/ (recoverable, never deleted)
  {_b('llm-router okf adopt')}         move VERIFIED legacy docs into this project's store
  {_b('llm-router okf restore')}       move everything in quarantine/ back into retrieval

Docs are scoped per project. Retrieval sees this project's docs plus the shared
model catalog — never another project's.
""".rstrip())


def cmd_okf(args: list[str]) -> None:
    from llm_router import okf

    sub = args[0] if args else "status"

    if sub in ("-h", "--help", "help"):
        _print_help()
        return

    if sub == "status":
        root = okf.project_root()
        pdir = okf.project_knowledge_dir()
        print(f"\n{_b('OKF knowledge store')}")
        print(f"  enabled        : {'yes' if okf._okf_enabled() else 'no (LLM_ROUTER_OKF=off)'}")
        print(f"  project        : {root}")
        print(f"  project store  : {pdir}")
        print(f"  shared catalog : {okf.MODELS_DIR}")

        bundle = okf._get_bundle()
        print(f"\n  {_b('injectable right now')}: {len(bundle)} doc(s)")
        for c in bundle:
            print(f"    {c.type:16} {c.title}")

        legacy = sorted(okf.LEGACY_SOURCE_DIR.rglob("*.md")) if okf.LEGACY_SOURCE_DIR.exists() else []
        if legacy:
            print(f"\n  {_y('legacy (pre-scoping, NOT injected)')}: {len(legacy)} doc(s)")
            print(_dim("    These predate per-project scoping and were shared across all"))
            print(_dim("    projects. Run `llm-router okf gc` to see which are model prose."))
            for p in legacy:
                print(f"    {p.name}")

        q = sorted(okf.QUARANTINE_DIR.rglob("*.md")) if okf.QUARANTINE_DIR.exists() else []
        if q:
            print(f"\n  {_dim('quarantined')}: {len(q)} doc(s) — `llm-router okf restore` to undo")

        others = sorted(okf.PROJECTS_DIR.glob("*")) if okf.PROJECTS_DIR.exists() else []
        if len(others) > 1:
            print(f"\n  {_dim('other projects with a store')}: {len(others) - 1}")
        print()
        return

    if sub == "gc":
        apply = "--apply" in args
        report = okf.gc_store(apply=apply)
        print(f"\n{_b('OKF garbage collection')} {'' if apply else _dim('(dry run)')}")
        print(f"  verified, kept  : {report['kept']}")
        print(f"  flagged as prose: {report['flagged']}")

        if not report["flagged"]:
            print(f"\n  {_g('Nothing to do — every stored doc is verified structure.')}\n")
            return

        print(f"\n  {_y('These are free text produced by a model, stored as fact,')}")
        print(f"  {_y('and injected into prompts that keyword-match them:')}\n")
        for path, title, why in report["flagged_docs"]:
            print(f"    {_b(title)}")
            print(_dim(f"      {path}"))
            print(_dim(f"      {why}"))

        if apply:
            moved_count = len(report["moved"])
            print("\n  " + _g(f"Moved {moved_count} doc(s) to quarantine/"))
            for src, dest in report["moved"]:
                print(_dim(f"    {Path(src).name} → {dest}"))
            print(_dim("\n  Nothing was deleted. `llm-router okf restore` puts them back.\n"))
        else:
            print(f"\n  Run {_b('llm-router okf gc --apply')} to move them to quarantine/.")
            print(_dim("  They are moved, never deleted, and can be restored.\n"))
        return

    if sub == "adopt":
        # Legacy docs were written before scoping, so which project they describe is
        # not recorded anywhere — only the user knows. Rather than guess (and
        # re-create the cross-contamination scoping just removed), adoption is
        # explicit: run it from the project the docs belong to.
        legacy = sorted(okf.LEGACY_SOURCE_DIR.rglob("*.md")) if okf.LEGACY_SOURCE_DIR.exists() else []
        if not legacy:
            print("\n  No legacy docs to adopt.\n")
            return
        dest_root = okf.project_knowledge_dir() / "source"
        adopted, skipped = 0, 0
        for md in legacy:
            try:
                concept = okf._parse_okf(md.read_text(encoding="utf-8"), md)
            except OSError:
                continue
            if concept is None:
                continue
            verdict, why = okf.classify_concept(concept)
            if verdict != "keep":
                print(_dim(f"  skipped {md.name} — {why} (use `okf gc --apply`)"))
                skipped += 1
                continue
            dest = dest_root / md.relative_to(okf.LEGACY_SOURCE_DIR)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                print(_y(f"  skipped {md.name} — already present in this project"))
                continue
            md.replace(dest)
            adopted += 1
        okf.invalidate_cache()
        print(f"\n  Adopted {adopted} verified doc(s) into {okf.project_root().name}.")
        if skipped:
            print(_dim(f"  Left {skipped} unverified doc(s) where they are — quarantine them with `okf gc --apply`."))
        print()
        return

    if sub == "restore":
        qdir = okf.QUARANTINE_DIR
        if not qdir.exists():
            print("\n  Nothing in quarantine.\n")
            return
        restored = 0
        for md in sorted(qdir.rglob("*.md")):
            dest = okf.KNOWLEDGE_DIR / md.relative_to(qdir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                print(_y(f"  skipped {md.name} — {dest} already exists"))
                continue
            md.replace(dest)
            restored += 1
        okf.invalidate_cache()
        print(f"\n  Restored {restored} doc(s) from quarantine.\n")
        return

    print(f"Unknown subcommand: {sub!r}")
    _print_help()
    sys.exit(1)
