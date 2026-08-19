#!/usr/bin/env python3
"""Rebrand upstream into the downstream package. Deterministic and re-runnable.

WHY A SCRIPT AND NOT A COPY
===========================

The sync rewrites ~4,956 ``llm_router`` occurrences across 363 files. Done by hand
it is unreviewable, unrepeatable, and impossible to redo when upstream moves on
next month. Done as a script the diff is the script, the mapping is written
down, and the next sync is one command.

More importantly, a copy cannot express the two rules that matter, both
discovered by ``check_downstream_superset.py``:

  * ``audit_routing.py`` means DIFFERENT FEATURES in the two trees. Copying it
    across deletes one of them, silently — same path, no merge conflict, no
    import error, no failing test.
  * seven downstream-only public symbols are dead code downstream, and several
    downstream-only MODULES are alive. A wholesale replace deletes both
    indiscriminately.

SAFETY MODEL
============

Default is ``--dry-run``: prints what would change and writes nothing.
``--apply`` writes. Nothing is deleted, ever — files that exist downstream and
not upstream are LEFT ALONE and reported, because deciding a downstream module
is obsolete is a human call and this script cannot make it.

REWRITES
========

Order matters — the longest and most specific patterns first, or a shorter one
eats the prefix of a longer one and the result is subtly wrong in a way tests
do not always catch (``llm-routing`` becoming ``llm_router-router``).
``_check_rewrite_order`` asserts the ordering property directly rather than
trusting the list to stay sorted.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from pathlib import Path

UPSTREAM_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_SRC = UPSTREAM_ROOT / "src" / "llm_router"
DOWNSTREAM_ROOT_DEFAULT = Path.home() / "Projects" / "llm-router"

#: Ordered longest-first. See _check_rewrite_order.
REWRITES: list[tuple] = [
    ("llm-routing", "llm-routing"),      # distribution name
    ("LLM_ROUTER_", "LLM_ROUTER_"),            # env var prefix
    # The home directory, in the three forms it is actually written. NOT a bare
    # `.llm_router` rule: that also matches ATTRIBUTE ACCESS, and it did —
    # `_config.llm_router_claude_subscription` became
    # `_config.llm-router_claude_subscription`, which parses as a subtraction
    # and raised AttributeError at import. The bare form is too greedy to be
    # safe; the quoted and tilde forms are unambiguous.
    ('".llm-router"', '".llm-router"'),
    ("'.llm-router'", "'.llm-router'"),
    ("~/.llm-router", "~/.llm-router"),
    ("llm_router", "llm_router"),              # python package / module paths
    # `LLM Router` appears in two contexts that need OPPOSITE treatment, and a
    # single rule cannot serve both:
    #
    #   LLMRouterDashboard   a CamelCase IDENTIFIER -> LLMRouterDashboard
    #   "LLM Router routes…"  PROSE                  -> "LLM Router routes…"
    #
    # The prose rule alone produced `class LLM RouterDashboard(App[None]):` in
    # three files — a syntax error, caught by the parse guard rather than by a
    # human reading 359 files. Identifier context is "followed by an uppercase
    # letter", handled by the regex rule below, which must run FIRST.
    (r"LLM Router(?=[A-Z])", "LLMRouter", "regex"),
    ("LLM Router", "LLM Router"),              # prose brand
    ("LLM_ROUTER", "LLM_ROUTER"),              # any remaining shout-case
]

#: Not synced. From 36_DOWNSTREAM_SYNC_PLAN.md §1 (maintainer decision) plus the
#: enterprise tree, which the downstream README sells separately rather than
#: ships.
EXCLUDED_PATHS = {
    "tools/agoragentic.py",
    "tenant_policy_sidecar.py",
    "admin_api.py",
    "commands/admin_api.py",
}
EXCLUDED_DIRS = {"enterprise", "invoice_reconciliation", "__pycache__"}

#: Import targets that do not exist downstream, because the module they name is
#: excluded above or lives outside the synced trees. Any test importing one of
#: these is skipped — a test for a capability that was deliberately not shipped
#: is not a test failure, it is a test that should not have travelled.
#:
#: Detected by parsing imports rather than by listing filenames, so a new test
#: for an excluded module is skipped automatically instead of arriving broken.
#: 51 of the 75 collection errors on the first tests sync were exactly this:
#: 27 enterprise, 20 admin_api, 3 invoice_reconciliation, 1
#: tenant_policy_sidecar.
UNAVAILABLE_IMPORT_ROOTS = {
    "llm_router.enterprise",
    "llm_router.admin_api",
    "llm_router.commands.admin_api",
    "llm_router.invoice_reconciliation",
    "llm_router.tenant_policy_sidecar",
    "llm_router.tools.agoragentic",
    # Repo-root dev helpers that live outside src/ and tests/, so the sync never
    # carries them. Their tests are upstream-development tooling, not downstream
    # product surface.
    "bench",
    "soak",
    "backfill_sidecars",
}

#: Upstream path -> downstream path, where the names legitimately differ.
#: Every entry here is a collision `check_downstream_superset.py` reports: the
#: same path already means something else downstream, so copying onto it would
#: destroy a feature.
#: Keyed by TREE. A single flat map was wrong the moment the scripts tree was
#: added: `summary.py -> observability/summary.py` is a src/ relocation and
#: means nothing under scripts/, where a file of that name would be silently
#: moved into a directory that does not exist there.
PATH_MAP_BY_TREE: dict[str, dict[str, str]] = {}

PATH_MAP_BY_TREE["src"] = {
    # NOTE ON audit_routing.py — the collision that started this script.
    #
    # The first version mapped upstream misroute_audit.py ONTO downstream
    # audit_routing.py, to preserve downstream's existing name. That was wrong,
    # and the tree it produced could not import: upstream router.py does
    # `from llm_router.audit_routing import audit_routing_turn`, and with the
    # scorer occupying that path the compliance log had nowhere to land.
    #
    # Upstream now has BOTH modules under distinct names, so the right move is
    # for downstream to mirror that. Downstream's audit_routing.py (the scorer)
    # is replaced by upstream's audit_routing.py (the compliance log), and the
    # scorer arrives beside it as misroute_audit.py. No capability is lost —
    # the two features simply stop sharing a name, which is what made the
    # collision possible in the first place. The rename is a breaking change
    # for anyone importing llm_router.audit_routing expecting the scorer, and
    # belongs in the 13.0.0 release note for that reason.
    #
    # Relocations, verified by symbol overlap rather than by name:
    # upstream summary.py shares 7 symbols with downstream
    # observability/summary.py, and surface_status.py shares 22 with
    # observability/surface_status.py. Without these the sync writes a SECOND
    # copy at the top level and downstream ends up with two of each — one
    # imported by existing code, one dead and drifting.
    "summary.py": "observability/summary.py",
    "surface_status.py": "observability/surface_status.py",
    # Upstream `observability.py` is a MODULE with 14 symbols; downstream
    # `observability/` is a PACKAGE whose __init__ shares NONE of them. Landing
    # the module at `observability.py` next to the package makes the package win
    # the import and the module's contents unreachable — code present, silently
    # never executed. Placed inside the package instead; re-exporting from
    # __init__ is a public-API decision left to the human doing the merge.
    "observability.py": "observability/core.py",
}

PATH_MAP_BY_TREE["tests"] = {}

PATH_MAP_BY_TREE["scripts"] = {
    # Upstream `scripts/release.py` collides with downstream's `scripts/release/`
    # PACKAGE — caught by the structural check on the very first scripts run,
    # against a tree that check was not written for. Renamed rather than
    # skipped: the upstream release helper is what the synced tests import.
    "release.py": "release_helper.py",
}

#: Set per-run from PATH_MAP_BY_TREE. Module-level so `rewrite()` and
#: `_import_rewrites()` can see it without threading it through every call.
PATH_MAP: dict[str, str] = PATH_MAP_BY_TREE["src"]

#: Downstream paths this script must never write, because the downstream file is
#: a DIFFERENT feature that happens to share a name, or is a merge rather than a
#: replace. Listed explicitly so the reason survives.
DO_NOT_OVERWRITE: dict[str, str] = {
    # Empty, deliberately, and worth explaining rather than deleting.
    #
    # Both former entries turned out to be cases where preserving the
    # downstream name was the WORSE option:
    #
    #   audit_routing.py    — see the note in PATH_MAP. Protecting it left
    #                         upstream's compliance log with nowhere to go and
    #                         the package could not import at all.
    #   commands/audit.py   — upstream's version is a strict SUPERSET
    #                         (verify/export/misroute vs misroute alone), so
    #                         "needs a merge decision" was overcautious; taking
    #                         the superset loses nothing.
    #
    # Kept as a mechanism because the next sync may well need it, and because
    # the reasoning above is the useful part.
}


def _check_rewrite_order() -> list[str]:
    """Each pattern must not be a substring of a LATER pattern's source.

    If it is, the earlier rewrite fires inside the later one's text and the
    later rule never matches what it was written for. Checked rather than
    assumed, because the failure is silent: the output still looks like code.
    """
    problems = []
    plain = [r for r in REWRITES if len(r) == 2]
    for i, (src, _) in enumerate(plain):
        for later_src, _ in plain[i + 1 :]:
            if src in later_src:
                problems.append(
                    f"{src!r} precedes {later_src!r} but is a substring of it — "
                    f"the second rule will never match its intended text"
                )
    return problems


def _imports_unavailable_module(text: str) -> str | None:
    """The import root that makes this file unshippable, or None.

    Parsed, not grepped: a module named in a docstring or a comment is not an
    import, and skipping a test on the strength of a mention would drop tests
    that are perfectly fine.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        for name in names:
            for root in UNAVAILABLE_IMPORT_ROOTS:
                if name == root or name.startswith(root + "."):
                    return root
    return None


