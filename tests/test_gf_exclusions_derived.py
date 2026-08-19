"""The G-F exclusion list must stay DERIVED, never hand-edited.

Protocol doc 20, AMENDMENT 1. Excluding tests from a mutation run is the single most
challengeable step in the whole G-F qualification: a wide enough exclusion can make any
score look like anything. The defence is that the list is produced by a mechanical rule
about a test's METHOD (does it read source text?) rather than chosen by hand — and that
anyone can re-derive it and get the same list.

This test is what makes that claim checkable. Without it, `config/mutmut_gf.cfg` is just
a list of test names somebody typed, and one more `--deselect` line added later to make a
number clear the threshold would look exactly like the other twenty-two.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "gf_excluded_tests.py"


def test_config_matches_the_derived_exclusion_set():
    """`--check` fails loudly if the config and the rule disagree in EITHER direction.

    Both directions matter, and for different reasons. An entry in the config that the
    rule does not derive is a hand-widened exclusion — the failure mode that would let
    someone quietly delete an inconvenient test from the measurement. An entry the rule
    derives but the config lacks means a source-scanning test is still in the run,
    contributing textual kills that inflate the score.
    """
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    assert proc.returncode == 0, (
        "config/mutmut_gf.cfg no longer matches the derived exclusion set.\n"
        "Regenerate it with `python scripts/gf_excluded_tests.py` rather than editing "
        "by hand, and if the RULE itself needs to change, amend protocol doc 20 first.\n\n"
        f"{proc.stdout}{proc.stderr}"
    )


def test_the_rule_is_narrow_enough_to_mean_something():
    """A rule that excluded most of the suite would satisfy the check above and still be
    worthless. This pins the order of magnitude: the exclusion is a small, named minority
    of the suite, not a quiet gutting of it."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, text=True, cwd=_ROOT
    )
    assert proc.returncode == 0, proc.stderr

    total = len(list((_ROOT / "tests").rglob("test_*.py")))
    deselects = proc.stdout.count("--deselect=")
    assert deselects <= total * 0.10, (
        f"{deselects} deselections against {total} test files — the exclusion rule has "
        "stopped being a narrow carve-out for source-scanning tests. Re-read AMENDMENT 1 "
        "before touching it; this bound is the thing standing between a mutation score "
        "and a number chosen by deleting tests."
    )


def test_behavioural_tests_for_the_biggest_module_survive_the_exclusion():
    """Deselection is per test FUNCTION for a reason.

    `tests/test_tool_surface.py` both scans `*.py` and holds the behavioural tests for
    `tool_surface.py`, which is 287 of G-F's mutants. Excluding the whole file would have
    discarded real coverage and depressed the score for a reason unrelated to the code
    under test. This asserts the file is still partly in the run — i.e. that the
    exclusion never silently coarsened back to file granularity.
    """
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, text=True, cwd=_ROOT
    )
    assert proc.returncode == 0, proc.stderr

    assert "--deselect=tests/test_tool_surface.py\n" not in proc.stdout, (
        "the whole of test_tool_surface.py is being deselected; its behavioural tests "
        "for tool_surface.py (287 mutants) would leave the measurement with it"
    )
    assert "--deselect=tests/test_tool_surface.py::" in proc.stdout, (
        "expected per-function deselections from test_tool_surface.py"
    )


def test_a_gate_script_NAMED_IN_PROSE_does_not_exclude_anything():
    """Audit #38. Rule B is about INVOKING a gate script, not mentioning one.

    It used to be a bare substring match over the raw file text, so this module docstring
    in `tests/telemetry/test_failopen.py` —

        "The lint (`scripts/lint_fail_open.py`) pins that call sites exist; this pins
         that the mechanism they call actually records…"

    — matched at MODULE level and deselected all eight tests in the file. Seven were
    ordinary behavioural tests of the fail-open store, including
    `test_unreadable_store_is_unknown_not_zero`, which is the RED2-02 shape this whole
    campaign exists to protect. An excluded test kills nothing, so the score was being
    measured over a smaller suite than intended.

    Note a path-based rule would not have helped: the docstring names the full
    `scripts/lint_fail_open.py` path. The distinction is WHERE the name appears —
    invocations are code, mentions are documentation — so the rule now matches against
    the segment with comments and docstrings stripped.
    """
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("gfx", _SCRIPT)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    derived = mod.derive()
    failopen = {k for k in derived if "telemetry/test_failopen.py" in k}

    assert "tests/telemetry/test_failopen.py" not in derived, (
        "the whole file is excluded again — a docstring is being read as an invocation"
    )
    assert failopen == {
        "tests/telemetry/test_failopen.py::test_the_protected_modules_actually_call_it"
    }, (
        "exactly one test in this file scans source (it reads cost.py, router.py and "
        f"execution_ledger.py and counts a substring). Got: {sorted(failopen)}"
    )
    assert "A2" in " ".join(derived[next(iter(failopen))]), (
        "it must be excluded for READING SOURCE (A2), not for naming a gate script (B)"
    )


def test_the_loop_form_of_a_source_scan_is_still_caught():
    """The false NEGATIVE that fixing rule B exposed.

    Rule A2's original pattern required the quoted module name within 80 characters of
    `.read_text(`. That misses:

        for name in ("cost.py", "router.py", "execution_ledger.py"):
            total += (src / name).read_text().count("failopen.record(")

    where the names sit two lines above and the path is built from a loop variable. It
    scans three MUTATED modules and asserts on their text — precisely what A2 is for.

    It went unnoticed because rule B was excluding the whole file for a docstring: one
    over-exclusion masked one under-exclusion, and neither was visible while the file was
    being dropped wholesale. Recorded because that is the general hazard — a wrong rule
    can hide a second wrong rule, and fixing one is what surfaces the other.
    """
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location("gfx", _SCRIPT)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    loop_scan = (
        'def test_x():\n'
        '    for name in ("cost.py", "router.py"):\n'
        '        total += (src / name).read_text().count("failopen.record(")\n'
    )
    assert any("A2" in r for r in mod._reasons_for(loop_scan)), (
        "the loop form of a source scan is not being caught by rule A2"
    )

    prose_only = (
        'def test_y():\n'
        '    """See scripts/lint_fail_open.py for the companion check."""\n'
        '    assert compute() == 3\n'
    )
    assert mod._reasons_for(prose_only) == [], (
        "a docstring mentioning a gate script must not exclude a behavioural test"
    )
