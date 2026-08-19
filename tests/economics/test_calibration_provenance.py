"""#12(b) — a projection built on a fallback must not read as a measurement.

INITIAL_CALIBRATION holds empirical output-token profiles for exactly ONE pair:
(claude-sonnet-4-6, QUERY), n=1114. Every other (model, task) pair --- including
the savings baseline, which WP-05 repointed to Opus --- falls through to
``_LEGACY_FALLBACK_OUTPUT = 80``, a static assumption carried over from
pre-calibration code.

``predict_cost`` returns a bare float for both cases. A caller cannot tell a
figure derived from 1114 observations from one derived from a hardcoded 80, and
auto-route renders the result as a plain "$0.0012" either way.

That is the same defect shape this audit has now found repeatedly: a value whose
CONFIDENCE is dropped on the floor, leaving a number that looks like data. The
zero-that-reads-as-data (RED2-02, the unreadable ledger rendering "$0.00 saved")
is the extreme version; this is the quieter one, because the number is not even
wrong --- it is just unmarked.

WP-05's rule is already implemented in ``llm_router.provenance``: an estimated figure
renders as "~$X (estimated)". Calibration simply never used it.

NOT FIXED HERE, and deliberately: the corpus still covers one model. Inventing
profiles for the others would be fabricating measurements, which is worse than
admitting the gap. What lands is the ADMISSION --- the gap becomes visible and
countable instead of silent.
"""

from __future__ import annotations

import pytest

from llm_router.types import TaskType


def test_a_calibrated_pair_is_measured():
    """The one pair with real observations must claim to be measured, or the
    provenance tag is decoration rather than a distinction."""
    from llm_router.calibration import predict_cost_measured

    m = predict_cost_measured("claude-sonnet-4-6", TaskType.QUERY, 200)
    assert m.provenance == "measured", (
        "the only empirically-calibrated pair in the corpus reports as estimated"
    )
    assert m.known


def test_an_uncalibrated_pair_is_estimated():
    """The savings baseline has NO profile. Its projection uses the static
    80-token fallback and must say so."""
    from llm_router.calibration import predict_cost_measured
    from llm_router.pricing import savings_baseline_model

    m = predict_cost_measured(savings_baseline_model(), TaskType.QUERY, 200)
    assert m.provenance == "estimated", (
        f"{savings_baseline_model()} has no calibration profile, so its cost "
        "projection is an assumption presented as a measurement"
    )
    assert m.detail, "an estimated figure with no stated basis is not auditable"


def test_estimated_figures_render_with_a_marker():
    """The tag has to reach the user. A provenance field nothing displays is
    the same as no provenance field."""
    from llm_router.calibration import predict_cost_measured
    from llm_router.pricing import savings_baseline_model

    rendered = predict_cost_measured(
        savings_baseline_model(), TaskType.QUERY, 200
    ).render()
    assert "estimated" in rendered, f"rendered as a bare figure: {rendered!r}"


def test_the_same_task_on_an_uncalibrated_task_type_is_also_estimated():
    """Calibration is keyed on (model, task), not model. Sonnet is calibrated
    for QUERY only; CODE on the same model is still an assumption."""
    from llm_router.calibration import predict_cost_measured

    m = predict_cost_measured("claude-sonnet-4-6", TaskType.CODE, 200)
    assert m.provenance == "estimated", (
        "a calibrated MODEL is being treated as a calibrated (model, task) pair"
    )


def test_predict_cost_still_returns_the_same_number():
    """The provenance wrapper must not change any projection. If it did, this
    would be a pricing change wearing a labelling change's clothes."""
    from llm_router.calibration import predict_cost, predict_cost_measured

    for model in ("claude-sonnet-4-6", "claude-opus-5", "gpt-4o-mini"):
        for tt in (TaskType.QUERY, TaskType.CODE):
            assert predict_cost_measured(model, tt, 200).or_zero() == pytest.approx(
                predict_cost(model, tt, 200)
            ), f"{model}/{tt} projection changed"


# ── the blind spot itself ────────────────────────────────────────────────────

def test_calibration_coverage_is_reportable():
    """WP-07's point, applied here: a surface that cannot say how much of its
    traffic it has no profile for cannot be audited for coverage.

    The denominator is deliberately taken from the PRICED-MODEL list, not from
    INITIAL_CALIBRATION's own keys. Measuring the corpus against itself would
    report 100% coverage forever --- the identical trap as
    tool_surface.unregistered() checking tier constants against _TIERS, and
    lint_tool_surface.py checking emitters against emitters.
    """
    from llm_router.calibration import calibration_coverage

    cov = calibration_coverage()
    assert cov.total > cov.profiled, (
        "coverage reports every routable pair as profiled, which is false: the "
        "corpus holds one entry. The denominator is probably the corpus itself."
    )
    assert cov.profiled >= 1
    assert 0.0 < cov.fraction < 1.0


def test_coverage_names_the_gap_rather_than_only_counting_it():
    """A bare percentage tells a maintainer nothing actionable. Which models
    are unprofiled is the part that gets the corpus extended."""
    from llm_router.calibration import calibration_coverage
    from llm_router.pricing import savings_baseline_model

    unprofiled = calibration_coverage().unprofiled_models
    assert savings_baseline_model() in unprofiled, (
        "the SAVINGS BASELINE is unprofiled and must appear in the gap list --- "
        "it is the model every savings figure is computed against"
    )


# ── the tag must reach the display ───────────────────────────────────────────

def _load_hook():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "auto_route_hook_prov",
        Path(__file__).resolve().parent.parent.parent
        / "src" / "llm_router" / "hooks" / "auto-route.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_estimate_cost_carries_provenance():
    """The hook prices its hint against the savings baseline, which has no
    profile. A provenance field nothing propagates is the same as none."""
    out = _load_hook()._estimate_cost("query", "moderate")
    assert out.get("provenance") == "estimated", (
        "the displayed savings figure rests on an 80-token assumption and does "
        "not say so"
    )


def test_savings_stays_machine_parseable():
    """The tag is an ADDITIONAL key, not a reformat. Existing callers parse
    out['savings'] with lstrip('$'); breaking that to add a label would trade
    one defect for another."""
    out = _load_hook()._estimate_cost("query", "moderate")
    assert out["savings"].startswith("$")
    float(out["savings"].lstrip("$"))


def test_legacy_static_fallback_is_also_tagged():
    """The static map is the least-measured path of all — a hardcoded table
    used when calibration will not import. It must not render as measured."""
    out = _load_hook()._legacy_static_savings("code", "complex")
    assert out["savings"] == "$0.010", "legacy string changed"
    assert out.get("provenance") == "estimated"
