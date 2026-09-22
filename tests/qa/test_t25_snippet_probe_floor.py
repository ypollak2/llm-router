"""Ground Truth audit item 4.5 — the snippet path had no discrimination floor.

The two validation paths were not equally defended:

    pytest path    mutants come from a FIXED, hand-authored library applied to
                   the target. The operator cannot influence how hard they are.
    snippet path   `bad_answers` come from the CLI, ad hoc.

So a rushed operator supplying ONE trivially-wrong bad answer reached
`detected == total` and, with a complete contract, **HIGH confidence** for a
check that barely discriminates. The verifier then becomes ACTIVE and its
verdicts become Ground Truth labels.

`detected == total` is a ratio, and a ratio over a tiny hand-picked denominator
is not evidence. Three probes is the floor because one proves almost nothing and
two can be the same mistake written twice.

**What the floor deliberately does not do.** It checks the SHAPE of the probe
set — how many, whether they are distinct, whether one is the good answer — and
never their content. Judging whether a bad answer is "wrong enough" is the
operator's job; a rule that tried would be a heuristic pretending to be a
measurement, which is the thing this subsystem exists to avoid.

It is a floor on the EVIDENCE, not on the verifier: falling below it caps
confidence at MEDIUM rather than rejecting the verifier outright.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def M():
    sys.path.insert(0, str(ROOT / "scripts"))
    from groundtruth import mutants

    return mutants


def _validation(M, n_detected: int, weak: str | None):
    v = M.Validation(task_id="gt-1", executable=True, baseline_passed=True)
    v.weak_probe_set = weak
    for i in range(n_detected):
        v.mutants.append(M.MutantResult(f"bad-{i}", "probe", applied=True, detected=True))
    return v


# ── the floor itself ────────────────────────────────────────────────────────

def test_one_bad_answer_is_not_enough(M):
    why = M._snippet_discrimination_floor("correct", ["wrong"])
    assert why and "1 non-empty" in why, why


def test_repeated_probes_are_not_evidence(M):
    why = M._snippet_discrimination_floor("correct", ["wrong", "wrong", "wrong"])
    assert why and "distinct" in why, why


def test_empty_probes_do_not_count(M):
    why = M._snippet_discrimination_floor("correct", ["a", "", "   ", "b"])
    assert why, "whitespace-only probes were counted toward the floor"


def test_a_probe_equal_to_the_good_answer_is_refused(M):
    why = M._snippet_discrimination_floor("correct", ["a", "b", "correct"])
    assert why and "identical" in why, why


def test_three_distinct_probes_clear_the_floor(M):
    """Anti-over-correction: a floor nothing can clear is not a floor."""
    assert M._snippet_discrimination_floor("correct", ["a", "b", "c"]) is None


# ── how classify uses it ────────────────────────────────────────────────────

def test_a_thin_probe_set_cannot_reach_high(M):
    """The exact defect: detecting 1 of 1 used to be HIGH."""
    weak = M._snippet_discrimination_floor("correct", ["wrong"])
    conf, why = M.classify(_validation(M, 1, weak),
                           strategy_is_mechanical=True, contract_complete=True)
    assert conf == M.MEDIUM, f"thin probe set still reached {conf}: {why}"
    assert "probe set" in why


def test_a_sound_probe_set_still_reaches_high(M):
    conf, why = M.classify(_validation(M, 3, None),
                           strategy_is_mechanical=True, contract_complete=True)
    assert conf == M.HIGH, f"a well-probed verifier was capped at {conf}: {why}"


def test_the_floor_does_not_override_unusable(M):
    """A verifier that cannot detect anything stays UNUSABLE, not MEDIUM.

    The floor caps an over-claim; it must not launder a failure upward.
    """
    v = M.Validation(task_id="gt-1", executable=True, baseline_passed=True)
    v.weak_probe_set = "only 1 non-empty bad answer"
    v.mutants.append(M.MutantResult("bad-0", "probe", applied=True, detected=False))
    conf, why = M.classify(v, strategy_is_mechanical=True, contract_complete=True)
    assert conf == M.UNUSABLE, f"a non-discriminating verifier was rated {conf}: {why}"


def test_the_pytest_path_is_unaffected(M):
    """Its mutants come from a fixed library, so it has no operator-chosen probes."""
    conf, why = M.classify(_validation(M, 6, None),
                           strategy_is_mechanical=True, contract_complete=True)
    assert conf == M.HIGH, why


def test_the_floor_judges_shape_not_content(M):
    """Pin the design choice, so a later 'improvement' does not add a heuristic.

    Two probe sets that differ only in how *plausible* the wrong answers are must
    be treated identically — the floor has no opinion about that.
    """
    subtle = M._snippet_discrimination_floor("42", ["41", "43", "4.2"])
    absurd = M._snippet_discrimination_floor("42", ["banana", "", "zzz", "qqq"])
    assert subtle is None
    assert absurd is None, (
        "the floor is judging how wrong a probe is; that is the operator's "
        "judgement and not something this rule can measure"
    )


def test_this_suite_exercises_both_outcomes(M):
    """Anti-vacuity: the floor must be capable of passing AND failing."""
    assert M._snippet_discrimination_floor("x", ["a", "b", "c"]) is None
    assert M._snippet_discrimination_floor("x", ["a"]) is not None
    assert M.MIN_BAD_ANSWERS >= 3
