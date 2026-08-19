#!/usr/bin/env python3
"""Derive the test files excluded from the G-F mutation campaign.

Protocol doc 20, AMENDMENT 1 (owner-approved 2026-08-13). Read that amendment before
changing anything here; the exclusion is the single most challengeable step in the
whole qualification, so it is DERIVED BY RULE and never hand-listed.

WHY EXCLUDE ANYTHING AT ALL
---------------------------
A mutant IS a source edit. A test that inspects source TEXT — by walking the tree with
`ast`/`rglob`, or by shelling out to a lint that does — can fail simply because the text
changed. mutmut records any suite failure as KILLED. So a text-scanning check reports a
mutant "killed" when nothing about the program's BEHAVIOUR was detected.

That is not a slow test. It is a wrong measurement, and it is wrong in the direction that
flatters the result — the same shape as the frozen sample's bogus 1.00 that this whole
protocol exists to replace.

It is worse than one wrong kill per mutant. mutmut's working copy contains ALL ~2436
mutant variants at once, so a tree-scanning check can be tripped by the presence of OTHER
mutants and mark a mutant killed whose own change was entirely irrelevant.

MEASURED, NOT ARGUED
--------------------
  * working copy: 14MB -> 460MB, 442k -> 23.4M AST nodes (53x)
  * a source walk: 0.3s in the real tree, 27.7s in the working copy
  * scripts/lint_savings_sign.py: instant in the real tree, >10min in the working copy
  * 3 of 4 tests in test_gate13_mutmut_config_intact.py failed inside the working copy
    BY CONSTRUCTION (setup.cfg there holds the swapped-in G-F scope). Those failures
    would have marked every mutant they covered as killed.

WHICH DIRECTION DOES THIS MOVE THE SCORE?
-----------------------------------------
Removing tests can normally only LOWER a mutation score — fewer tests, fewer kills. So
this exclusion is conservative by default. The one exception is precisely the spurious
textual kills described above, which it removes. Both effects push the reported number
DOWN or leave it unchanged. This amendment cannot inflate the result, and that asymmetry
is the reason it is defensible.

WHAT THIS IS NOT
----------------
It is NOT "select the tests that should own this behaviour". That is the B8 error — a
behaviour was recorded as uncovered because the nominated subset missed it while
tests/test_t2_m1_budget_key.py had covered it all along. Nothing here names an owner for
any behaviour. The rule is mechanical and about the test's METHOD, not its subject: does
it read source text? Every behavioural test in the repo is still in the run.

THE RULE
--------
A test file is excluded iff either holds:

  A. it traverses Python source files (`rglob`/`glob` over a `*.py` pattern), or
  B. it invokes one of the repo's source-scanning gate scripts as a subprocess.

Both are about the test's METHOD, never its subject. Neither asks what a test is "for".

`tests/test_gate13_mutmut_config_intact.py` is deliberately NOT excluded: it was made
context-aware instead, so it asserts the correct scope in whichever tree it runs. A skip
would have left the working copy — where a wrong scope does the most damage — unchecked.

Run with --check to verify config/mutmut_gf.cfg still matches what this derives;
tests/test_gf_exclusions_derived.py calls that so drift fails the suite rather than
silently widening the exclusion.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import io
import json
import re
import sys
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
GF_CFG = REPO / "config" / "mutmut_gf.cfg"
SEALED = REPO / ".llm-router" / "zero-tolerance-audit" / "gf"

#: Rule A — traverses PYTHON SOURCE FILES.
#:
#: The discriminator is the glob pattern, not the receiver's name and not the presence
#: of "src"/"llm_router" in the file. Two looser rules were tried first and both were WRONG,
#: in the same both-directions way as the CHZ-SS-01 lint earlier in this audit:
#:
#:   "any traversal + the word llm_router"  -> 20 files, sweeping in sandbox tests that walk
#:                                        tmp_path.rglob("*") and settings.json*.bak
#:   "constructs a src/llm_router path"     -> 110+ files, because nearly every test builds
#:                                        such a path for a sys.path insert
#:
#: A test that walks `*.py` is reading code text. A sandbox test walks `*`, `*.toml`,
#: `*.bak`. That distinction is mechanical and it is exact here: all 9 current matches
#: were checked receiver by receiver and every one resolves into the package (via
#: `parents[N]/"src"`, or a module's own `__file__`). Zero false positives, and the
#: three files the loose rules would have wrongly excluded are back in the run.
_PY_TRAVERSAL = re.compile(r"""(?:rglob|glob)\(\s*["']\*\*?/?\*?\.py["']\s*\)""")

#: Rule A2 — reads a SPECIFIC source file and asserts on its TEXT.
#:
#: Added 2026-08-13 after `test_every_success_path_calls_shared_finalizer` failed in the
#: working copy with "found 9854" where it expects 5. It does:
#:
#:     src = (ROOT / "src" / "llm_router" / "router.py").read_text()
#:     call_sites = len(re.findall(r"await _finalize_successful_route\(", src))
#:
#: No glob, so Rule A never saw it. Rule A was an operationalisation of "inspects source
#: text" that only caught the tree-walking half; this is the other half.
#:
#: WHY ALL 22 MATCHES ARE EXCLUDED AND NOT JUST THE ONE THAT FAILED: the others pass on
#: the unmutated tree today. That is not safety, it is luck about which counts happen to
#: be robust. Every one of them asserts on text that a mutant edits by definition, so any
#: of them can flip the moment a particular mutant is active — and mutmut records that
#: flip as KILLED, a kill that detected no behaviour. Excluding only the one observed to
#: break would leave the same defect in twenty-one places and would be exactly the
#: one-at-a-time checklist error this audit keeps recording.
#: The path construction and the read must be on the SAME LINE. A first attempt matched
#: them anywhere in the same function -- `.read_text(|open(` plus the strings "src" and
#: "llm_router" -- and swept in whole files like test_welcome_cli.py, because nearly every
#: test mentions those strings somewhere and `open(` is ubiquitous. Third time a rule on
#: this project has been too loose on first writing; caught the same way each time, by
#: printing the matches instead of trusting the regex.
#: ...and it must name one of the EIGHT MUTATED MODULES. That is the whole discriminator.
#: mutmut rewrites only the files in `only_mutate`; every other file under src/llm_router is
#: copied verbatim, so reading it yields identical text in both trees and can never
#: produce a spurious kill.
#:
#: Requiring only "reads something under src/llm_router" produced two false positives that
#: made this obvious: test_cluster5 reads `src/llm_router/rules/llm_router.md` (markdown, never
#: mutated) and test_welcome_cli reads `src/llm_router/cli.py` (a .py file, but not in scope).
#: Excluding those would have removed 13 real behavioural tests to protect against a risk
#: that does not exist for them.
def _mutated_module_names() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(GF_CFG)
    return [
        Path(line.strip()).name
        for line in parser.get("mutmut", "only_mutate", fallback="").splitlines()
        if line.strip()
    ]


def _reads_mutated_source() -> re.Pattern[str]:
    names = "|".join(re.escape(n) for n in _mutated_module_names())
    return re.compile(rf"""["'](?:{names})["'][^\n]{{0,80}}?\.read_text\(""")
_ASSERTS_ON_TEXT = re.compile(
    r"\.count\(|re\.findall|re\.search|re\.finditer|\bin src\b|\.splitlines\("
)

#: Rule B — the repo's source-scanning gate scripts, invoked as subprocesses.
_GATE_SCRIPTS = (
    "lint_savings_sign",
    "lint_fail_open",
    "lint_tool_surface",
    "lint_capability_claims",
    "quality_gate_test_hygiene",
    "validate_claim_evidence",
    "verify_criteria_hashes",
)


_READS_MUTATED = _reads_mutated_source()


def _code_only(segment: str) -> str:
    """`segment` with comments and docstrings blanked out.

    Rule B is meant to catch a test that INVOKES a gate script as a subprocess. It was a
    bare substring match over the raw text, so a test that merely NAMED one in prose was
    excluded too — and an excluded test kills nothing, so the score was measured over a
    smaller suite than intended.

    Measured cost of that: 8 test node ids, of which 7 were wrong. `test_failopen.py`'s
    MODULE DOCSTRING says "The lint (`scripts/lint_fail_open.py`) pins that call sites
    exist; this pins that the mechanism they call actually records…" — prose explaining a
    division of labour — and that one sentence deselected all eight tests in the file,
    including `test_unreadable_store_is_unknown_not_zero`, which is the exact RED2-02
    shape this campaign exists to protect.

    Note a path-based rule would NOT have fixed it: that docstring names the full
    `scripts/lint_fail_open.py` path. The real distinction is not how the name is spelled
    but WHERE it appears — invocations live in executable code, mentions live in
    documentation. So strip documentation and match what is left.

    Returns the segment with the same line count, so any line numbers derived from it
    stay valid.
    """
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(segment).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A fragment that will not tokenise on its own (e.g. an indented method body
        # sliced out of a class). Fall back to the raw text: over-excluding is the
        # conservative direction for a fragment we cannot read properly.
        return segment

    lines = segment.splitlines(keepends=True)
    out = list(lines)
    prev_toktype = tokenize.INDENT
    for tok in toks:
        if tok.type == tokenize.COMMENT or (
            tok.type == tokenize.STRING
            and prev_toktype in (tokenize.INDENT, tokenize.NEWLINE, tokenize.NL)
        ):
            # A STRING whose previous significant token ended a statement is a bare
            # expression-statement string: a docstring, or prose pinned in place.
            for ln in range(tok.start[0], tok.end[0] + 1):
                if 1 <= ln <= len(out):
                    out[ln - 1] = "\n" if out[ln - 1].endswith("\n") else ""
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev_toktype = tok.type
    return "".join(out)


#: Rule A2, second shape. `_READS_MUTATED` requires the quoted module name within 80
#: characters of `.read_text(`, which misses the loop form:
#:
#:      for name in ("cost.py", "router.py", "execution_ledger.py"):
#:          total += (src / name).read_text().count("failopen.record(")
#:
#: The names are two lines above the read and the path is built from a loop variable, so
#: adjacency never matches — yet this scans three MUTATED modules and asserts on their
#: text, which is exactly what rule A2 exists to catch. Found when fixing rule B removed
#: the file-level exclusion that had been hiding it: one over-exclusion was masking one
#: under-exclusion, and neither was visible while the whole file was being dropped.
_NAMES_A_MUTATED_MODULE = re.compile(
    "|".join(rf"""["']{re.escape(n)}["']""" for n in _mutated_module_names())
)
_ANY_READ_TEXT = re.compile(r"\.read_text\(")


def _reasons_for(segment: str) -> list[str]:
    """Reasons this segment must be deselected, judged on CODE ONLY.

    Every rule reads `_code_only(segment)`. Prose is documentation, and documentation
    that describes a scan is not a scan — the whole point of #38.
    """
    code = _code_only(segment)
    reasons: list[str] = []
    if _PY_TRAVERSAL.search(code):
        reasons.append("A: walks Python source files")
    if _ASSERTS_ON_TEXT.search(code) and (
        _READS_MUTATED.search(code)
        or (_NAMES_A_MUTATED_MODULE.search(code) and _ANY_READ_TEXT.search(code))
    ):
        reasons.append("A2: reads a source file and asserts on its text")
    hit = sorted({g for g in _GATE_SCRIPTS if g in code})
    if hit:
        reasons.append(f"B: invokes gate script(s) {', '.join(hit)}")
    return reasons


def classify(path: Path) -> dict[str, list[str]]:
    """Map pytest nodeid -> reasons, for the SMALLEST unit that can be excluded.

    Deselection is per test FUNCTION, not per file. `tests/test_tool_surface.py` both
    scans `*.py` and carries the behavioural tests for `tool_surface.py` — 287 of G-F's
    mutants. Dropping the whole file would discard real behavioural coverage and depress
    the score for a reason that has nothing to do with the code under test. An exclusion
    that removes genuine coverage is no more honest than one that adds fake kills; it is
    just wrong in the flattering-to-nobody direction.

    If the scanning happens at module level or inside a shared helper, the whole file is
    excluded — every test in it inherits the behaviour, and pretending otherwise would be
    a guess about which ones. That case is reported distinctly so it can be challenged.
    """
    rel = str(path.relative_to(REPO))
    text = path.read_text(encoding="utf-8", errors="replace")

    if not _reasons_for(text):
        return {}

    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover — a broken test file is a different problem
        return {rel: ["file: unparseable, excluded conservatively"]}

    lines = text.splitlines()
    out: dict[str, list[str]] = {}
    covered: list[tuple[int, int]] = []

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.ClassDef):
            members = [
                m for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
        else:
            members = [node]

        for m in members:
            seg = "\n".join(lines[m.lineno - 1 : (m.end_lineno or m.lineno)])
            reasons = _reasons_for(seg)
            if not reasons:
                continue
            covered.append((m.lineno, m.end_lineno or m.lineno))
            if not m.name.startswith("test"):
                # A shared helper: attribute to the file, not to a guessed set of tests.
                return {rel: [f"file: helper {m.name}() scans source — {'; '.join(reasons)}"]}
            nodeid = (
                f"{rel}::{node.name}::{m.name}"
                if isinstance(node, ast.ClassDef)
                else f"{rel}::{m.name}"
            )
            out[nodeid] = reasons

    # Module-level means OUTSIDE EVERY top-level def/class — not merely outside the
    # functions that tripped the rule.
    #
    # The first version used the latter, concatenating all lines it had not already
    # attributed and re-running the rule over them. That invented matches across
    # unrelated functions: in test_tool_surface.py a `.read_text()` on line 172 joined a
    # `.splitlines(` on line 263, ninety lines and several functions away, and the file
    # was reported as a module-level scan. It would have deselected the whole file —
    # every behavioural test for tool_surface.py, 287 of G-F's mutants — and
    # `test_behavioural_tests_for_the_biggest_module_survive_the_exclusion` is what
    # caught it. Concatenating disjoint regions and pattern-matching the result is a
    # measurement of something that does not exist.
    top_level_spans = [
        (n.lineno, n.end_lineno or n.lineno)
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    residual = "\n".join(
        line
        for i, line in enumerate(lines, start=1)
        if not any(lo <= i <= hi for lo, hi in top_level_spans)
    )
    if _reasons_for(residual):
        return {rel: [f"file: module-level scan — {'; '.join(_reasons_for(residual))}"]}

    return out


def derive() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        # Context-aware by design; see module docstring.
        if path.name == "test_gate13_mutmut_config_intact.py":
            continue
        out.update(classify(path))
    return out


def cfg_ignores() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(GF_CFG)
    raw = parser.get("mutmut", "pytest_add_cli_args", fallback="")
    return sorted(
        line.strip().removeprefix("--deselect=")
        for line in raw.splitlines()
        if line.strip().startswith("--deselect=")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify the config matches")
    ap.add_argument("--write-evidence", action="store_true")
    ns = ap.parse_args()

    derived = derive()
    names = sorted(derived)

    if ns.check:
        configured = cfg_ignores()
        if configured != names:
            print("config/mutmut_gf.cfg does not match the derived exclusion set:")
            for extra in sorted(set(configured) - set(names)):
                print(f"  IN CONFIG, NOT DERIVED (widens the exclusion): {extra}")
            for miss in sorted(set(names) - set(configured)):
                print(f"  DERIVED, NOT IN CONFIG: {miss}")
            return 1
        print(f"config matches the derived set ({len(names)} files)")
        return 0

    for name in names:
        print(f"{name}\n    {'; '.join(derived[name])}")
    whole_files = sum(1 for n in names if "::" not in n)
    print(
        f"\n{len(names)} deselections: {len(names) - whole_files} individual test "
        f"functions + {whole_files} whole files (module-level or helper scans), "
        f"across {len(list(TESTS.rglob('test_*.py')))} test files"
    )

    print("\n--- paste into config/mutmut_gf.cfg under [mutmut] ---")
    print("pytest_add_cli_args=" + "\n                   ".join(f"--deselect={n}" for n in names))

    if ns.write_evidence:
        SEALED.mkdir(parents=True, exist_ok=True)
        (SEALED / "excluded_tests.json").write_text(
            json.dumps({"rule_version": 1, "excluded": derived}, indent=2) + "\n"
        )
        print(f"\nwrote {SEALED / 'excluded_tests.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
