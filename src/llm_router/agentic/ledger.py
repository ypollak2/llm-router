"""TaskLedger + Milestone data model for the MGEE engine.

The ledger is the durable checkpoint: passed milestones are *frozen* into the
done-frontier and never re-executed. Escalation resumes at the first pending
milestone, handing the stronger tier the frozen artifacts as read-only context.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llm_router.capabilities import RelevantContext


class MilestoneStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class AcceptanceResult:
    """Outcome of an objective acceptance check.

    ``deterministic`` distinguishes a real failure (escalate) from a flaky /
    non-reproducible one (re-run once, do not count against attempts).
    """

    ok: bool
    reason: str = ""
    deterministic: bool = True


# An acceptance check is an objective predicate over a milestone's artifacts.
AcceptanceCheck = Callable[[dict[str, Any]], AcceptanceResult]


@dataclass
class Attempt:
    tier: int
    ok: bool
    reason: str
    cost_usd: float = 0.0


@dataclass
class Milestone:
    id: str
    description: str
    acceptance: AcceptanceCheck
    deps: tuple[str, ...] = ()
    reversible: bool = True  # False → irreversible action, must pass the gate before DONE
    status: MilestoneStatus = MilestoneStatus.PENDING
    artifacts: dict[str, Any] = field(default_factory=dict)
    achieved_by: int | None = None  # tier that cleared it
    attempts: list[Attempt] = field(default_factory=list)


#: Hard ceiling on artifact text handed to a downstream milestone. An artifact is
#: agent output and can be arbitrarily large; an unbounded one would push the real
#: task out of the context window, which fails as "the agent ignored its
#: instructions" rather than as a size error.
_MAX_ARTIFACT_CHARS = 2000


def _render_artifacts(artifacts: dict) -> str:
    """Flatten an artifact map to bounded text for the delegated prompt."""
    parts: list[str] = []
    for key, value in sorted(artifacts.items()):
        text = str(value)
        if len(text) > _MAX_ARTIFACT_CHARS:
            text = text[:_MAX_ARTIFACT_CHARS] + f"… [truncated, {len(text)} chars total]"
        parts.append(f"{key}: {text}")
    return "\n".join(parts)


@dataclass
class TaskLedger:
    """Ordered milestones + the frozen done-frontier + budget/tier cursor."""

    goal: str
    milestones: list[Milestone]
    current_tier: int = 0
    budget_cap_usd: float = 1.0
    spent_usd: float = 0.0
    # P1-S2 (Known Limit A): conversation context from the calling session, handed
    # to every delegated agent via frozen_context() so a routed model isn't blind
    # to what was discussed (not just its own milestones).
    session_context: str = ""
    # CF-2 §7.5: capability-provisioned relevant context (candidate files, repo state).
    # Stored at the LEDGER level, NOT inside session_context, so it is NOT subject to
    # session_context's 2000-char truncation and SURVIVES tier escalation unchanged.
    relevant_context: "RelevantContext | None" = None

    # ── frontier ────────────────────────────────────────────────────────────
    @property
    def done_ids(self) -> set[str]:
        return {m.id for m in self.milestones if m.status is MilestoneStatus.DONE}

    def complete(self) -> bool:
        return all(m.status is MilestoneStatus.DONE for m in self.milestones)

    def next_pending(self) -> Milestone | None:
        """Earliest *ready* PENDING milestone (all deps done).

        DAG-aware: BLOCKED nodes and their unreachable dependents are skipped, so
        a stuck milestone never stalls ready independent siblings.
        """
        done = self.done_ids
        for m in self.milestones:
            if m.status is MilestoneStatus.PENDING and all(d in done for d in m.deps):
                return m
        return None

    def freeze(self, m: Milestone, tier: int, artifacts: dict[str, Any]) -> None:
        """Mark a verified milestone DONE — it is never executed again."""
        m.status = MilestoneStatus.DONE
        m.achieved_by = tier
        m.artifacts = dict(artifacts)

    def frozen_context(self) -> list[dict[str, Any]]:
        """Read-only view of achieved milestones handed to an escalated tier so
        it resumes at the frontier instead of redoing completed work."""
        from llm_router.prompt_injection import wrap_untrusted_context

        frozen = [
            {"id": m.id, "description": m.description,
             "achieved_by": m.achieved_by, "artifacts": m.artifacts,
             # RED3-06: pack_prompt renders these so a later milestone can build
             # on what an earlier one PRODUCED, not merely learn that it ran.
             # RED6-02: artifacts are agent output — untrusted by construction —
             # so they are neutralised HERE, alongside the other context blocks,
             # rather than at the render site. Wrapping at the point of rendering
             # would let an escalated tier that packs its own prompt route around
             # it, which is the whole reason the other two are wrapped here.
             "artifacts_rendered": wrap_untrusted_context(
                 _render_artifacts(m.artifacts), f"ARTIFACTS FROM {m.id}"
             ) if m.artifacts else ""}
            for m in self.milestones
            if m.status is MilestoneStatus.DONE
        ]
        # RED6-02 (P0): both context blocks are UNTRUSTED and are neutralised
        # here — the last point before pack_prompt renders them into a delegated
        # prompt. Doing it here rather than at each caller means an escalation to
        # a different tier cannot route around it: every tier takes its context
        # from this one method.
        from llm_router.prompt_injection import wrap_untrusted_context

        if self.session_context:
            # Prepended, distinct id — pack_prompt renders it as conversation
            # context, NOT as a completed milestone.
            frozen.insert(0, {"id": "SESSION_CONTEXT",
                              "description": wrap_untrusted_context(
                                  self.session_context, "CONVERSATION CONTEXT"),
                              "achieved_by": None, "artifacts": {}})
        if self.relevant_context is not None:
            # CF-2: a separate RELEVANT_CONTEXT entry (candidate files / repo state),
            # prepended ahead of SESSION_CONTEXT. Ledger-level, so it survives
            # escalation and is not truncated with conversation history.
            #
            # The sharper risk of the two: this is literal repository content,
            # and on the delegation path the repository is precisely the thing
            # the user may not control.
            from llm_router.capabilities import serialize_relevant_context
            frozen.insert(0, {"id": "RELEVANT_CONTEXT",
                              "description": wrap_untrusted_context(
                                  serialize_relevant_context(self.relevant_context),
                                  "REPOSITORY CONTEXT"),
                              "achieved_by": None, "artifacts": {}})
        return frozen

    def remaining(self) -> list[Milestone]:
        return [m for m in self.milestones if m.status is not MilestoneStatus.DONE]

    def charge(self, cost_usd: float) -> None:
        self.spent_usd += max(0.0, cost_usd)

    def budget_left(self) -> float:
        return max(0.0, self.budget_cap_usd - self.spent_usd)
