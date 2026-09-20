"""Verifier lifecycle: PROPOSED → VALIDATED → APPROVED → ACTIVE.

The gate this file exists to hold: **a generated verifier never becomes ACTIVE
on its own.** Two independent things must happen, in order.

    VALIDATED   evidence — it passed a known-good implementation and failed
                broken ones. Machine-checkable, and `mark_validated` refuses
                without it.
    APPROVED    judgement — a person looked and said yes. Nothing computes this.

Keeping them separate matters because they fail differently. A verifier can be
demonstrably discriminating and still be checking the wrong thing; only a human
catches that. And a human can wave through a verifier that has never been run;
only the evidence gate catches that. Requiring both is the point.

`ACTIVE` is what `run_matrix` may execute. Anything else is a draft.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import mutants as mut  # noqa: E402
from groundtruth import propose as pr  # noqa: E402

SCHEMA_VERSION = 1

PROPOSED = "PROPOSED"
VALIDATED = "VALIDATED"
APPROVED = "APPROVED"
ACTIVE = "ACTIVE"
REJECTED = "REJECTED"

_ALLOWED: dict[str, frozenset[str]] = {
    PROPOSED: frozenset({VALIDATED, REJECTED}),
    VALIDATED: frozenset({APPROVED, REJECTED}),
    APPROVED: frozenset({ACTIVE, REJECTED}),
    ACTIVE: frozenset({REJECTED}),
    REJECTED: frozenset({PROPOSED}),      # a fixed proposal may re-enter
}


@dataclass
class Transition:
    at: float
    frm: str
    to: str
    reason: str
    actor: str = "assistant"


@dataclass
class VerifierRecord:
    task_id: str
    schema_version: int = SCHEMA_VERSION
    status: str = PROPOSED
    proposal: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    confidence: str = mut.UNUSABLE
    history: list[Transition] = field(default_factory=list)
    created_at: float = 0.0
    approved_by: str | None = None

    @property
    def strategy(self) -> str:
        return self.proposal.get("verification_strategy", pr.S_NONE)

    @property
    def is_mechanical(self) -> bool:
        return self.strategy in pr.MECHANICAL_STRATEGIES

    def _move(self, to: str, reason: str, actor: str) -> bool:
        if to not in _ALLOWED.get(self.status, frozenset()):
            return False
        self.history.append(Transition(time.time(), self.status, to, reason, actor))
        self.status = to
        return True

    def mark_validated(self, validation: mut.Validation, *,
                       contract_complete: bool) -> tuple[bool, str]:
        """Record evidence and set confidence from it. Never from a claim.

        Refuses to advance when the validation shows the verifier cannot
        discriminate — that is not a judgement call, and allowing an override
        here would make every downstream confidence figure meaningless.
        """
        conf, why = mut.classify(validation,
                                 strategy_is_mechanical=self.is_mechanical,
                                 contract_complete=contract_complete)
        self.validation = validation.to_json()
        self.confidence = conf
        if conf == mut.UNUSABLE:
            self._move(REJECTED, f"validation failed: {why}", "validator")
            return False, why
        if not self._move(VALIDATED, f"validated: {why}", "validator"):
            return False, f"cannot validate from status {self.status}"
        return True, why

    def approve(self, actor: str, reason: str = "") -> tuple[bool, str]:
        """Human approval. Requires evidence first — no shortcut from PROPOSED."""
        if self.status == PROPOSED:
            return False, ("this verifier has not been validated; approving an "
                           "unrun verifier is how an unusable one becomes trusted")
        if not actor or actor == "assistant":
            return False, "approval requires a human actor"
        if not self._move(APPROVED, reason or "approved after review", actor):
            return False, f"cannot approve from status {self.status}"
        self.approved_by = actor
        return True, "approved"

    def activate(self, actor: str) -> tuple[bool, str]:
        if not self._move(ACTIVE, "activated for Ground Truth evaluation", actor):
            return False, f"cannot activate from status {self.status}"
        return True, "active"

    def reject(self, actor: str, reason: str) -> tuple[bool, str]:
        if not self._move(REJECTED, reason, actor):
            return False, f"cannot reject from status {self.status}"
        return True, "rejected"

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "VerifierRecord":
        hist = [Transition(**t) for t in d.get("history", [])]
        known = {k: v for k, v in d.items()
                 if k in cls.__dataclass_fields__ and k != "history"}
        r = cls(**known)
        r.history = hist
        return r


def default_path() -> Path:
    home = Path(os.environ.get("LLM_ROUTER_HOME", Path.home() / ".llm-router"))
    return home / "verifiers.jsonl"


class Registry:
    """Append-only verifier store. Latest row per task_id wins."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self._index: dict[str, VerifierRecord] = {}
        self.load()

    def load(self) -> None:
        self._index.clear()
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = VerifierRecord.from_json(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
            self._index[r.task_id] = r

    def save(self, rec: VerifierRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.to_json(), ensure_ascii=False, sort_keys=True) + "\n")
        self._index[rec.task_id] = rec

    def get(self, task_id: str) -> VerifierRecord | None:
        return self._index.get(task_id)

    def all(self) -> list[VerifierRecord]:
        return list(self._index.values())

    def in_status(self, *statuses: str) -> list[VerifierRecord]:
        return [r for r in self._index.values() if r.status in statuses]

    def active_for(self, task_id: str) -> VerifierRecord | None:
        """The only accessor `run_matrix` should use. ACTIVE or nothing."""
        r = self._index.get(task_id)
        return r if r and r.status == ACTIVE else None

    def stats(self, candidate_total: int = 0) -> dict:
        recs = self.all()
        by_status = Counter(r.status for r in recs)
        by_conf = Counter(r.confidence for r in recs if r.status != REJECTED)
        by_strategy = Counter(r.strategy for r in recs if r.status != REJECTED)
        mechanical = sum(1 for r in recs
                         if r.is_mechanical and r.status in (VALIDATED, APPROVED, ACTIVE))
        blockers: Counter[str] = Counter()
        for r in recs:
            for b in (r.proposal.get("blockers") or []):
                blockers[b] += 1
        total = candidate_total or len(recs)
        return {
            "candidates": total,
            "verifiers": len(recs),
            "no_verifier_generated": max(0, total - len(recs)),
            "by_status": dict(by_status),
            "by_confidence": dict(by_conf),
            "by_strategy": dict(by_strategy),
            "mechanical_coverage": (mechanical / total) if total else 0.0,
            "top_blockers": dict(blockers.most_common(6)),
        }


