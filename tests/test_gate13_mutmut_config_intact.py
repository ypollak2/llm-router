"""Gate 13's mutmut scope must survive every G-F run.

`scripts/gf_mutmut.py` swaps `config/mutmut_gf.cfg` into `setup.cfg` for the
duration of one mutation run and restores the original afterwards. If a run is
killed between those two steps — Ctrl-C, an OOM, a laptop lid — `setup.cfg` is
left holding the **G-F** scope while claiming to be Gate 13's.

Nothing else would notice. mutmut would keep running, over the wrong eight files,
and report a perfectly well-formed score. That is the exact failure shape this
audit keeps finding: a wrong measurement that looks like a right one.

These assertions run in the ordinary suite, so a stranded swap is caught on the
next test run rather than the next release.

WHY THIS FILE EXISTS RATHER THAN TRUSTING THE `finally`
--------------------------------------------------------
`gf_mutmut.py` restores in a `finally` and verifies the sha256 matches. That
covers the ordinary failure paths. It does **not** cover the process being killed
outright, which is precisely when a swap is most likely to strand. A guard that
depends on the guarded code running to completion is not a guard.
"""

from __future__ import annotations

import configparser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SETUP_CFG = _ROOT / "setup.cfg"

#: The Gate-13 campaign's scope. Committed here as an INDEPENDENT declaration —
#: reading it out of setup.cfg and comparing it to setup.cfg would pass no matter
#: what the file contained, which is the self-validating shape found four times
#: in this audit already.
_GATE13_ONLY_MUTATE = {
    "src/llm_router/execution_ledger.py",
    "src/llm_router/execution_signal.py",
    "src/llm_router/operational_signal.py",
    "src/llm_router/context_signal.py",
    "src/llm_router/gates.py",
    "bench/savings.py",
}


#: G-F's scope, as `config/mutmut_gf.cfg` declares it. Same independent-declaration
#: reasoning as above: derived from neither file at runtime.
_GF_ONLY_MUTATE = {
    "src/llm_router/cost.py",
    "src/llm_router/savings.py",
    "src/llm_router/execution_ledger.py",
    "src/llm_router/router.py",
    "src/llm_router/tool_surface.py",
    "src/llm_router/classify.py",
    "src/llm_router/budget.py",
    "src/llm_router/coverage.py",
}


def _is_mutmut_working_copy() -> bool:
    """True when this file is executing inside the tree mutmut builds for a run.

    mutmut copies the repo to ``<repo>/mutants`` and runs the suite from there. It
    takes that copy WHILE gf_mutmut.py has the G-F config swapped into setup.cfg, so
    the copy's setup.cfg legitimately holds G-F's scope rather than Gate 13's.

    WHY THIS IS NOT A SKIP
    ----------------------
    Skipping here would leave the assertions blind in exactly the tree where a wrong
    scope does the most damage. Instead each test asserts the scope that is CORRECT
    for the tree it is in, so neither context has a hole.

    WHY IT MATTERS ENOUGH TO DETECT AT ALL
    --------------------------------------
    mutmut marks a mutant KILLED whenever the suite fails. A test that fails for an
    ENVIRONMENTAL reason fails on every mutant run it covers and marks them all
    killed regardless of the mutation — inflating the score in the direction that
    flatters the result. These three assertions failed inside the working copy by
    construction, so the guard written to protect Gate 13's measurement would have
    corrupted G-F's.

    Detection is by location, not by content: mutmut's copy is always the `mutants`
    directory of a git repository. `test_detector_is_false_in_the_real_repository`
    pins the other direction, so this can never quietly become always-true and turn
    the guard above into a no-op.
    """
    return _ROOT.name == "mutants" and (_ROOT.parent / ".git").exists()


def _only_mutate() -> set[str]:
    cfg = configparser.ConfigParser()
    cfg.read(_SETUP_CFG)
    raw = cfg.get("mutmut", "only_mutate", fallback="")
    return {line.strip() for line in raw.splitlines() if line.strip()}