def _import_rewrites() -> list[tuple[str, str]]:
    """Import-path rewrites implied by PATH_MAP, derived rather than restated.

    Moving a file is only half a relocation. ``summary.py`` ->
    ``observability/summary.py`` also means every ``from llm_router.summary
    import …`` has to become ``from llm_router.observability.summary import …``,
    and the first version of this script did the move without the imports — 4
    test modules failed to collect with ``No module named
    'llm_router.summary'``.

    Derived from PATH_MAP so the two can never disagree. Restating them as a
    second hand-written list is how a later PATH_MAP entry gets a file move with
    no matching import fix.

    Each pattern ends in a negative lookahead so a package rename does not eat
    its own children: ``llm_router.observability`` -> ``…observability.core``
    must NOT turn ``llm_router.observability.summary`` into
    ``llm_router.observability.core.summary``.
    """
    out: list[tuple[str, str]] = []
    for src_rel, dst_rel in PATH_MAP.items():
        src_mod = "llm_router." + src_rel.removesuffix(".py").replace("/", ".")
        dst_mod = "llm_router." + dst_rel.removesuffix(".py").replace("/", ".")
        if src_mod == dst_mod:
            continue
        out.append((re.escape(src_mod) + r"(?![.\w])", dst_mod))

        # A relocated module is also imported as a NAME from its old parent:
        #
        #     from llm_router import surface_status        <- this form
        #     from llm_router.surface_status import …      <- the form above
        #
        # The path rule alone misses the first, and three test modules failed to
        # collect with `cannot import name 'surface_status' from 'llm_router'`.
        # Same relocation, two syntaxes, and only one of them looks like a path.
        src_parent, _, src_leaf = src_mod.rpartition(".")
        dst_parent, _, dst_leaf = dst_mod.rpartition(".")
        if src_leaf == dst_leaf and src_parent != dst_parent:
            out.append(
                (
                    rf"from {re.escape(src_parent)} import (?=.*\b{re.escape(src_leaf)}\b)",
                    f"from {dst_parent} import ",
                )
            )
    # Longest source first, so a shorter module path does not match inside a
    # longer one before the longer rule gets its turn.
    out.sort(key=lambda pair: -len(pair[0]))
    return out


