"""Budget envelope — track consumed cost + refuse calls that would breach.

The envelope is the single most valuable safety feature LLM Router offers
agentic systems: agent runtimes don't know provider pricing in real time,
so they can't refuse expensive calls themselves. The envelope sits
between the agent and the provider and says "no" when a proposed call
would exceed the session cap.
"""
from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(Exception):
    """Raised when a proposed routing call would breach the session cap.

    Carries enough context (cap, consumed, proposed) for the caller to
    surface a useful error to the user or to retry with a cheaper model.
    """

    def __init__(
        self,
        session_id: str,
        cap_usd: float,
        consumed_usd: float,
        proposed_usd: float,
    ) -> None:
        self.session_id = session_id
        self.cap_usd = cap_usd
        self.consumed_usd = consumed_usd
        self.proposed_usd = proposed_usd
        super().__init__(
            f"session {session_id}: budget exceeded — "
            f"cap=${cap_usd:.4f}, consumed=${consumed_usd:.4f}, "
            f"proposed=${proposed_usd:.4f}"
        )


class IterationsExceeded(Exception):
    """Raised when a session's ``step_count`` would exceed its
    ``max_iterations`` cap.

    T3-M3 (Track-3 agent-safety). Per-session counter; the cap is set
    at ``SessionStore.create(max_iterations=N)`` and checked in
    ``record_step`` before any state transition. The session is
    transitioned to ``BUDGET_EXCEEDED`` on raise so subsequent
    ``record_step`` calls fail with ``TerminalStateViolation``.
    """

    def __init__(
        self,
        session_id: str,
        max_iterations: int,
        current_step_count: int,
    ) -> None:
        self.session_id = session_id
        self.max_iterations = int(max_iterations)
        self.current_step_count = int(current_step_count)
        super().__init__(
            f"session {session_id} reached max_iterations="
            f"{max_iterations} (current step {current_step_count + 1})"
        )


class RecursionDepthExceeded(Exception):
    """Raised when a parent chain at session-create time would exceed
    the parent's ``max_recursion_depth`` cap.

    T3-M3 (Track-3 agent-safety). The depth is walked from the
    parent's ``parent_session_id`` chain at create time so a child
    cannot be spawned that would push the chain past the cap.
    """

    def __init__(
        self,
        parent_session_id: str,
        max_recursion_depth: int,
        current_depth: int,
    ) -> None:
        self.parent_session_id = parent_session_id
        self.max_recursion_depth = int(max_recursion_depth)
        self.current_depth = int(current_depth)
        super().__init__(
            f"parent {parent_session_id} max_recursion_depth="
            f"{max_recursion_depth} reached (would-be depth {current_depth})"
        )


@dataclass(frozen=True)
class BudgetEnvelope:
    """Holds a cap, tracks consumed, answers two questions:
        (1) would_exceed(prospective_cost) — is this next call safe?
        (2) consume(actual_cost) — record what was spent and return a new
            envelope with the updated consumed total.

    Envelopes are immutable; consume() returns a new instance so the
    caller controls when to persist. Tests need only construct + compare.
    """

    cap_usd: float
    consumed_usd: float = 0.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.consumed_usd)

    @property
    def exhausted(self) -> bool:
        return self.consumed_usd >= self.cap_usd

    def would_exceed(self, prospective_cost_usd: float) -> bool:
        """True if charging prospective_cost would push consumed past cap."""
        return (self.consumed_usd + prospective_cost_usd) > self.cap_usd

    def consume(self, actual_cost_usd: float) -> "BudgetEnvelope":
        """Return a new envelope with consumed += actual_cost.

        Does NOT enforce — caller should check would_exceed first if they
        want to refuse pre-emptively. Use raise_if_would_exceed for the
        check + raise pattern.
        """
        if actual_cost_usd < 0:
            raise ValueError("actual_cost_usd must be non-negative")
        return BudgetEnvelope(
            cap_usd=self.cap_usd,
            consumed_usd=self.consumed_usd + actual_cost_usd,
        )

    def raise_if_would_exceed(
        self, prospective_cost_usd: float, session_id: str
    ) -> None:
        """Raise BudgetExceeded if charging would breach the cap. No-op otherwise."""
        if self.would_exceed(prospective_cost_usd):
            raise BudgetExceeded(
                session_id=session_id,
                cap_usd=self.cap_usd,
                consumed_usd=self.consumed_usd,
                proposed_usd=prospective_cost_usd,
            )
