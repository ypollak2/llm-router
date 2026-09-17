"""Engineering experience as records that cannot launder a guess into a fact.

The library already keeps durable facts: flat sentences with a commit and a
date. That is enough to remember THAT something was decided and useless for
deciding whether it still applies. A bullet cannot say "this was the right call
on the 3.10 branch, we never reproduced the cause, and the check that would
catch a recurrence was never written" — and that sentence is the entire value
of remembering the incident at all.

Every field here exists because collapsing it produced a specific wrong answer.

TWO TIMELINES

When a claim applies in the project is not when we learned it. A cause found
today for a bug that shipped in March is `valid_from` March and `known_from`
today. With one timeline you cannot ask "what did we believe in April?", which
is exactly the question you ask when April's decision looks inexplicable — and
without it you judge a past decision against evidence nobody had.

FOUR INDEPENDENT AXES

    review          did a person look at it
    validation      did reality
    applicability   does it still hold
    enforcement     does anything actually stop a recurrence

These are four questions, not four values of one. An accepted design decision
can have zero empirical validation. A reproduced bug can be fixed while its
prevention lesson stays active. An unresolved issue must stay discoverable
without being presented as an established root cause. Collapse them into a
single `status` and every one of those states renders as "known good", which is
the failure mode that makes a memory system worse than no memory system.

DIAGNOSIS IS NOT REPAIR

`supported_mechanism` says what the evidence supports. `repair_status` says
what was done about it. A bug that is understood and unfixed must not read like
a bug that is fixed, and the same record has to be useful in both states —
before a fix, it tells the next person what is still unverified.

CONTRADICTIONS SURFACE

Newest-wins is a truth rule and a timestamp is not evidence. Two maintenance
branches hold different valid decisions at the same wall-clock instant. When
claims conflict, both come back with their provenance and the conflict is
named. A resolver may propose supersession; absent authoritative evidence the
system shows the conflict rather than picking.

WHAT THIS IS NOT, YET

Records key on `(path, symbol, commit range)` — deliberately not an entity id,
because the structural index does not exist yet and inventing a foreign key to
a table nobody has written is how two layers ship as disconnected islands. The
indexer backfills the link when it lands.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class Review(str, Enum):
    """Did a person look at it."""
    EXTRACTED = "extracted"
    REVIEWED = "reviewed"
    DISPUTED = "disputed"
    REJECTED = "rejected"


class Validation(str, Enum):
    """Did reality. Separate from review: agreeing is not observing."""
    UNTESTED = "untested"
    SUPPORTED = "supported-by-observation"
    REPRODUCED = "reproduced"
    CONTRADICTED = "contradicted"


class Applicability(str, Enum):
    """Does it still hold."""
    ACTIVE = "active"
    NEEDS_REVALIDATION = "needs-revalidation"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class Enforcement(str, Enum):
    """Does anything actually stop a recurrence.

    ADVISORY is a warning a reader may ignore. ADOPTED_RULE claims something
    fails when it is broken, so it requires a check reference — otherwise the
    phrase is a sentence with nothing behind it.
    """
    ADVISORY = "advisory"
    SELECTED_CHECK = "selected-check"
    ADOPTED_RULE = "adopted-project-rule"


class RepairStatus(str, Enum):
    """What was done, held apart from what is understood."""
    NONE = "none"
    PROPOSED = "proposed"
    ATTEMPTED = "attempted"
    VERIFIED = "verified"
    REVERTED = "reverted"


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _coerce(enum_cls, value, field_name: str):
    """Enum or its exact value. Never a guess.

    A memory store that accepts "probably fine" as a review state and files it
    under something has invented a fact, which is the one thing this module
    exists to prevent.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        raise ValueError(
            f"{field_name}={value!r} is not a {enum_cls.__name__}; "
            f"valid: {[e.value for e in enum_cls]}"
        ) from None