def rewrite(text: str) -> str:
    for rule in REWRITES:
        if len(rule) == 3:
            src, dst, _ = rule
            text = re.sub(src, dst, text)
        else:
            src, dst = rule
            text = text.replace(src, dst)
    # ONE pass over all import rules, via alternation.
    #
    # Applying them sequentially let each rule rewrite the previous rule's
    # OUTPUT. `from llm_router import surface_status` correctly became
    # `from llm_router.observability import surface_status`, and then the
    # `observability -> observability.core` rule fired on that result and
    # produced `from llm_router.observability.core import surface_status`,
    # which does not exist. Every ordering fixed one pair and broke another,
    # because the rules genuinely overlap.
    #
    # A single alternation pass cannot do that: each position in the text is
    # matched at most once, so no rule ever sees another's output.
    rules = _import_rewrites()
    if rules:
        # NAMED groups, so the dispatcher knows which alternative fired.
        #
        # The first version re-matched `match.group(0)` against each pattern to
        # find the winner. That silently never worked for any rule containing a
        # LOOKAHEAD: the lookahead needs the text that follows the match, and
        # `group(0)` does not include it, so `re.fullmatch` always failed and
        # every such rule fell through to "return the text unchanged". The
        # rewrite looked like it ran and did nothing.
        combined = "|".join(
            f"(?P<r{index}>{pattern})" for index, (pattern, _) in enumerate(rules)
        )
        replacements = [replacement for _, replacement in rules]

        def _pick(match: re.Match) -> str:
            for index in range(len(rules)):
                if match.group(f"r{index}") is not None:
                    return replacements[index]
            return match.group(0)  # pragma: no cover - alternation always sets one

        text = re.sub(combined, _pick, text)
    return text


