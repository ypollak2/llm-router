"""AUD-06 across EVERY money surface, not just the one that was fixed.

`tests/economics/test_savings_sign.py` (WP-04, immutable) pins `session-end.py`.
It loads that one file, so it could not and did not notice that eleven other
surfaces kept the clamp — including `cost.get_team_savings`, whose output
`team.py` broadcasts to Slack/Discord, and dashboard's
``net_saved = max(0, gross - overhead)``, which is AUD-06's own sentence in code.

This file is separate because the immutable asset must not be edited.

WHY A LINT IS THE REAL FIX AND THESE TESTS SUPPORT IT
-----------------------------------------------------
`13_HISTORICAL_DEFECT_PATTERNS.md` records the `$15/$75` price bug being fixed
locally FOUR times and returning every time, "because no fix was ever made
structural". Eleven point-fixes would have been the fifth. `llm_router.savings.net_saved`
plus `scripts/lint_savings_sign.py` (CHZ-SS-01) is the structural half; these
tests keep the lint honest and cover the behaviour the lint cannot see.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


# ── the primitive ────────────────────────────────────────────────────────────

def test_net_saved_is_signed():
    from llm_router.savings import net_saved

    assert net_saved(1.0, 0.25) == pytest.approx(0.75)
    assert net_saved(0.25, 1.0) == pytest.approx(-0.75), (
        "a period that cost more than the baseline must report a NEGATIVE saving"
    )
    assert net_saved(1.0, 1.0) == 0.0


def test_net_saved_does_not_round_a_sub_cent_loss_away():
    """Routing losses are sub-cent by nature; at that magnitude the SIGN is the
    finding, and a clamp or a stray round() eats it silently."""
    from llm_router.savings import net_saved

    assert net_saved(0.0001, 0.0003) < 0


# ── the guard, and proof it can fail ─────────────────────────────────────────

def _run_lint(cwd: Path) -> int:
    return subprocess.run(
        [sys.executable, "scripts/lint_savings_sign.py"],
        cwd=cwd, capture_output=True, text=True,
    ).returncode


def test_chz_ss_01_passes_on_the_shipped_tree():
    assert _run_lint(_ROOT) == 0, "a savings clamp is present in a money module"


def test_chz_ss_01_actually_detects_a_clamp(tmp_path):
    """The guard must be shown to FAIL, not assumed to.

    A lint that passes on a clean tree is indistinguishable from a lint whose
    matcher is broken — the failure mode that let a probe report "0/6
    reproductions" while measuring nothing. This copies the repo, reintroduces
    AUD-06's clamp, and asserts CHZ-SS-01 goes red.
    """
    import shutil

    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    (work / "src" / "llm_router" / "tools").mkdir(parents=True)
    shutil.copy(_ROOT / "scripts" / "lint_savings_sign.py", work / "scripts")
    for rel in (
        "src/llm_router/cost.py", "src/llm_router/router.py", "src/llm_router/digest.py",
        "src/llm_router/retrospective.py", "src/llm_router/route_server.py",
        "src/llm_router/tools/dashboard.py",
    ):
        shutil.copy(_ROOT / rel, work / rel)

    assert _run_lint(work) == 0, "the copied subset should start clean"

    target = work / "src" / "llm_router" / "digest.py"
    src = target.read_text()
    old = "            saved = net_saved(baseline, cost)"
    assert src.count(old) == 1, "anchor drifted — this test would prove nothing"
    target.write_text(src.replace(old, "            saved = max(0.0, baseline - cost)", 1))

    assert _run_lint(work) == 1, (
        "CHZ-SS-01 did not detect a reintroduced AUD-06 clamp — the guard is blind"
    )


def test_the_lint_covers_the_surfaces_this_finding_named():
    """A module list that silently loses an entry is a guard that shrinks without
    telling anyone. These are the surfaces where the clamp was actually found."""
    from importlib import util

    spec = util.spec_from_file_location(
        "_ss_lint", _ROOT / "scripts" / "lint_savings_sign.py"
    )
    mod = util.module_from_spec(spec)
    sys.modules["_ss_lint"] = mod
    spec.loader.exec_module(mod)

    for rel in (
        "src/llm_router/cost.py",
        "src/llm_router/router.py",
        "src/llm_router/digest.py",
        "src/llm_router/retrospective.py",
        "src/llm_router/route_server.py",
        "src/llm_router/tools/dashboard.py",
    ):
        assert rel in mod.MONEY_MODULES, f"{rel} dropped out of CHZ-SS-01's scope"


def test_the_only_exemption_is_the_documented_gross_metric():
    """Every exemption is a place a real clamp can hide. One is defensible;
    a growing list means the gate is being negotiated with rather than fixed."""
    from importlib import util

    spec = util.spec_from_file_location(
        "_ss_lint2", _ROOT / "scripts" / "lint_savings_sign.py"
    )
    mod = util.module_from_spec(spec)
    sys.modules["_ss_lint2"] = mod
    spec.loader.exec_module(mod)

    assert len(mod.EXEMPTIONS) == 1, (
        f"CHZ-SS-01 now has {len(mod.EXEMPTIONS)} exemptions; each one is a hole. "
        "Justify it here deliberately or remove it."
    )
    (rel, frag), reason = next(iter(mod.EXEMPTIONS.items()))
    assert rel == "src/llm_router/execution_ledger.py"
    low = reason.lower()
    assert "potential" in low and "signed" in low, (
        "an exemption must name the upside-only metric AND its signed sibling; "
        f"got: {reason}"
    )


# ── behaviour, where the surface is callable ─────────────────────────────────

def test_dashboard_renders_a_loss_with_its_sign_and_not_in_green():
    """The display half. A negative printed in green reads as a win — the same
    display-layer failure as RED2-02's "$0.00 saved", one layer later.
    """
    src = (_ROOT / "src" / "llm_router" / "tools" / "dashboard.py").read_text()
    assert "_net_col = _GREEN if net_total_saved >= 0 else _RED" in src, (
        "the dashboard's net figure is no longer coloured by sign"
    )
    assert "net_saved = max(" not in src, "the clamped total is back"
