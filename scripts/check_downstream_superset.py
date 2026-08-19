#!/usr/bin/env python3
"""Is upstream a superset of downstream? The precondition for the sync, measured.

WHY THIS EXISTS
===============

36_DOWNSTREAM_SYNC_PLAN.md Step 2 says: "Copy upstream ``src/`` minus the
exclusion set, into the downstream package layout." That instruction is safe
only if upstream contains everything downstream does. Nobody had checked.

It does not. Measured 2026-08-19, upstream ``LLM Router/src/llm_router`` against
downstream ``llm-router/src/llm_router``: **47 top-level symbols** are defined
downstream and nowhere upstream, across whole features:

    response_validation.py    ABSENT upstream — 9 symbols, an entire module
    audit_routing.py          PRESENT upstream, DIFFERENT FEATURE (see below)
    dashboard_data.py         query_realized_savings + 2 supporting symbols
    signals/__init__.py       detect_pii, force_local_for_pii
    cost.py                   get_savings_by_task_type + 2 baseline helpers
    commands/audit.py         the whole `audit` CLI command

The ``audit_routing.py`` case is the one that makes this a script rather than a
paragraph. Both repositories have a file at that path. They are unrelated:

    upstream    audit_routing_turn / _get_audit_log / reset_audit_log_for_tests
                -- an append-only log of routing turns
    downstream  run_audit / score_decision / sample_unaudited_decisions /
                _write_verdict -- a post-hoc misroute SCORER

A file-level copy overwrites one with the other. Same path, no merge conflict,
no import error, no failing test upstream -- the downstream feature simply
stops existing, and the only signal is that a test file somewhere downstream
now fails to import. That is a silent-deletion shape, and it is exactly what a
"copy src/ across" instruction produces when the two trees are not the
containment the instruction assumes.

WHAT THIS CHECKS
================

For every symbol defined in the downstream tree, is a symbol of that name
defined ANYWHERE in the upstream tree? Name-level, not signature-level,
deliberately:

  * a relocated symbol (``observability/summary.py`` upstream is ``summary.py``)
    is NOT a gap, and a path-sensitive check would report dozens of those as
    findings, which is how a check gets ignored;
  * a same-name-different-meaning symbol IS still reported by the file-level
    collision section below, which is the case that actually loses work.

Private helpers (``_fmt_usd_or_na``, ``_bold``) are reported separately from
public API, because a missing formatter is a cosmetic gap and a missing
``query_realized_savings`` is a missing feature, and lumping them together
makes the number meaningless.

EXIT CODES
==========

0  upstream is a superset -- Step 2's copy is safe to perform
1  gaps found -- port them upstream FIRST, then re-run

Usage:
    python scripts/check_downstream_superset.py [--downstream PATH] [--verbose]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

UPSTREAM_DEFAULT = Path(__file__).resolve().parent.parent / "src" / "llm_router"
DOWNSTREAM_DEFAULT = Path.home() / "Projects" / "llm-router" / "src" / "llm_router"

#: Modules deliberately not synced (36_DOWNSTREAM_SYNC_PLAN.md §1). A symbol
#: only present downstream inside one of these is out of scope, not a gap.
EXCLUDED = {
    "tools/agoragentic.py",
    "tenant_policy_sidecar.py",
    "admin_api.py",
    "commands/admin_api.py",
}

#: Downstream name -> the upstream name carrying the same capability.
#:
#: This is a RENAME RECORD, not a suppression list, and the difference is
#: enforced: the check verifies the upstream name actually exists and fails if
#: it does not. Mapping a downstream symbol to a name that is absent here turns
#: one gap into a louder gap rather than hiding it.
#:
#: Only for cases where porting under the same name would COLLIDE with an
#: existing upstream feature. Not for "I'd rather call it something else" —
#: gratuitous divergence makes every future sync harder, which is the cost this
#: whole exercise exists to avoid paying twice.
RENAMED: dict[str, tuple[str, str]] = {
    "cmd_audit": (
        "_misroute",
        "downstream exposes the misroute audit as a top-level `audit` command, "
        "which is precisely what collides with upstream's existing enterprise "
        "audit-log CLI (commands/audit.py::main). Ported upstream as the "
        "`misroute` SUBcommand of that same command — composing instead of "
        "displacing. Same capability, one level down in the command tree.",
    ),
}

#: Paths that exist in both trees with disjoint APIs *by design*, because the
#: same filename came to mean two different features. Recorded rather than
#: resolved: renaming either file now would break imports for no benefit, and
#: the sync must simply not treat these as the same file.
KNOWN_DIVERGENT_PATHS: dict[str, str] = {
    "audit_routing.py": (
        "upstream = live per-turn compliance log (audit_routing_turn); "
        "downstream = offline misroute scorer, ported upstream as "
        "misroute_audit.py. The sync must map downstream/audit_routing.py to "
        "upstream/misroute_audit.py, NOT to upstream/audit_routing.py."
    ),
    "commands/audit.py": (
        "upstream = enterprise audit-log CLI (verify/export/misroute); "
        "downstream = misroute CLI only. The sync must merge, not overwrite."
    ),
}


def _defined_symbols(root: Path) -> dict[str, list[str]]:
    """Every top-level function/class name in the tree -> the files defining it."""
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root))
        if rel in EXCLUDED:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.setdefault(node.name, []).append(rel)
    return found


def _reachable_names(root: Path, defining_file: str, name: str) -> int:
    """How many times ``name`` is referenced OUTSIDE the file that defines it.

    Zero means the symbol is an orphan in this tree: correct, tested, and
    reached by nothing. That distinction decides whether a downstream-only
    symbol is a real upstream GAP or just downstream dead code, and the two
    call for opposite actions -- port it, versus wire or delete it there.

    It is the same shape as the SUBSCRIPTION_LOCAL defect found upstream on the
    same day: a complete module with its own tests and no production caller.
    Measured downstream, `response_validation.py` is exactly that -- six public
    symbols, zero references anywhere in `src/`, one test file. Porting it
    upstream would move dead code between repositories and call it progress.

    References inside the defining file are excluded deliberately, so a helper
    called only by its own module's public entry point does not read as
    reachable on its own. The entry point is what has to be reachable.

    MENTION IS NOT USE
    ------------------

    Counted by parsing, not by ``text.count(name)``. The string version was
    written first and was wrong, in the direction that hides gaps: it scored
    ``force_local_for_pii`` as reachable on the strength of a docstring in a
    sibling module ("see llm_router.signals.force_local_for_pii") and its own
    ``__all__`` entry. Neither is a call. An orphan was about to be reported as
    live capability and ported on that basis.

    So: ``ast.Name`` / ``ast.Attribute`` references only. A name in a comment,
    a docstring, a string literal, or an ``__all__`` list does not count --
    ``__all__`` in particular is an export declaration, which is exactly what
    an orphaned public symbol still has.
    """
    count = 0
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if str(path.relative_to(root)) == defining_file:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name:
                count += 1
            elif isinstance(node, ast.Attribute) and node.attr == name:
                count += 1
            elif isinstance(node, ast.ImportFrom):
                # An import is a genuine reference to the symbol, but only if
                # something then uses it -- which the Name/Attribute walk above
                # already counts in the same file. Counting the import too
                # would score a re-export as a use.
                continue
    return count


def _file_level_collisions(upstream: Path, downstream: Path) -> list[tuple[str, set[str], set[str]]]:
    """Same path, disjoint public API -- the silent-overwrite case.

    Reported only when the two files share NO public symbol at all. Partial
    overlap is ordinary drift; zero overlap means the path means two different
    things in the two trees, and copying one over the other deletes a feature
    without any signal.
    """
    out: list[tuple[str, set[str], set[str]]] = []
    for dpath in sorted(downstream.rglob("*.py")):
        if "__pycache__" in dpath.parts:
            continue
        rel = dpath.relative_to(downstream)
        if str(rel) in EXCLUDED:
            continue
        upath = upstream / rel
        if not upath.exists():
            continue

        def public(p: Path) -> set[str]:
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                return set()
            return {
                n.name
                for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and not n.name.startswith("_")
            }

        dsyms, usyms = public(dpath), public(upath)
        if dsyms and usyms and not (dsyms & usyms):
            out.append((str(rel), usyms, dsyms))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, default=UPSTREAM_DEFAULT)
    parser.add_argument("--downstream", type=Path, default=DOWNSTREAM_DEFAULT)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.downstream.exists():
        print(
            f"SKIP: no downstream checkout at {args.downstream}. This check "
            f"compares two working trees and cannot run without both; it is a "
            f"local pre-sync gate, not a CI gate.",
            file=sys.stderr,
        )
        return 0

    upstream = _defined_symbols(args.upstream)
    downstream = _defined_symbols(args.downstream)

    if not upstream or not downstream:
        print(
            "FAIL: one of the trees yielded no symbols at all — the parser or a "
            "path is wrong, and a comparison against nothing would report a "
            "clean bill of health.",
            file=sys.stderr,
        )
        return 1

    # A recorded rename must point at a name that really exists upstream, or
    # the record is fiction and the gap is still open.
    broken_renames = [
        (dn_name, up_name, why)
        for dn_name, (up_name, why) in RENAMED.items()
        if up_name not in upstream
    ]
    if broken_renames:
        print("BROKEN RENAME RECORD — mapped to a name that does not exist upstream:")
        for dn_name, up_name, why in broken_renames:
            print(f"  {dn_name} -> {up_name}  (absent)")
            print(f"      recorded reason: {why}")
        print(
            "\nA rename record is a claim that the capability was ported under "
            "another name.\nIf that name is missing, the claim is false and the "
            "gap is still open."
        )
        return 1

    missing = {
        name: files
        for name, files in downstream.items()
        if name not in upstream and name not in RENAMED
    }
    public_missing = {n: f for n, f in missing.items() if not n.startswith("_")}
    private_missing = {n: f for n, f in missing.items() if n.startswith("_")}
    collisions = _file_level_collisions(args.upstream, args.downstream)

    print(f"upstream symbols:   {len(upstream)}")
    print(f"downstream symbols: {len(downstream)}")
    print(f"downstream-only:    {len(missing)}  ({len(public_missing)} public, "
          f"{len(private_missing)} private)")
    print(f"path collisions:    {len(collisions)}")
    print()

    unrecorded_collisions = [c for c in collisions if c[0] not in KNOWN_DIVERGENT_PATHS]

    if collisions:
        print("SAME PATH, DISJOINT API — a file copy here deletes a feature silently:")
        for rel, usyms, dsyms in collisions:
            known = KNOWN_DIVERGENT_PATHS.get(rel)
            marker = "recorded" if known else "UNRECORDED"
            print(f"  {rel}  [{marker}]")
            print(f"      upstream:   {', '.join(sorted(usyms))}")
            print(f"      downstream: {', '.join(sorted(dsyms))}")
            if known:
                print(f"      sync rule:  {known}")
        print()

    # A downstream-only symbol that nothing downstream calls is not an upstream
    # gap — it is downstream dead code, and porting it moves dead code between
    # repositories. Split on that before reporting anything as a gap.
    live: dict[str, list[str]] = {}
    orphaned: dict[str, list[str]] = {}
    for name, files in public_missing.items():
        target = live if _reachable_names(args.downstream, files[0], name) else orphaned
        target[name] = files

    if live:
        by_file: dict[str, list[str]] = {}
        for name, files in live.items():
            by_file.setdefault(files[0], []).append(name)
        print("GAPS — public, downstream-only, and REACHED by downstream code:")
        for f in sorted(by_file):
            print(f"  {f}")
            print(f"      {', '.join(sorted(by_file[f]))}")
        print()

    if orphaned:
        by_file_o: dict[str, list[str]] = {}
        for name, files in orphaned.items():
            by_file_o.setdefault(files[0], []).append(name)
        print("NOT gaps on their own — public, downstream-only, and referenced")
        print("NOWHERE OUTSIDE their own module.")
        print()
        print("Read this list carefully before acting on it. It contains two")
        print("different things, and the check cannot separate them without a call")
        print("graph:")
        print("  (a) genuine dead code — a whole module nothing reaches, e.g.")
        print("      response_validation.py: 6 public symbols, 0 references in src/,")
        print("      one test file. Porting it upstream moves dead code between")
        print("      repositories; wire or delete it downstream instead.")
        print("  (b) module-internal helpers of an entry point that IS live, e.g.")
        print("      score_decision is called by run_audit inside audit_routing.py.")
        print("      Those travel with their entry point and are NOT dead.")
        print("Rule of thumb: if the file appears in the GAPS list above, its")
        print("entries here are case (b).")
        for f in sorted(by_file_o):
            print(f"  {f}")
            print(f"      {', '.join(sorted(by_file_o[f]))}")
        print()

    if args.verbose and private_missing:
        print("private helpers, downstream-only (cosmetic unless they carry logic):")
        for name in sorted(private_missing):
            print(f"  {name:38} {private_missing[name][0]}")
        print()

    if unrecorded_collisions or live:
        print(
            "NOT A SUPERSET. Copying upstream src/ over downstream would drop the\n"
            "GAPS above. Port those upstream first — that is what 'after LLM Router is\n"
            "completely ready' has to mean before a copy is safe — then re-run.\n"
            "The orphan list is a separate, downstream decision and does not gate."
        )
        return 1

    if collisions:
        print(
            "SUPERSET OK, but the recorded path divergences above still apply:\n"
            "the sync must follow each one's stated rule rather than copying the\n"
            "file across. A recorded collision is a routing instruction for the\n"
            "sync, not a resolved problem."
        )

    print("SUPERSET OK: every downstream symbol has an upstream definition.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