def _still_parses(original: str, rewritten: str, rel: str) -> str | None:
    """The rewritten Python must still parse. Returns an error string or None.

    This is the guard that would have caught the `.llm_router` rule immediately.
    A bare `.llm_router` -> `.llm-router` replacement hit attribute access as well
    as paths, turning `_config.llm_router_claude_subscription` into
    `_config.llm-router_claude_subscription` — which is not a syntax error (it
    parses as a subtraction) but fails at runtime with a bewildering
    `'RouterConfig' object has no attribute 'llm'`.

    Text substitution across 359 files WILL eventually produce something that
    is not the code it looks like. Checking that the input parsed and the
    output still does is cheap, catches the whole class, and localises the
    failure to the file and rule that caused it instead of to an import
    traceback three modules away.
    """
    try:
        ast.parse(original)
    except SyntaxError:
        return None  # upstream file was already unparseable; not ours to fix
    try:
        ast.parse(rewritten)
    except SyntaxError as exc:
        return f"  {rel}: rewritten source no longer parses — {exc}"
    return None


def _iter_upstream_files():
    for path in sorted(UPSTREAM_SRC.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        rel = str(path.relative_to(UPSTREAM_SRC))
        if rel in EXCLUDED_PATHS:
            continue
        if path.suffix in {".pyc", ".pyo", ".db", ".sqlite3"}:
            continue
        yield path, rel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downstream", type=Path, default=DOWNSTREAM_ROOT_DEFAULT)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--tree",
        choices=("src", "tests", "scripts"),
        default="src",
        help=(
            "which tree to sync. One at a time, deliberately: the three fail "
            "differently, and running them together makes it impossible to tell "
            "a code problem from a test problem from a tooling problem. "
            "`scripts` carries the CI guards — the half of a sync people forget, "
            "and the half that keeps the ported fixes from silently regressing."
        ),
    )
    parser.add_argument(
        "--tests", action="store_true", help="deprecated alias for --tree tests"
    )
    args = parser.parse_args()
    if args.tests:
        args.tree = "tests"

    global UPSTREAM_SRC
    UPSTREAM_SRC = {  # noqa: F824
        "src": UPSTREAM_ROOT / "src" / "llm_router",
        "tests": UPSTREAM_ROOT / "tests",
        "scripts": UPSTREAM_ROOT / "scripts",
    }[args.tree]

    global PATH_MAP
    PATH_MAP = PATH_MAP_BY_TREE[args.tree]

    order_problems = _check_rewrite_order()
    if order_problems:
        print("REWRITE ORDER BROKEN:", file=sys.stderr)
        for p in order_problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    dst_pkg = {
        "src": args.downstream / "src" / "llm_router",
        "tests": args.downstream / "tests",
        "scripts": args.downstream / "scripts",
    }[args.tree]
    if not dst_pkg.exists():
        print(f"no downstream target at {dst_pkg}", file=sys.stderr)
        return 1

    # A file landing where a directory already lives (or the reverse) produces a
    # tree Python cannot import unambiguously — the package shadows the module
    # and its contents become unreachable code that still passes every syntax
    # check. Detected up front and refused, because the fix is a path decision
    # a human has to make, not something to guess mid-write.
    structural: list[str] = []
    for _, rel in _iter_upstream_files():
        target = dst_pkg / rewrite(PATH_MAP.get(rel, rel))
        # `foo.py` and `foo/` coexist happily ON DISK — the collision is at
        # IMPORT time, where the package wins and the module is unreachable. So
        # the test is against the suffix-stripped path, not the path itself.
        # The first version of this check compared `target.is_dir()` and found
        # nothing, because `observability.py` is not the directory
        # `observability` and never will be.
        if target.suffix == ".py" and target.with_suffix("").is_dir():
            structural.append(
                f"  {rel} -> {target.relative_to(dst_pkg)} — a PACKAGE named "
                f"{target.stem!r} already exists downstream. Both can sit on "
                f"disk, but the package wins the import and this module's "
                f"contents become unreachable code that still passes every "
                f"syntax check. Add a PATH_MAP entry."
            )
        elif target.parent.exists() and target.parent.is_file():
            structural.append(
                f"  {rel} -> {target.relative_to(dst_pkg)} — its parent is a FILE "
                f"downstream, so the directory cannot be created."
            )
    if structural:
        print("STRUCTURAL COLLISIONS — refusing to write:\n", file=sys.stderr)
        print("\n".join(structural), file=sys.stderr)
        return 1

    written = skipped_protected = unchanged = 0
    parse_failures: list[str] = []
    skipped_unavailable: list[str] = []
    new_files: list[str] = []
    binary: list[str] = []

    # TWO PASSES, and the separation is the whole point.
    #
    # The first version validated and wrote in ONE loop, so a parse failure on
    # file 300 printed "refusing to write" after 299 files had already been
    # written. A refusal that has already written is not a refusal, and it left
    # the downstream tree in a half-synced state that looked like a clean one.
    #
    # Pass 1 computes and validates everything, touching nothing. Pass 2 writes,
    # and only runs if pass 1 was completely clean.
    planned: list[tuple[Path, str | None, Path]] = []  # (target, content, source)

    for src_path, rel in _iter_upstream_files():
        target_rel = PATH_MAP.get(rel, rel)
        if target_rel in DO_NOT_OVERWRITE and rel not in PATH_MAP:
            skipped_protected += 1
            if args.verbose:
                print(f"  PROTECTED {target_rel}: {DO_NOT_OVERWRITE[target_rel]}")
            continue

        target = dst_pkg / rewrite(target_rel)
        try:
            content = src_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            binary.append(rel)
            planned.append((target, None, src_path))
            written += 1
            continue

        # TESTS ONLY. A source module that imports an excluded package almost
        # always guards it (`try: from llm_router.enterprise.audit import …  except
        # ImportError:`) precisely because enterprise/ is not in public
        # distributions — so it ships fine and degrades gracefully. Applying
        # this rule to src/ dropped 19 working modules on its first run.
        # A TEST has no such fallback: it imports the thing it exists to
        # exercise, and without it the module cannot even be collected.
        unavailable = (
            _imports_unavailable_module(content)
            if args.tree == "tests" and src_path.suffix == ".py"
            else None
        )
        if unavailable:
            skipped_unavailable.append(f"{rel} (imports {unavailable})")
            continue

        new_content = rewrite(content)
        if target.suffix == ".py":
            problem = _still_parses(content, new_content, rel)
            if problem:
                parse_failures.append(problem)
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        if existing == new_content:
            unchanged += 1
            continue
        if existing is None:
            new_files.append(str(target.relative_to(dst_pkg)))
        planned.append((target, new_content, src_path))
        written += 1

    # Downstream files with no upstream counterpart. NEVER deleted -- several
    # are live downstream-only modules, and deciding one is obsolete is a human
    # call this script has no basis to make.
    upstream_targets = {
        rewrite(PATH_MAP.get(rel, rel)) for _, rel in _iter_upstream_files()
    }
    downstream_only = sorted(
        str(p.relative_to(dst_pkg))
        for p in dst_pkg.rglob("*.py")
        if "__pycache__" not in p.parts
        and str(p.relative_to(dst_pkg)) not in upstream_targets
    )

    if parse_failures:
        print("REWRITE PRODUCED UNPARSEABLE PYTHON:\n", file=sys.stderr)
        print("\n".join(parse_failures), file=sys.stderr)
        print(
            "\nA rewrite rule is matching more than it should. Narrow the rule; "
            "do not exclude the file.",
            file=sys.stderr,
        )
        print(
            f"NOTHING WAS WRITTEN — {len(planned)} planned changes discarded.",
            file=sys.stderr,
        )
        return 1

    # Pass 2. Only reached when pass 1 found no problem at all.
    if args.apply:
        for target, content, src_path in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                shutil.copy2(src_path, target)
            else:
                target.write_text(content, encoding="utf-8")

    mode = "APPLIED" if args.apply else "DRY RUN — nothing written"
    print(f"=== sync_downstream: {mode} ===")
    print(f"  files written/changed : {written}")
    print(f"  already identical     : {unchanged}")
    print(f"  protected (not copied): {skipped_protected}")
    print(f"  skipped, import unavailable downstream: {len(skipped_unavailable)}")
    print(f"  new downstream files  : {len(new_files)}")
    print(f"  binary copied verbatim: {len(binary)}")
    print(f"  downstream-only, LEFT ALONE: {len(downstream_only)}")

    if args.verbose:
        for group, items in (
            ("NEW", new_files),
            ("DOWNSTREAM-ONLY (kept)", downstream_only),
            ("BINARY", binary),
        ):
            if items:
                print(f"\n{group}:")
                for i in items[:40]:
                    print(f"  {i}")
                if len(items) > 40:
                    print(f"  … {len(items) - 40} more")

    if not args.apply:
        print("\nRe-run with --apply to write. Nothing is ever deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
