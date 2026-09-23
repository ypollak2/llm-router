"""S9 — a confidence that was never recorded is not a confidence of zero.

Found while measuring S8. `routing_decisions.classifier_confidence` is NULL for
**213 of the 214** rows with trusted (`provenance='runtime'`) origin on the
development ledger; the 1387 rows carrying 0.9 are the pre-provenance
placeholder rows that S4a already excludes from the bandit.

`retrospective.py` read that column as::

    conf = d.get("classifier_confidence", 0) or 0
    if conf < 0.70:
        gap_flags.append("LOW_CONFIDENCE")

so a NULL became 0.0, fell below every threshold, and was promoted by
`diagnose_root_causes` into a ``CLASSIFIER_ERROR`` at confidence "High" with
the evidence string ``"Classifier confidence 0%"``. A root-cause analysis was
manufacturing a certain finding out of a missing column, for 99.5% of the rows
it could trust.

The same coercion ran in `summarise`, averaging NULLs in as zeros, so
"avg confidence" was approximately "the share of rows that recorded one".

Both are the audit's recurring shape — unknown rendered as a confident answer,
and a denominator that disappeared — so both are fixed and pinned here.
"""

from __future__ import annotations

from llm_router.retrospective import analyze_facts, analyze_gaps


def _decision(**kw):
    base = {
        "id": 1,
        "timestamp": "2026-09-23T10:00:00",
        "task_type": "code",
        "final_model": "ollama/qwen3-coder:30b",
        "recommended_model": "ollama/qwen3-coder:30b",
        "success": 1,
        "cost_usd": 0.0,
        "judge_score": None,
    }
    base.update(kw)
    return base


# ── Premise ───────────────────────────────────────────────────────────────


def test_the_fixtures_really_do_differ_in_the_way_that_matters() -> None:
    """A NULL and a genuine 0.0 must be distinguishable, or nothing below means
    anything."""
    unrecorded = _decision(id=1, classifier_confidence=None)
    genuinely_zero = _decision(id=2, classifier_confidence=0.0)
    assert unrecorded["classifier_confidence"] is None
    assert genuinely_zero["classifier_confidence"] == 0.0


# ── analyze_gaps ──────────────────────────────────────────────────────────


def test_an_unrecorded_confidence_is_not_flagged_low_confidence() -> None:
    gaps = analyze_gaps([_decision(id=1, classifier_confidence=None)], [])
    flags = [f for g in gaps for f in g["flags"]]
    assert "LOW_CONFIDENCE" not in flags, (
        "a column nobody wrote is being reported as a measured low confidence"
    )


def test_an_unrecorded_confidence_is_reported_as_unmeasured() -> None:
    """Not flagging it is not the same as hiding it — the gap is real, it is
    just a gap in the instrumentation rather than in the classifier."""
    gaps = analyze_gaps([_decision(id=1, classifier_confidence=None)], [])
    flags = [f for g in gaps for f in g["flags"]]
    assert "CONFIDENCE_UNMEASURED" in flags
    assert any("never recorded" in g.get("reason", "") for g in gaps)


def test_a_genuinely_low_confidence_is_still_flagged() -> None:
    """The fix must not buy its way out by flagging nothing (anti-vacuity)."""
    gaps = analyze_gaps([_decision(id=1, classifier_confidence=0.4)], [])
    flags = [f for g in gaps for f in g["flags"]]
    assert "LOW_CONFIDENCE" in flags
    assert "CONFIDENCE_UNMEASURED" not in flags


def test_a_measured_zero_is_still_low_confidence() -> None:
    """0.0 recorded by a classifier that really was unsure is a finding. Only
    the absence of a number is not."""
    gaps = analyze_gaps([_decision(id=1, classifier_confidence=0.0)], [])
    assert "LOW_CONFIDENCE" in [f for g in gaps for f in g["flags"]]


# ── summarise ─────────────────────────────────────────────────────────────


def test_unrecorded_confidences_are_not_averaged_in_as_zeros() -> None:
    decisions = [
        _decision(id=1, classifier_confidence=0.8),
        _decision(id=2, classifier_confidence=None),
        _decision(id=3, classifier_confidence=None),
    ]
    facts = analyze_facts(decisions, [])
    # The old code returned 0.8/3 = 0.267.
    assert facts["avg_confidence"] == 0.8
    assert facts["confidence_measured"] == 1
    assert facts["confidence_unmeasured"] == 2


def test_the_mean_travels_with_its_denominator() -> None:
    """0.9 over one decision and 0.9 over a thousand are different claims."""
    facts = analyze_facts([_decision(id=1, classifier_confidence=None)], [])
    assert facts["confidence_measured"] == 0, (
        "a mean with nothing behind it must say so"
    )
    assert facts["confidence_unmeasured"] == 1
