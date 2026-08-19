"""Decision engine — compose signal scores into a model pick.

A Decision is a boolean expression over Signal results:
    AND(signal_a, signal_b)  fires when both signals fire
    OR(signal_a, signal_b)   fires when either signal fires
    NOT(signal_a)            fires when signal_a does not fire
    SINGLE(signal_a)         fires when signal_a fires

When a Decision fires, its `action` selects the model/chain. Decisions are
evaluated in priority order; the first firing wins.
"""
from llm_router.decisions.engine import Decision, DecisionEngine, DecisionResult

__all__ = ["Decision", "DecisionEngine", "DecisionResult"]
