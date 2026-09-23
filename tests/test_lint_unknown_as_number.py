"""The unknown-as-number lint: proven on a positive, then ratcheted.

S9 found one instance of "an absent value coerced to 0 and then compared".
`scripts/lint_unknown_as_number.py` looks for the *flow* rather than the
pattern — the coercion only matters where it reaches a comparison or a mean.

Two things are tested here, and the order matters.

**First, that the detector detects.** A lint reporting zero findings and a lint
that is broken produce identical output, and this repo has already shipped a
near-duplicate stage that reported "0 collapses" because it was comparing raw
whitespace splits and nothing could ever match. So the detector is proven
against a synthetic copy of the pre-fix S9 source, not against the repo — the
repo is not required to keep the disease in order for the test to mean
something.

**Second, a ratchet, not a gate.** The lint finds 113 sites in `src/`, and a
sample of four showed the precision is real but well under 100%:

    user_routing_policy._policy_quota_exhaustion  REAL — a provider whose
        quota pressure was never measured reads as 0.0 pressure and keeps full
        priority. Unknown as the favourable answer, in routing.
    tools/admin.llm_quality_guard                 PLAUSIBLE — an absent
        avg_score compares as 0.0 against a quality threshold.
    streaming_judge.observe                       INTENTIONAL — an absent
        threshold disables the judge, which is the conservative reading and is
        named as such ("no_threshold").
    ui/session_summary.render_main_panel          BENIGN — display code where
        a missing count really is zero.

A pass/fail gate over a class that size, at that precision, is a gate people
learn to ignore. So the count is frozen and may only go down. Fixing a site
lowers the number and this test tells you to lower the constant with it; adding
a new coercion into a comparison fails immediately, which is the property that
actually stops the next S9.
"""

from __future__ import annotations

import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from lint_unknown_as_number import scan_source, scan_path, DEFAULT_TARGET  # noqa: E402


#: The two S9 sites, exactly as they read before the fix, plus a benign
#: control. Verbatim so that the detector is tested against the real defect
#: rather than against a shape invented to match the detector.
PRE_FIX_S9 = '''
def analyze_gaps(decisions, corrections):
    gaps = []
    for d in decisions:
        gap_flags = []
        conf = d.get("classifier_confidence", 0) or 0
        if conf < 0.70:
            gap_flags.append("LOW_CONFIDENCE")
    return gaps


def analyze_facts(decisions, corrections):
    confidences = [d.get("classifier_confidence", 0) or 0 for d in decisions]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return {"avg_confidence": avg_conf}


def benign_sum(decisions):
    """Summed, never compared, never averaged. Absent really is zero here."""
    return sum(d.get("input_tokens", 0) or 0 for d in decisions)
'''


# ── 1. The detector detects ───────────────────────────────────────────────


def test_it_catches_the_comparison_shape() -> None:
    """S9's `analyze_gaps`: coerced, assigned, then compared to a threshold."""
    hits = scan_source(PRE_FIX_S9, pathlib.Path("synthetic_s9.py"))
    gaps = [h for h in hits if h.func == "analyze_gaps"]
    assert gaps, "the detector missed the exact defect it was built for"
    assert gaps[0].shape == "compared-via-name"


def test_it_catches_the_mean_shape() -> None:
    """S9's `analyze_facts`: NULLs averaged in as real observations."""
    hits = scan_source(PRE_FIX_S9, pathlib.Path("synthetic_s9.py"))
    means = [h for h in hits if h.func == "analyze_facts"]
    assert means, "a coerced absent value averaged into a mean was not caught"
    assert means[0].shape == "averaged"


def test_it_does_not_flag_a_plain_sum() -> None:
    """A lint that flags everything is the same as one that flags nothing."""
    hits = scan_source(PRE_FIX_S9, pathlib.Path("synthetic_s9.py"))
    assert not [h for h in hits if h.func == "benign_sum"], (
        "summing token counts is not the defect; flagging it would bury the "
        "cases that are"
    )


def test_the_fixed_source_is_clean() -> None:
    """The real `retrospective.py` must no longer trip its own lint.

    This is the half that would silently rot: if the fix were reverted, the
    synthetic tests above would still pass, because they read a string constant.
    """
    hits = scan_path(REPO_ROOT / "src" / "llm_router" / "retrospective.py")
    offenders = [h for h in hits if h.func in {"analyze_gaps", "analyze_facts"}]
    assert not offenders, (
        "retrospective.py has regressed to reading an absent value as 0:\n"
        + "\n".join(h.render() for h in offenders)
    )


# ── 2. The ratchet ────────────────────────────────────────────────────────

#: Measured 2026-09-23 on src/llm_router. Lower this when you fix a site.
#: Never raise it — a new coercion reaching a comparison is the defect this
#: exists to stop.
BASELINE = 113


def _current_count() -> int:
    return sum(len(scan_path(p)) for p in sorted(DEFAULT_TARGET.rglob("*.py")))


def test_the_scan_is_not_vacuous() -> None:
    """Anti-vacuity: a zero from a scan that found no files proves nothing."""
    files = list(DEFAULT_TARGET.rglob("*.py"))
    assert len(files) > 100, f"only {len(files)} files scanned — check the path"


def test_unknown_as_number_does_not_grow() -> None:
    count = _current_count()
    assert count <= BASELINE, (
        f"{count} sites coerce an absent value to 0 and then compare or average "
        f"it, up from {BASELINE}.\n\n"
        "An absent value is not a zero. Read it as None and give 'unmeasured' "
        "its own branch — see S9 in audit/REMEDIATION_II_RUN.md, where a NULL "
        "column produced 213 'High' confidence CLASSIFIER_ERROR findings.\n\n"
        "Run: python scripts/lint_unknown_as_number.py"
    )


def test_the_baseline_is_not_stale() -> None:
    """If the count has dropped, lower the constant in the same commit.

    A baseline left above the real number is a ratchet with slack in it: the
    next regression fits underneath it and lands green.
    """
    count = _current_count()
    assert count == BASELINE, (
        f"the lint now finds {count} sites but BASELINE says {BASELINE}. "
        f"Set BASELINE = {count} in this file — a ratchet with slack lets the "
        "next regression land green."
    )
