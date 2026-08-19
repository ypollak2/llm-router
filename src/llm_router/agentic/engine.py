"""MGEE engine — the milestone-gated escalating execution loop.

Guarantees (see docs/agentic-router.md §5): monotonic escalation over a finite
tier ladder + bounded attempts per (milestone, tier) ⇒ always terminates as
COMPLETE or a *surfaced* failure — never an infinite loop, never a silent stall.
Passed milestones are frozen into the ledger and never re-executed.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from llm_router.agentic.ledger import (
    AcceptanceResult,
    Attempt,
    Milestone,
    MilestoneStatus,
    TaskLedger,
)


class Outcome(str, Enum):
    COMPLETE = "complete"          # every milestone verified done
    SURFACED = "surfaced"          # honest, specific failure handed to the user
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass
class AgentRunResult:
    artifacts: dict[str, Any]
    cost_usd: float = 0.0
    confidence: float = 1.0


class Agent(Protocol):
    tier: int

    def run(
        self, milestone: Milestone, frozen_context: list[dict[str, Any]], budget_left: float
    ) -> AgentRunResult:
        ...


EVENT_KINDS = frozenset(
    {"plan", "execute", "pass", "fail", "retry", "escalate", "surface", "complete"}
)


@dataclass
class Event:
    kind: str          # one of EVENT_KINDS
    milestone_id: str = ""
    tier: int = -1
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "milestone_id": self.milestone_id,
                "tier": self.tier, "reason": self.reason}

    def render(self) -> str:
        icon = {"plan": "🗺", "execute": "⚙", "pass": "✓", "fail": "✗",
                "retry": "↻", "escalate": "↑", "surface": "⚠",
                "complete": "✅"}.get(self.kind, "·")
        bits = [icon, self.kind, self.milestone_id]
        if self.tier >= 0:
            bits.append(f"t{self.tier}")
        if self.reason:
            bits.append(f"— {self.reason}")
        return " ".join(str(b) for b in bits if b != "")


def validate_event_stream(events: list[Event]) -> tuple[bool, str]:
    """A well-formed transparency stream: non-empty, opens with ``plan``, every
    kind is known, and it closes on a terminal ``complete`` or ``surface``."""
    if not events:
        return False, "empty stream"
    if events[0].kind != "plan":
        return False, f"stream must open with 'plan', got {events[0].kind!r}"
    for e in events:
        if e.kind not in EVENT_KINDS:
            return False, f"unknown event kind {e.kind!r}"
    if events[-1].kind not in ("complete", "surface"):
        return False, f"stream must close on complete|surface, got {events[-1].kind!r}"
    return True, ""


@dataclass
class TaskResult:
    outcome: Outcome
    ledger: TaskLedger
    events: list[Event] = field(default_factory=list)
    reason: str = ""


# route(milestone) -> starting tier.
Router = Callable[[Milestone], int]
# gate(milestone, result) -> True if an irreversible action is confirmed/safe to freeze.
Gate = Callable[[Milestone, AgentRunResult], bool]


def _refuse_unisolated_irreversible(milestone: Milestone, result: AgentRunResult) -> bool:
    """Default gate: reversible work freezes; irreversible work needs isolation.

    RED3-01 (P0). An irreversible milestone — push, merge, delete, external send
    — that ran straight in the working tree cannot be rolled back if its
    acceptance check turns out to be wrong, so it does not auto-freeze on a bare
    pass. It is surfaced instead.

    Callers that CAN isolate should pass ``reversibility_gate(ops)`` from
    llm_router.agentic.worktree, which merges the worktree when the milestone
    verified there and discards it otherwise. This default is the honest answer
    for callers that cannot: refuse, rather than pretend.
    """
    if milestone.reversible:
        return True
    return bool(result.artifacts.get("worktree"))


class MGEEEngine:
    def __init__(
        self,
        agents_by_tier: dict[int, Agent],
        *,
        max_attempts_per_tier: int = 2,
        router: Router | None = None,
        gate: Gate | None = None,
        event_sink: Callable[[Event], None] | None = None,
        workdir: str | None = None,
    ) -> None:
        if not agents_by_tier:
            raise ValueError("at least one tier agent is required")
        self.agents = dict(agents_by_tier)
        self.top_tier = max(self.agents)
        self.k = max(1, max_attempts_per_tier)
        self.router = router or (lambda _m: min(self.agents))
        # RED3-01 (P0): the default REFUSES to freeze irreversible work that was
        # not isolated. It used to be `lambda _m, _r: True` — approve everything.
        #
        # The gate mechanism was wired all along (see the call site in
        # _work_milestone); what made the README's "irreversible steps run in an
        # isolated git worktree, merged only after they verify" false was that
        # its default said yes and no caller ever supplied a real one. A
        # permissive default on a safety gate is indistinguishable from no gate,
        # and reads in review as though the protection is present.
        self.gate = gate or _refuse_unisolated_irreversible
        self.event_sink = event_sink
        # RED3-08: the directory the agents actually work in, threaded to the
        # acceptance check so a repository-reading check looks at the right tree.
        self.workdir = workdir
        self.events: list[Event] = []

    def _emit(self, kind: str, m: str = "", tier: int = -1, reason: str = "") -> None:
        ev = Event(kind, m, tier, reason)
        self.events.append(ev)
        if self.event_sink:
            self.event_sink(ev)

    def _start_tier(self, m: Milestone) -> int:
        # clamp the router's choice into the available ladder
        t = self.router(m)
        return min(max(t, min(self.agents)), self.top_tier)

    def _attempts_at(self, m: Milestone, tier: int) -> int:
        return sum(1 for a in m.attempts if a.tier == tier)

    def _verify(self, m: Milestone, artifacts: dict[str, Any]) -> AcceptanceResult:
        # RED3-03 (P0): reject a do-nothing oracle before trusting it. A
        # `return True` stub submitted as the acceptance check for a
        # security-hole task was ACCEPTED and the milestone recorded DONE — at
        # which point "done" means "the executor said so", the one property this
        # whole design exists to rule out.
        #
        # Enforced here, at the single point every milestone is verified, rather
        # than in the check factories: a stub does not come from acceptance.py's
        # factories, it comes from an executor asked to supply its own check.
        # Guarding anywhere else would leave the path that actually produces
        # stubs unguarded.
        from llm_router.agentic.acceptance import reject_stubs

        # RED3-08 (P0): the working directory reaches the check. Without it a
        # repository-reading check resolves `cwd=None` to the PROCESS's cwd —
        # which, when llm_router is run from its own checkout, is a different git
        # repo entirely. It would then verify the milestone against LLM Router's
        # source tree instead of the agent's, and confidently report the wrong
        # answer in whichever direction that tree happened to point.
        if "cwd" not in artifacts and self.workdir is not None:
            artifacts = {**artifacts, "cwd": self.workdir}

        try:
            return reject_stubs(m.acceptance)(artifacts)
        except Exception as exc:  # noqa: BLE001 — a broken check must never hang the flow
            return AcceptanceResult(False, f"acceptance check errored: {exc}", deterministic=True)

    def run(self, ledger: TaskLedger) -> TaskResult:
        """Drive milestones to completion. Stuck milestones are *quarantined*
        (BLOCKED) and the flow continues with ready siblings — so the process
        never stalls; blocked work is surfaced together at the end."""
        self.events = []
        self._emit("plan", reason=f"{len(ledger.milestones)} milestones")
        blocked: list[str] = []

        while True:
            m = ledger.next_pending()
            if m is None:
                break  # nothing ready — either complete, or only blocked/unreachable remain
            tier = self._start_tier(m)
            m.status = MilestoneStatus.IN_PROGRESS
            status, reason = self._work_milestone(ledger, m, tier)
            if status == "budget":
                return self._budget(ledger, m)
            if status == "blocked":
                m.status = MilestoneStatus.BLOCKED
                blocked.append(f"{m.id}: {reason}")
                self._emit("surface", m.id, reason=reason)
            # "done" → just continue the outer loop

        if ledger.complete():
            self._emit("complete")
            return TaskResult(Outcome.COMPLETE, ledger, list(self.events))
        reason = "; ".join(blocked) or "unresolved milestones (unreachable dependencies)"
        self._emit("surface", reason=reason)  # terminal event → stream closes on 'surface'
        return TaskResult(Outcome.SURFACED, ledger, list(self.events), reason)

    def _work_milestone(
        self, ledger: TaskLedger, m: Milestone, tier: int
    ) -> tuple[str, str]:
        """Attempt/escalation loop for ONE milestone (bounded ⇒ terminates).

        Returns (status, reason): 'done' | 'blocked' | 'budget'.
        """
        while True:
            if ledger.budget_left() <= 0:
                return "budget", "budget exhausted"
            agent = self.agents[tier]
            self._emit("execute", m.id, tier)
            res, run = self._run_and_verify(agent, m, ledger, tier)
            m.attempts.append(Attempt(tier, res.ok, res.reason, run.cost_usd))

            if res.ok:
                if not m.reversible and not self.gate(m, run):
                    return "blocked", f"irreversible milestone '{m.id}' needs confirmation"
                ledger.freeze(m, tier, run.artifacts)
                self._emit("pass", m.id, tier, res.reason)
                return "done", ""

            self._emit("fail", m.id, tier, res.reason)
            if self._attempts_at(m, tier) < self.k:
                self._emit("retry", m.id, tier)
                continue
            if tier < self.top_tier:
                tier += 1  # monotonic escalation, frozen ledger carried forward
                self._emit("escalate", m.id, tier, res.reason)
                continue
            # WP-10: the replan path is deleted, not disabled. It worked when
            # called and had NO production caller -- run_delegation threaded
            # replan_fn through to here and nothing ever supplied one; a single
            # test was its only caller. Dead safety code reads as coverage, and
            # this codebase has already had such a path silently wired back up
            # (RED3-01). Exhausting the ladder now blocks, which is what actually
            # happened in production the whole time.
            return "blocked", res.reason

    def _run_and_verify(
        self, agent: Agent, m: Milestone, ledger: TaskLedger, tier: int
    ) -> tuple[AcceptanceResult, AgentRunResult]:
        run = agent.run(m, ledger.frozen_context(), ledger.budget_left())
        ledger.charge(run.cost_usd)
        res = self._verify(m, run.artifacts)
        # flaky / non-reproducible failure → re-run once, do NOT escalate on it
        if not res.ok and not res.deterministic and ledger.budget_left() > 0:
            run = agent.run(m, ledger.frozen_context(), ledger.budget_left())
            ledger.charge(run.cost_usd)
            res = self._verify(m, run.artifacts)
        return res, run

    def _budget(self, ledger: TaskLedger, m: Milestone) -> TaskResult:
        self._emit("surface", m.id, reason="budget exhausted — partial progress returned")
        return TaskResult(
            Outcome.BUDGET_EXHAUSTED, ledger, list(self.events), "budget exhausted"
        )