@dataclass(frozen=True)
class _Record:
    """Fields every experience record carries, whatever kind it is."""

    statement: str = ""

    # Timeline one: when this applies in the project.
    valid_from: str = ""
    valid_until: str = ""
    # Timeline two: when this system learned or revised it.
    known_from: str = ""
    known_until: str = ""

    review: Review = Review.EXTRACTED
    validation: Validation = Validation.UNTESTED
    applicability: Applicability = Applicability.ACTIVE
    enforcement: Enforcement = Enforcement.ADVISORY

    # Retrieval keys. Not an entity id — see the module docstring.
    affected_paths: list[str] = field(default_factory=list)
    affected_symbols: list[str] = field(default_factory=list)
    commit_range: str = ""

    evidence_ids: list[str] = field(default_factory=list)
    check_refs: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)
    superseded_by: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # frozen, so validated values go back through object.__setattr__
        for name, enum_cls in (("review", Review), ("validation", Validation),
                               ("applicability", Applicability),
                               ("enforcement", Enforcement)):
            object.__setattr__(
                self, name, _coerce(enum_cls, getattr(self, name), name))
        for name in ("valid_from", "valid_until", "known_from", "known_until"):
            value = getattr(self, name)
            if value and not _DATE.match(value):
                raise ValueError(f"{name}={value!r} is not an ISO date (YYYY-MM-DD)")
        if self.enforcement is Enforcement.ADOPTED_RULE and not self.check_refs:
            raise ValueError(
                "enforcement=adopted-project-rule with no check_refs: a rule "
                "that nothing fails on is a warning wearing a rule's name"
            )

    # -- the four axes, asked one at a time ---------------------------------

    def is_established(self) -> bool:
        """Reviewed AND observed. Either alone is not an established fact."""
        return (self.review is Review.REVIEWED
                and self.validation in (Validation.SUPPORTED, Validation.REPRODUCED))

    def is_repaired(self) -> bool:
        return getattr(self, "repair_status", RepairStatus.NONE) is RepairStatus.VERIFIED

    # -- the two timelines --------------------------------------------------

    def applied_at(self, date: str) -> bool:
        """Was this true OF THE PROJECT on *date*."""
        if self.valid_from and date < self.valid_from:
            return False
        if self.valid_until and date > self.valid_until:
            return False
        return True

    def was_known_at(self, date: str) -> bool:
        """Had this system LEARNED it by *date*.

        Distinct from `applied_at` on purpose: judging an old decision against
        evidence that did not exist yet is the mistake this separation blocks.
        """
        if self.known_from and date < self.known_from:
            return False
        if self.known_until and date > self.known_until:
            return False
        return True


@dataclass(frozen=True)
class Lesson(_Record):
    """Something that went wrong, and what would catch it next time."""
    lesson_id: str = ""
    failure_family: str = ""
    triggers: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    supported_mechanism: str = ""
    suggested_action: str = ""
    repair_status: RepairStatus = RepairStatus.NONE

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self, "repair_status",
            _coerce(RepairStatus, self.repair_status, "repair_status"))
        if not self.lesson_id:
            raise ValueError("a lesson needs an id to be superseded or cited by")


@dataclass(frozen=True)
class Decision(_Record):
    """A choice, the options it beat, and why they lost.

    A rejected option keeps its reason, because the constraint behind it can
    disappear — and when it does, the decision should be revisited rather than
    inherited.
    """
    decision_id: str = ""
    alternatives: list[str] = field(default_factory=list)
    rejected_because: dict[str, str] = field(default_factory=dict)
    decided_by: str = ""
    consequences: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.decision_id:
            raise ValueError("a decision needs an id to be superseded or cited by")


@dataclass(frozen=True)
class Problem(_Record):
    """A symptom and its candidate causes, held apart from any accepted one."""
    problem_id: str = ""
    symptom: str = ""
    impact: str = ""
    failure_signature: str = ""
    cause_hypotheses: list[str] = field(default_factory=list)
    supported_mechanism: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.problem_id:
            raise ValueError("a problem needs an id")


