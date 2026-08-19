"""delegate() — the single entry point that ties PLAN → engine → adapters.

Given a caller-provided milestone list, a tier→adapter map, and a premium
baseline cost, it runs the MGEE engine and returns one bundle: the outcome, the
final ledger, the transparency event stream, and honest savings. The auto-planner
(freeform task → milestones) is intentionally NOT here — decomposition is a
design decision handled a layer up; this orchestrator is pure, deterministic glue.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from llm_router.agentic.engine import (
    Agent,
    Event,
    Gate,
    MGEEEngine,
    Outcome,
    Router,
)
from llm_router.agentic.ledger import Milestone, TaskLedger
from llm_router.agentic.savings import Savings, compute_savings


@dataclass
class DelegationResult:
    outcome: Outcome
    ledger: TaskLedger
    events: list[Event]
    savings: Savings
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.COMPLETE

    def summary(self) -> str:
        """Human-readable transparency: the event stream + savings + verdict."""
        lines = [e.render() for e in self.events]
        lines.append(self.savings.render())
        verdict = {
            Outcome.COMPLETE: "✅ complete",
            Outcome.SURFACED: f"⚠ surfaced — {self.reason}",
            Outcome.BUDGET_EXHAUSTED: "⚠ budget exhausted (partial)",
        }[self.outcome]
        lines.append(verdict)
        return "\n".join(lines)


def delegate(
    goal: str,
    milestones: list[Milestone],
    adapters_by_tier: dict[int, Agent],
    *,
    baseline_cost_per_milestone: float,
    budget_cap_usd: float = 1.0,
    max_attempts_per_tier: int = 2,
    router: Router | None = None,
    gate: Gate | None = None,
    event_sink: Callable[[Event], None] | None = None,
    session_context: str = "",
    workdir: str | None = None,
) -> DelegationResult:
    """Run one milestone-gated escalating delegation and return a result bundle.

    RED3-08: ``workdir`` is threaded to the engine so a repository-reading
    acceptance check inspects the tree the agents actually worked in. Left
    unset it resolves to the process's cwd — which, when llm_router runs from its
    own checkout, is a DIFFERENT git repository, so the milestone would be
    verified against LLM Router's source tree instead of the user's.
    """
    ledger = TaskLedger(goal=goal, milestones=milestones, budget_cap_usd=budget_cap_usd,
                        session_context=session_context)

    # RED3-01 (P0): supply a REAL reversibility gate when we can isolate.
    # `reversibility_gate` existed in llm_router/agentic/worktree.py and nothing
    # imported it — the README described code that was written and never wired.
    # Without a caller the engine fell back to its default, which used to
    # approve every irreversible milestone.
    #
    # Only when workdir is a git repository: GitWorktreeOps needs one, and
    # claiming isolation we cannot provide is the failure being fixed. Elsewhere
    # the engine's fail-closed default surfaces the milestone instead.
    if gate is None and workdir and _is_git_repo(workdir):
        from llm_router.agentic.worktree import GitWorktreeOps, reversibility_gate

        gate = reversibility_gate(GitWorktreeOps(repo=workdir))

    engine = MGEEEngine(
        adapters_by_tier,
        max_attempts_per_tier=max_attempts_per_tier,
        router=router,
        gate=gate,
        event_sink=event_sink,
        workdir=workdir,
    )
    result = engine.run(ledger)
    savings = compute_savings(ledger, baseline_cost_per_milestone)
    return DelegationResult(
        outcome=result.outcome,
        ledger=result.ledger,
        events=result.events,
        savings=savings,
        reason=result.reason,
    )


def _is_git_repo(path: str) -> bool:
    """True when ``path`` is inside a git work tree.

    RED3-01: gates the worktree isolation. GitWorktreeOps cannot isolate outside
    a repository, and offering isolation that silently does nothing is the exact
    shape of the defect being fixed — so the check is explicit rather than
    optimistic.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"
