"""Which remembered things apply to the work in front of you, and which do not.

Two failure modes, opposite and equally bad.

Warn about everything and the warnings stop being read. A store that has
something to say about every task has taught its reader to skip all of it, and
the one time it matters the advice is in a block they have learned to scroll
past. Abstaining is a correct answer, and it is the common one.

Warn about nothing and the store is decoration. The lesson that would have
caught today's bug was in there, and nothing surfaced it.

So matching is narrow — a record must name the file or the symbol being worked
on — and the states that make a record inapplicable are checked explicitly
rather than left to a relevance score:

    superseded / retired    replaced or abandoned; still readable, not offered
    rejected                reviewed and found wrong
    outside its valid range a lesson about code that no longer exists that way

Contradictions are NOT a state. Two records that disagree are both offered,
with the disagreement named, because a timestamp is not evidence and two
branches hold different valid decisions at the same instant. Picking one is a
judgement, and the system does not have the standing to make it silently.
"""
from __future__ import annotations

from dataclasses import dataclass

from llm_router.semantic import experience as exp


@dataclass(frozen=True)
class ApplicableLesson:
    """A record and the reason it was selected — so a reader can disagree."""

    record: object
    matched_paths: list[str]
    matched_symbols: list[str]

    @property
    def why(self) -> str:
        parts = []
        if self.matched_symbols:
            parts.append("symbol " + ", ".join(self.matched_symbols))
        if self.matched_paths:
            parts.append("file " + ", ".join(self.matched_paths))
        return "this change touches " + " and ".join(parts) if parts else "scope match"


def select(
    store: exp.ExperienceStore,
    paths: list[str] | None = None,
    symbols: list[str] | None = None,
    at: str | None = None,
    limit: int = 5,
) -> tuple[list[ApplicableLesson], list[dict]]:
    """Return (applicable records, named conflicts among them).

    *limit* is small on purpose. Three to five is what a person reads before a
    change; a list of twenty is a document, and a document is not a warning.
    A record dropped for the limit is reported by the caller as an omission
    rather than silently forgotten.
    """
    want_paths = set(paths or [])
    want_symbols = set(symbols or [])
    if not want_paths and not want_symbols:
        return [], []

    selected: list[ApplicableLesson] = []
    for record in store.all():
        if record.applicability in (exp.Applicability.SUPERSEDED,
                                    exp.Applicability.RETIRED):
            continue
        if record.review is exp.Review.REJECTED:
            continue
        if at is not None and not record.applied_at(at):
            continue

        hit_paths = sorted(want_paths & set(record.affected_paths))
        hit_symbols = sorted(want_symbols & set(record.affected_symbols))
        if not hit_paths and not hit_symbols:
            continue
        selected.append(ApplicableLesson(record, hit_paths, hit_symbols))

    # A record matched on a named symbol is more specific than one matched on
    # the file alone, and a reproduced cause outranks an untested guess.
    def rank(item: ApplicableLesson) -> tuple:
        validated = item.record.validation in (
            exp.Validation.REPRODUCED, exp.Validation.SUPPORTED)
        enforced = item.record.enforcement is not exp.Enforcement.ADVISORY
        return (
            0 if item.matched_symbols else 1,
            0 if enforced else 1,
            0 if validated else 1,
            exp.record_id(item.record),
        )

    selected.sort(key=rank)
    kept = selected[:limit]

    # Conflicts are computed over what was SELECTED: a disagreement with a
    # record that does not apply here is not this task's problem.
    kept_ids = {exp.record_id(item.record) for item in kept}
    conflicts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in kept:
        rid = exp.record_id(item.record)
        for other in item.record.contradicts:
            if other not in kept_ids:
                continue
            key = tuple(sorted((rid, other)))
            if key in seen:
                continue
            seen.add(key)
            conflicts.append({
                "a": key[0], "b": key[1],
                "note": "both apply and they disagree; a timestamp is not evidence",
            })
    return kept, conflicts