# ── Part 12: prioritisation ─────────────────────────────────────────────────

def priority(candidate, registry: Registry, *,
             type_counts: dict[str, int] | None = None) -> tuple[float, list[str]]:
    """(score, why). Higher is more worth authoring next.

    Ordered by what makes a verifier cheap to write and valuable to have, not by
    capture order. The underrepresentation term is what stops the queue filling
    the pool with fifty variants of the same easy task.
    """
    score = 0.0
    why: list[str] = []

    if candidate.replay_ready:
        score += 40
        why.append("replay-ready")
    if candidate.verifier_class in ("mechanical", "programmatic", "sandbox"):
        score += 30
        why.append(f"likely mechanical ({candidate.verifier_class})")
    if candidate.needs_reference_answer:
        score -= 20
        why.append("needs a human-established reference answer")
    if candidate.residual_flags:
        score -= 15
        why.append("flagged for privacy review")

    counts = type_counts or {}
    if counts:
        n = counts.get(candidate.task_type or "unknown", 0)
        smallest = min(counts.values()) if counts else 0
        if n <= smallest:
            score += 20
            why.append(f"underrepresented type ({candidate.task_type}: {n})")

    # Reuse: a task shaped like ones already solved is cheap to author.
    reuse = sum(1 for r in registry.all()
                if r.status in (APPROVED, ACTIVE)
                and r.proposal.get("acceptance_contract", {}).get("template")
                == _template_of(candidate))
    if reuse:
        score += min(15, 5 * reuse)
        why.append(f"{reuse} approved verifier(s) share its template")

    if candidate.duplicate_count > 1:
        score += min(10, candidate.duplicate_count)
        why.append(f"seen {candidate.duplicate_count}x — representative traffic")

    if registry.get(candidate.task_id):
        score -= 100
        why.append("already has a verifier record")
    return score, why


def _template_of(candidate) -> str | None:
    from groundtruth import contract as ct
    prompt = (candidate.envelope or {}).get("prompt") or ""
    t = ct.pick_template(prompt)
    return t.name if t else None


def rank(candidates, registry: Registry) -> list[tuple[float, object, list[str]]]:
    counts = Counter(c.task_type or "unknown" for c in candidates)
    scored = [(priority(c, registry, type_counts=counts)[0], c,
               priority(c, registry, type_counts=counts)[1]) for c in candidates]
    return sorted(scored, key=lambda x: (-x[0], str(getattr(x[1], "task_id", ""))))
