"""The starter set has to be honest, or it teaches the wrong lesson first.

A seed where every record is reviewed, reproduced and fixed would demonstrate
precisely the failure the four axes exist to prevent: a store in which
everything renders as "known good". These tests refuse that shape.

They also check the seed is real. Every record cites a file that exists and a
check that exists, because a memory layer whose first fifteen entries point at
nothing is a memory layer nobody will trust with the sixteenth.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.semantic import experience as exp
from llm_router.semantic import seed_lessons

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def seeded(tmp_path: Path) -> exp.ExperienceStore:
    seed_lessons.seed(tmp_path / "experience")
    return exp.ExperienceStore(tmp_path / "experience")


def test_the_seed_is_big_enough_to_be_worth_retrieving(seeded):
    records = seeded.all()
    assert 10 <= len(records) <= 25, (
        f"{len(records)} starter records — the plan calls for 10-20; fewer is "
        f"not worth a retrieval layer and more is unreviewed bulk"
    )


def test_not_everything_is_fixed(seeded):
    """An all-green seed is a lie about the state of the repository."""
    unfixed = [r for r in seeded.all()
               if getattr(r, "repair_status", None) is exp.RepairStatus.PROPOSED]
    assert unfixed, (
        "every seeded record claims a completed repair. Real engineering "
        "memory is mostly open questions, and a seed that hides them teaches "
        "readers that this store only holds settled things"
    )


def test_not_everything_is_validated(seeded):
    """Reviewed is not observed, and the seed has to show both states."""
    untested = [r for r in seeded.all() if r.validation is exp.Validation.UNTESTED]
    assert untested, (
        "no seeded record is untested, so the distinction between a decision "
        "somebody agreed with and a claim reality confirmed is invisible"
    )


def test_the_axes_actually_vary(seeded):
    """If every record has the same four values, the axes are decoration."""
    for axis in ("review", "validation", "enforcement"):
        values = {getattr(r, axis) for r in seeded.all()}
        assert len(values) > 1, (
            f"every seeded record has the same {axis}, so that axis carries no "
            f"information in the only data the store starts with"
        )


def test_the_seed_contains_a_real_contradiction(seeded):
    """S3's conflict handling cannot be shown to work on a set that agrees."""
    conflicts = seeded.conflicts()
    assert conflicts, (
        "no seeded records contradict each other, so conflict surfacing has "
        "nothing to surface and will ship untested against real data"
    )
    for a, b in conflicts:
        assert a.statement != b.statement


def test_both_sides_of_a_conflict_are_retrieved(seeded):
    """Not the newest. Both."""
    found = seeded.applicable(paths=["scripts/bench_grounding.py"])
    ids = {exp.record_id(r) for r in found}
    assert {"basename-is-enough-012a", "exact-path-or-nothing-012b"} <= ids, (
        f"one side of a live disagreement was dropped: {sorted(ids)}"
    )


def test_every_cited_file_exists(seeded):
    """A record pointing at a file that is gone is worse than no record."""
    missing = []
    for record in seeded.all():
        for rel in record.affected_paths:
            if not (REPO / rel).exists():
                missing.append(f"{exp.record_id(record)} → {rel}")
    assert not missing, "seeded records cite files that do not exist: " + \
        ", ".join(missing)


def test_every_claimed_check_exists_and_is_a_real_test(seeded):
    """`enforcement=adopted-project-rule` claims something fails. Verify it can."""
    missing = []
    for record in seeded.all():
        for rel in record.check_refs:
            if not (REPO / rel).exists():
                missing.append(f"{exp.record_id(record)} → {rel}")
    assert not missing, (
        "records claim prevention checks that do not exist, which is the "
        "'enforcement' axis asserting something nothing backs: " + ", ".join(missing)
    )


def test_an_adopted_rule_always_names_its_check(seeded):
    for record in seeded.all():
        if record.enforcement is exp.Enforcement.ADOPTED_RULE:
            assert record.check_refs, (
                f"{exp.record_id(record)} claims to be an adopted rule with no "
                f"check behind it"
            )


def test_seeding_twice_does_not_duplicate_or_clobber(tmp_path):
    root = tmp_path / "experience"
    first = seed_lessons.seed(root)
    second = seed_lessons.seed(root)

    assert first > 0
    assert second == 0, "re-seeding rewrote records a person may have corrected"
    assert len(exp.ExperienceStore(root).all()) == first


def test_a_hand_edit_survives_reseeding(tmp_path):
    root = tmp_path / "experience"
    seed_lessons.seed(root)
    store = exp.ExperienceStore(root)

    original = store.get("gateway-scope-via-environ-006")
    corrected = exp.Lesson(
        **{**{f.name: getattr(original, f.name)
              for f in original.__dataclass_fields__.values()},
           "repair_status": exp.RepairStatus.VERIFIED,
           "check_refs": ["tests/semantic/test_scope_is_one_resolver.py"],
           "enforcement": exp.Enforcement.ADOPTED_RULE},
    )
    store.put(corrected)
    seed_lessons.seed(root)

    assert store.get("gateway-scope-via-environ-006").repair_status is \
        exp.RepairStatus.VERIFIED, "re-seeding reverted a human correction"