def test_detector_is_false_in_the_real_repository():
    """Locks the other direction of `_is_mutmut_working_copy`.

    If that predicate ever became always-true, every assertion below would silently
    switch to checking G-F's scope and the stranded-swap guard would be a no-op that
    still reported green. CI runs this in the real repository, where it must be False.
    """
    if _ROOT.name == "mutants":  # pragma: no cover — only inside mutmut's copy
        return
    assert not _is_mutmut_working_copy(), (
        f"detector claims {_ROOT} is mutmut's working copy; the Gate-13 assertions "
        "would stop checking Gate 13's scope while still passing"
    )


def test_setup_cfg_still_holds_gate_13_scope():
    """The load-bearing assertion: a stranded G-F swap fails here."""
    actual = _only_mutate()

    if _is_mutmut_working_copy():
        # Inside mutmut's copy the G-F scope is CORRECT — the copy was taken with the
        # swap active. Asserting it (rather than skipping) keeps this tree covered.
        assert actual == _GF_ONLY_MUTATE, (
            "inside mutmut's working copy setup.cfg should hold G-F's eight modules "
            f"(the swap was active when the copy was taken), got: {sorted(actual)}"
        )
        return

    assert actual == _GATE13_ONLY_MUTATE, (
        "setup.cfg's [mutmut] scope is not Gate 13's.\n"
        f"  missing: {sorted(_GATE13_ONLY_MUTATE - actual)}\n"
        f"  unexpected: {sorted(actual - _GATE13_ONLY_MUTATE)}\n"
        "If the unexpected entries are G-F's eight modules, a gf_mutmut.py run "
        "was killed mid-swap. Restore setup.cfg from git."
    )


def test_gate_13_keeps_its_explicit_test_selection():
    """Its per-module test list is part of the campaign's evidence. G-F
    deliberately omits one; losing Gate 13's would silently widen its runs and
    change its score without changing its scope."""
    cfg = configparser.ConfigParser()
    cfg.read(_SETUP_CFG)
    sel = cfg.get("mutmut", "pytest_add_cli_args_test_selection", fallback="")

    if _is_mutmut_working_copy():
        # G-F's config deliberately omits test selection: naming the tests that
        # "should" own a behaviour is how B8 was misread as absent coverage. Assert
        # the absence, so this tree is checked rather than waved through.
        assert sel == "", (
            "inside mutmut's working copy the G-F config should carry NO test "
            f"selection, got: {sel!r}"
        )
        return

    assert "tests/test_gates_mutation_coverage.py" in sel, (
        "Gate 13's explicit test selection is gone from setup.cfg"
    )


def test_pyproject_does_not_hijack_mutmut_config():
    """mutmut 3.6 prefers pyproject.toml [tool.mutmut] and IGNORES setup.cfg
    entirely when it is present.

    Adding that section — the obvious way to give G-F its own config — would
    silently disable Gate 13's scope rather than sitting beside it. This asserts
    nobody has, so the swap in gf_mutmut.py still means what it says.
    """
    assert "[tool.mutmut]" not in (_ROOT / "pyproject.toml").read_text(), (
        "pyproject.toml has a [tool.mutmut] section; mutmut will ignore "
        "setup.cfg entirely and BOTH the Gate-13 scope and the G-F swap become "
        "no-ops that still produce a score"
    )


def test_the_gf_config_exists_and_targets_a_different_scope():
    """Guards the guard: if config/mutmut_gf.cfg vanished or drifted to Gate
    13's scope, the tests above would pass while G-F measured nothing new."""
    gf = _ROOT / "config" / "mutmut_gf.cfg"
    assert gf.exists(), "config/mutmut_gf.cfg is missing"

    cfg = configparser.ConfigParser()
    cfg.read(gf)
    gf_scope = {
        line.strip()
        for line in cfg.get("mutmut", "only_mutate", fallback="").splitlines()
        if line.strip()
    }
    assert len(gf_scope) == 8, f"expected G-F's eight modules, got {len(gf_scope)}"
    assert gf_scope != _GATE13_ONLY_MUTATE, "the G-F config duplicates Gate 13's scope"
    # execution_ledger.py is the single legitimate overlap between the campaigns.
    assert gf_scope & _GATE13_ONLY_MUTATE == {"src/llm_router/execution_ledger.py"}