@dataclass(frozen=True)
class Attempt(_Record):
    """What was tried, and what actually happened — including nothing."""
    attempt_id: str = ""
    attempted_for: str = ""
    change_summary: str = ""
    outcome: str = "unknown"
    repair_status: RepairStatus = RepairStatus.NONE

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self, "repair_status",
            _coerce(RepairStatus, self.repair_status, "repair_status"))
        if not self.attempt_id:
            raise ValueError("an attempt needs an id")


@dataclass(frozen=True)
class PerformanceObservation(_Record):
    """A measurement is only a measurement with its conditions attached.

    Hardware, concurrency, workload hash, warm/cold cache, sample count and
    units, because "this path is fast" expires the moment any of them changes
    and a claim that cannot say what it ran on cannot be compared with anything.
    """
    experiment_id: str = ""
    metric: str = ""
    value: float = 0.0
    unit: str = ""
    sample_count: int = 0
    environment: str = ""
    workload_hash: str = ""
    baseline_id: str = ""
    uncertainty: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.experiment_id:
            raise ValueError("a performance observation needs an id")
        if self.sample_count and not self.unit:
            raise ValueError("a measured value with no unit is not comparable")


@dataclass(frozen=True)
class FeatureTransition(_Record):
    """Merged never implies deployed, and local-green never implies healthy."""
    transition_id: str = ""
    feature: str = ""
    from_state: str = ""
    to_state: str = ""
    environment: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.transition_id:
            raise ValueError("a transition needs an id")


_KINDS: dict[str, type] = {
    "Lesson": Lesson,
    "Decision": Decision,
    "Problem": Problem,
    "Attempt": Attempt,
    "PerformanceObservation": PerformanceObservation,
    "FeatureTransition": FeatureTransition,
}

_ID_FIELD = {
    "Lesson": "lesson_id",
    "Decision": "decision_id",
    "Problem": "problem_id",
    "Attempt": "attempt_id",
    "PerformanceObservation": "experiment_id",
    "FeatureTransition": "transition_id",
}

_ID_SAFE = re.compile(r"[^\w.-]")


def record_id(record: Any) -> str:
    return str(getattr(record, _ID_FIELD[type(record).__name__]))


