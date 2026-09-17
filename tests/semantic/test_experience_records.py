"""Engineering memory that cannot launder a guess into a fact.

The existing store keeps a biography of durable facts: flat sentences with a
commit and a date. That is enough to remember THAT something was decided and
useless for deciding whether it still applies. A bullet cannot say "this was
the right call on the 3.10 branch, we never reproduced the cause, and the check
that would catch a recurrence was never written".

Every field below exists because collapsing it produced a specific wrong
answer:

TWO TIMELINES. When a claim applies in the project is not when the system
learned it. A cause found today for a bug that shipped in March is valid from
March and known from today. Store one timeline and you cannot ask "what did we
believe in April?" — which is the question you ask when April's decision looks
inexplicable.

FOUR INDEPENDENT AXES. `review` is whether a person looked at it. `validation`
is whether reality did. `applicability` is whether it still holds. `enforcement`
is whether anything stops a recurrence. An accepted decision can have zero
validation; a reproduced bug can be fixed while its prevention lesson stays
active; an unresolved issue must stay discoverable without being presented as
an established root cause. Collapse them into one "status" and every one of
those states becomes "known good".

CONTRADICTIONS SURFACE. Newest-wins is a truth rule, and timestamps are not
evidence. Two maintenance branches can hold different valid decisions at the
same wall-clock instant. When claims conflict, both come back with their
provenance and the conflict is named, because the alternative is a system that
silently forgets the half it happened to see first.

DIAGNOSIS IS NOT REPAIR. The record for a bug that is understood and unfixed
must not read like a bug that is fixed. `supported_mechanism` and
`repair_status` are separate fields for that reason, and the starter record
below is deliberately a real, currently-unfixed example.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.semantic import experience as exp


@pytest.fixture
def store(tmp_path: Path):
    return exp.ExperienceStore(root=tmp_path / "experience")


def _lesson(**kw):
    base = dict(
        lesson_id="scope-write-001",
        statement="Indexing project B from inside A writes B's documents under A.",
        failure_family="project-scope",
        triggers=["an indexer that accepts a root"],
        affected_paths=["src/llm_router/okf.py"],
        affected_symbols=["_write_source_concept"],
        valid_from="2026-09-01",
        known_from="2026-09-17",
    )
    base.update(kw)
    return exp.Lesson(**base)


# ── the four axes are four questions ─────────────────────────────────────────

def test_an_accepted_decision_can_have_no_validation(store):
    """The commonest honest state, and the one a single status field destroys."""
    d = exp.Decision(
        decision_id="sqlite-not-a-graph-db",
        statement="Use SQLite for the derived index rather than a graph service.",
        alternatives=["a graph database service", "an in-memory graph"],
        rejected_because={"a graph database service": "a service to deploy and "
                                                      "operate for a local index"},
        review=exp.Review.REVIEWED,
        validation=exp.Validation.UNTESTED,
        valid_from="2026-09-18",
        known_from="2026-09-18",
    )
    store.put(d)

    got = store.get("sqlite-not-a-graph-db")
    assert got.review is exp.Review.REVIEWED
    assert got.validation is exp.Validation.UNTESTED
    assert not got.is_established(), (
        "a decision nobody has tested reported itself as established fact"
    )
    assert "a graph database service" in got.rejected_because, (
        "a rejected option lost the reason it was rejected, so nobody can tell "
        "whether the constraint behind it still holds"
    )


def test_a_reproduced_bug_can_be_fixed_while_its_lesson_stays_active(store):
    les = _lesson(
        validation=exp.Validation.REPRODUCED,
        repair_status=exp.RepairStatus.VERIFIED,
        applicability=exp.Applicability.ACTIVE,
    )
    store.put(les)

    got = store.get("scope-write-001")
    assert got.repair_status is exp.RepairStatus.VERIFIED
    assert got.applicability is exp.Applicability.ACTIVE, (
        "fixing the bug retired the lesson that prevents the next one"
    )


def test_diagnosis_and_repair_are_separate_fields(store):
    """Understood and unfixed must not read like fixed."""
    les = _lesson(
        supported_mechanism="the writer recomputes its destination without the root",
        repair_status=exp.RepairStatus.PROPOSED,
    )
    store.put(les)

    got = store.get("scope-write-001")
    assert got.supported_mechanism
    assert got.repair_status is exp.RepairStatus.PROPOSED
    assert not got.is_repaired()


def test_enforcement_is_advisory_until_a_check_exists(store):
    """A warning is not a rule. A rule has something that fails."""
    advisory = _lesson(lesson_id="advisory", check_refs=[])
    enforced = _lesson(lesson_id="enforced",
                       check_refs=["tests/okf/test_okf_scope_02_explicit_root.py"],
                       enforcement=exp.Enforcement.ADOPTED_RULE)
    store.put(advisory)
    store.put(enforced)

    assert store.get("advisory").enforcement is exp.Enforcement.ADVISORY
    assert store.get("enforced").enforcement is exp.Enforcement.ADOPTED_RULE


def test_a_rule_cannot_claim_enforcement_with_no_check(store):
    """Otherwise 'adopted project rule' is a sentence nothing backs."""
    with pytest.raises(ValueError, match="check"):
        _lesson(enforcement=exp.Enforcement.ADOPTED_RULE, check_refs=[])


# ── two timelines ────────────────────────────────────────────────────────────

def test_when_it_applied_is_not_when_we_learned_it(store):
    """A cause found today for a bug that shipped in March."""
    les = _lesson(valid_from="2026-03-04", known_from="2026-09-17")
    store.put(les)

    got = store.get("scope-write-001")
    assert got.valid_from == "2026-03-04"
    assert got.known_from == "2026-09-17"
    assert got.applied_at("2026-04-01"), "the bug was live in April"
    assert not got.was_known_at("2026-04-01"), (
        "the record claims April knew a cause that was found in September, "
        "which is how a past decision gets judged against evidence nobody had"
    )


def test_a_superseded_claim_is_kept_not_deleted(store):
    old = _lesson(lesson_id="old", valid_until="2026-09-17",
                  applicability=exp.Applicability.SUPERSEDED,
                  superseded_by="new")
    new = _lesson(lesson_id="new", valid_from="2026-09-18")
    store.put(old)
    store.put(new)

    assert store.get("old") is not None, "history was deleted rather than closed"
    assert store.get("old").superseded_by == "new"
    active = store.applicable(paths=["src/llm_router/okf.py"], at="2026-09-20")
    assert [r.lesson_id for r in active] == ["new"]


# ── contradictions ───────────────────────────────────────────────────────────

def test_conflicting_claims_both_surface_with_the_conflict_named(store):
    a = _lesson(lesson_id="a", statement="Always pass an explicit root.",
                known_from="2026-09-10")
    b = _lesson(lesson_id="b", statement="Never pass an explicit root.",
                known_from="2026-09-18", contradicts=["a"])
    store.put(a)
    store.put(b)

    conflicts = store.conflicts()
    assert len(conflicts) == 1
    pair = {conflicts[0][0].lesson_id, conflicts[0][1].lesson_id}
    assert pair == {"a", "b"}

    found = store.applicable(paths=["src/llm_router/okf.py"])
    assert {r.lesson_id for r in found} == {"a", "b"}, (
        "the newer claim silently won. A timestamp is not evidence, and two "
        "branches can hold different valid decisions at the same instant"
    )


# ── retrieval keys, before any structural index exists ───────────────────────

def test_records_are_found_by_path_and_by_symbol(store):
    store.put(_lesson())
    store.put(_lesson(lesson_id="unrelated",
                      affected_paths=["src/llm_router/budget.py"],
                      affected_symbols=["reserve"]))

    by_path = store.applicable(paths=["src/llm_router/okf.py"])
    assert {r.lesson_id for r in by_path} == {"scope-write-001"}

    by_symbol = store.applicable(symbols=["_write_source_concept"])
    assert {r.lesson_id for r in by_symbol} == {"scope-write-001"}

    assert store.applicable(paths=["src/llm_router/nothing.py"]) == [], (
        "an irrelevant lesson was offered; abstaining is the correct answer and "
        "a false warning has a real interruption cost"
    )


# ── persistence ──────────────────────────────────────────────────────────────

def test_a_record_survives_a_reload_with_every_axis_intact(tmp_path):
    root = tmp_path / "experience"
    exp.ExperienceStore(root=root).put(_lesson(
        review=exp.Review.REVIEWED,
        validation=exp.Validation.REPRODUCED,
        applicability=exp.Applicability.NEEDS_REVALIDATION,
        repair_status=exp.RepairStatus.PROPOSED,
        exceptions=["a command that only ever touches its own project"],
        evidence_ids=["probe:explicit-root-2026-09-17"],
    ))

    got = exp.ExperienceStore(root=root).get("scope-write-001")
    assert got.review is exp.Review.REVIEWED
    assert got.validation is exp.Validation.REPRODUCED
    assert got.applicability is exp.Applicability.NEEDS_REVALIDATION
    assert got.repair_status is exp.RepairStatus.PROPOSED
    assert got.exceptions == ["a command that only ever touches its own project"]
    assert got.evidence_ids == ["probe:explicit-root-2026-09-17"]


def test_the_readable_half_is_readable(tmp_path):
    """OKF-compatible markdown, because a human reads this when it matters.

    `library/store.py`'s YAML subset handles one nesting level, so the nested
    payload goes to a sidecar JSON rather than being flattened into prose that
    cannot be read back.
    """
    root = tmp_path / "experience"
    exp.ExperienceStore(root=root).put(_lesson(
        supported_mechanism="the writer recomputes its destination without the root",
    ))

    md = next(root.rglob("*.md"))
    text = md.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "scope-write-001" in text
    assert "Indexing project B" in text, "the statement is not in the body"
    assert next(root.rglob("*.json"), None) is not None, "no sidecar payload"


def test_an_unknown_state_value_is_refused_not_coerced(tmp_path):
    with pytest.raises(ValueError):
        _lesson(review="probably fine")
