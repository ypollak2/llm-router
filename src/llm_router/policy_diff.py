"""Policy diff tool — Plan 07 Category G.2.

Compares two routing policies by predicting which model each would route
each sample prompt to. The expensive parts of routing (classification,
provider health, dynamic chain selection, budget pressure) are deliberately
*not* simulated: a "diff" should reflect the policy author's choices, not
runtime conditions. The bandit and circuit-breaker layers are stripped so
two policies with the same workhorse + specialist tables diff as
byte-identical regardless of which provider was unhealthy this morning.

Head-model prediction for a (policy, subject) pair:

1. If ``policy.specialists[subject]`` exists → that's the head.
2. Otherwise → ``policy.workhorses[0]``.
3. If both are empty (behaviour-only policy) → ``None``, reported as
   ``"<unconfigured>"`` in the diff table.

Cost projection uses :func:`llm_router.calibration.predict_cost` so the
projected $/1K-queries numbers reflect actual empirical token shapes
(Cat F) — not the legacy 80-token guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from llm_router.calibration import predict_cost
from llm_router.policy import PolicyManager, RoutingPolicy
from llm_router.types import TaskType

__all__ = [
    "Sample",
    "PolicyDiffEntry",
    "PolicyDiffReport",
    "predict_head_model",
    "diff_policies",
]


# ── Inputs / outputs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sample:
    """A single sample to diff across two policies.

    ``input_tokens`` is used only for cost projection; defaults to 200 which
    matches the median QUERY-task input observed in RouterArena 2026-06 data
    so the projected $/1K-queries line is realistic even when callers pass
    only ``id`` and ``subject``.
    """

    id: str
    subject: str = "general"
    task_type: TaskType = TaskType.QUERY
    input_tokens: int = 200


@dataclass(frozen=True)
class PolicyDiffEntry:
    """One sample where the two policies pick different head models."""

    sample_id: str
    subject: str
    model_a: str
    model_b: str
    cost_a: float
    cost_b: float


@dataclass(frozen=True)
class PolicyDiffReport:
    """Summary of a policy-vs-policy run over a sample set."""

    policy_a: str
    policy_b: str
    n_samples: int
    differences: list[PolicyDiffEntry] = field(default_factory=list)
    total_cost_a: float = 0.0
    total_cost_b: float = 0.0

    @property
    def n_differences(self) -> int:
        return len(self.differences)

    @property
    def cost_delta_pct(self) -> float:
        """Percent change going from policy A → policy B (negative = cheaper)."""
        if self.total_cost_a <= 0:
            return 0.0
        return (self.total_cost_b - self.total_cost_a) / self.total_cost_a


# ── Prediction ──────────────────────────────────────────────────────────────


_UNCONFIGURED = "<unconfigured>"


def predict_head_model(policy: RoutingPolicy, subject: str) -> str:
    """Return the head model this policy would try first for ``subject``.

    The head is the deterministic first attempt before any bandit reorder
    or health gating. Returning :data:`_UNCONFIGURED` (rather than raising)
    keeps the diff usable on behaviour-only policies whose model tables
    are intentionally empty.
    """
    if subject and policy.specialists.get(subject):
        return policy.specialists[subject]
    if policy.workhorses:
        return policy.workhorses[0]
    return _UNCONFIGURED


def _projected_cost(model: str, task_type: TaskType, input_tokens: int) -> float:
    """Empirical-shape cost projection; zero for unconfigured/free models."""
    if model == _UNCONFIGURED:
        return 0.0
    return predict_cost(model, task_type, input_tokens)


# ── Diff ────────────────────────────────────────────────────────────────────


def diff_policies(
    policy_a_name: str,
    policy_b_name: str,
    samples: list[Sample],
    *,
    manager: PolicyManager | None = None,
) -> PolicyDiffReport:
    """Compare ``policy_a`` and ``policy_b`` over ``samples``.

    Args:
        policy_a_name: Name of the first policy (looked up via PolicyManager).
        policy_b_name: Name of the second policy.
        samples: Subjects + cost-projection inputs to evaluate.
        manager: Optional PolicyManager to inject for tests. Defaults to a
            fresh manager so test fakes can avoid touching the user's real
            ``~/.llm-router/policies`` directory.

    Returns:
        A :class:`PolicyDiffReport` with one entry per differing sample
        plus total/projected cost on each side. The order of
        ``report.differences`` matches the input order of ``samples``.
    """
    mgr = manager or PolicyManager()
    policy_a = mgr.load_policy(policy_a_name)
    policy_b = mgr.load_policy(policy_b_name)

    diffs: list[PolicyDiffEntry] = []
    total_a = 0.0
    total_b = 0.0
    for sample in samples:
        model_a = predict_head_model(policy_a, sample.subject)
        model_b = predict_head_model(policy_b, sample.subject)
        cost_a = _projected_cost(model_a, sample.task_type, sample.input_tokens)
        cost_b = _projected_cost(model_b, sample.task_type, sample.input_tokens)
        total_a += cost_a
        total_b += cost_b
        if model_a != model_b:
            diffs.append(
                PolicyDiffEntry(
                    sample_id=sample.id,
                    subject=sample.subject,
                    model_a=model_a,
                    model_b=model_b,
                    cost_a=cost_a,
                    cost_b=cost_b,
                )
            )

    return PolicyDiffReport(
        policy_a=policy_a_name,
        policy_b=policy_b_name,
        n_samples=len(samples),
        differences=diffs,
        total_cost_a=total_a,
        total_cost_b=total_b,
    )


def format_diff_report(report: PolicyDiffReport) -> str:
    """Render a :class:`PolicyDiffReport` as a CLI-friendly multi-line string.

    Kept out of :mod:`llm_router.commands.policy` so non-CLI callers (tests,
    notebooks, MCP tools) can reuse the layout without dragging in argparse.
    """
    lines = [
        f"Policy diff: {report.policy_a} → {report.policy_b}",
        f"Samples: {report.n_samples}  |  Differences: {report.n_differences}",
        "",
    ]
    if report.differences:
        lines.append("Per-sample differences:")
        for entry in report.differences:
            lines.append(
                f"  [{entry.subject:>12}] {entry.sample_id:<24} "
                f"{entry.model_a}  →  {entry.model_b}"
            )
        lines.append("")
    delta_pct = report.cost_delta_pct * 100
    lines.append(
        f"Projected total cost: "
        f"{report.policy_a}=${report.total_cost_a:.4f}  "
        f"{report.policy_b}=${report.total_cost_b:.4f}  "
        f"({delta_pct:+.1f}%)"
    )
    return "\n".join(lines)