class ExperienceStore:
    """Records on disk: readable markdown, exact JSON beside it.

    Two files per record on purpose. The markdown is what a person opens when
    an incident recurs at 2am, and it is OKF-shaped so existing retrieval can
    already see it. The JSON is the one that round-trips — `library/store.py`'s
    YAML subset handles a single nesting level, and flattening
    `rejected_because` into prose loses the mapping that makes a rejected
    option re-examinable.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # -- paths --------------------------------------------------------------

    def _dir_for(self, kind: str) -> Path:
        return self.root / {
            "Lesson": "lessons",
            "Decision": "decisions",
            "Problem": "problems",
            "Attempt": "attempts",
            "PerformanceObservation": "performance",
            "FeatureTransition": "transitions",
        }[kind]

    # -- write --------------------------------------------------------------

    def put(self, record: Any) -> Path:
        kind = type(record).__name__
        rid = _ID_SAFE.sub("_", record_id(record))
        out = self._dir_for(kind)
        out.mkdir(parents=True, exist_ok=True)

        payload = {k: (v.value if isinstance(v, Enum) else v)
                   for k, v in asdict(record).items()}
        payload["kind"] = kind
        (out / f"{rid}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

        md = out / f"{rid}.md"
        md.write_text(self._render(record, kind, rid), encoding="utf-8")
        return md

    def _render(self, record: Any, kind: str, rid: str) -> str:
        """OKF-compatible markdown. The four axes are on the face of it.

        A reader must not have to open the JSON to find out that the diagnosis
        was never reproduced.
        """
        head = [
            "---",
            f"type: Experience{kind}",
            f"title: {rid}",
            f"description: {record.statement[:120]}",
            f"review: {record.review.value}",
            f"validation: {record.validation.value}",
            f"applicability: {record.applicability.value}",
            f"enforcement: {record.enforcement.value}",
            f"valid_from: {record.valid_from or 'unknown'}",
            f"known_from: {record.known_from or 'unknown'}",
            f"tags: [experience, {kind.lower()}]",
            "---",
            "",
            record.statement or f"{kind} {rid}",
            "",
        ]
        if record.affected_paths:
            head.append("Affected: " + ", ".join(record.affected_paths))
        if record.affected_symbols:
            head.append("Symbols: " + ", ".join(record.affected_symbols))
        mech = getattr(record, "supported_mechanism", "")
        if mech:
            head.append(f"Supported mechanism: {mech}")
        repair = getattr(record, "repair_status", None)
        if repair is not None:
            head.append(f"Repair status: {repair.value}")
        if record.check_refs:
            head.append("Prevention checks: " + ", ".join(record.check_refs))
        else:
            head.append("Prevention checks: none — this is advisory only")
        if getattr(record, "exceptions", None):
            head.append("Exceptions: " + "; ".join(record.exceptions))
        if record.contradicts:
            head.append("Contradicts: " + ", ".join(record.contradicts))
        if record.superseded_by:
            head.append(f"Superseded by: {record.superseded_by}")
        return "\n".join(head) + "\n"

    # -- read ---------------------------------------------------------------

    def _load(self, path: Path) -> Any | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        kind = payload.pop("kind", "")
        cls = _KINDS.get(kind)
        if cls is None:
            return None
        known = {f.name for f in fields(cls)}
        # Drop anything this version does not know rather than raising: a record
        # written by a newer schema is still worth most of its content.
        return cls(**{k: v for k, v in payload.items() if k in known})

    def all(self) -> list[Any]:
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.rglob("*.json")):
            record = self._load(path)
            if record is not None:
                out.append(record)
        return out

    def get(self, record_identifier: str) -> Any | None:
        safe = _ID_SAFE.sub("_", record_identifier)
        for path in sorted(self.root.rglob(f"{safe}.json")):
            record = self._load(path)
            if record is not None:
                return record
        return None

    # -- retrieval ----------------------------------------------------------

    def applicable(
        self,
        paths: list[str] | None = None,
        symbols: list[str] | None = None,
        at: str | None = None,
        include_inactive: bool = False,
    ) -> list[Any]:
        """Records that apply to this work, and nothing else.

        Abstaining is a correct answer. A false warning costs a real
        interruption, and a system that offers something for every task teaches
        people to skip all of it.
        """
        want_paths = set(paths or [])
        want_symbols = set(symbols or [])
        out = []
        for record in self.all():
            if not include_inactive and record.applicability in (
                Applicability.SUPERSEDED, Applicability.RETIRED
            ):
                continue
            if record.review is Review.REJECTED:
                continue
            if at is not None and not record.applied_at(at):
                continue
            if want_paths or want_symbols:
                if not (want_paths & set(record.affected_paths)
                        or want_symbols & set(record.affected_symbols)):
                    continue
            out.append(record)
        return sorted(out, key=record_id)

    def conflicts(self) -> list[tuple[Any, Any]]:
        """Pairs that disagree, surfaced rather than resolved.

        A timestamp is not evidence. Two maintenance branches can hold
        different valid decisions at the same instant, so the honest move is to
        show both with their provenance and let a person judge. Supersession is
        a separate, explicit act — see `superseded_by`.
        """
        by_id = {record_id(r): r for r in self.all()}
        seen: set[tuple[str, str]] = set()
        out = []
        for rid, record in sorted(by_id.items()):
            for other_id in record.contradicts:
                other = by_id.get(other_id)
                if other is None:
                    continue
                key = tuple(sorted((rid, other_id)))
                if key in seen:
                    continue
                seen.add(key)
                out.append((record, other))
        return out
