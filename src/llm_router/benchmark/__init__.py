"""Pluggable benchmark runners — Plan 07 Category G (Operational tooling).

Cat G adds operational tooling on top of the routing infrastructure built
in Phases 1-5: a generic benchmark runner with a small plug-in protocol
(``llm_router benchmark run <name>``), a policy diff tool
(``llm_router policy diff a b``), and a regression detector
(``llm_router benchmark regress --since <tag>``).

The protocol is intentionally narrow: every runner just needs to load a
dataset, format predictions from a routing decision, evaluate accumulated
predictions, and (optionally) submit to a leaderboard. Anything more
complex (parallelism, retry, ranking) is the orchestrator's job, not the
runner's — so a new benchmark drops in as a single file.

Discovery model: a static registry keyed by benchmark name. The plan doc
floats entry-points based discovery as a possible follow-up; today's
registry covers the in-package use case (RouterArena) without forcing the
package to declare entry-points or callers to manage a plug-in cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Prompt",
    "Prediction",
    "BenchmarkResult",
    "SubmissionResult",
    "BenchmarkRunner",
    "register_runner",
    "get_runner",
    "list_runners",
]


# ── Data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Prompt:
    """A single benchmark prompt to route.

    ``subject`` is optional because not every benchmark tags prompts; when
    set, it feeds straight into the bandit/specialist machinery exactly like
    classifier-emitted subjects.
    """

    id: str
    text: str
    reference: str | None = None
    subject: str | None = None
    task_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Prediction:
    """The runner's view of one (prompt, route, response) triple.

    Holding ``prompt_id``, ``model``, ``cost_usd``, ``latency_ms`` plus the
    raw ``response`` text lets every benchmark's ``evaluate`` produce its
    own score without re-running the route.
    """

    prompt_id: str
    model: str
    response: str
    cost_usd: float
    latency_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkResult:
    """Aggregate score over a finished prediction set.

    ``score`` is the headline number a runner reports — accuracy, F1, or
    whatever the benchmark's primary metric is. ``per_subject`` lets the
    regression detector surface *which* subject regressed when the overall
    score drops, which the plan's spec calls out as the highest-value
    diagnostic.
    """

    benchmark: str
    split: str
    score: float
    n_samples: int
    per_subject: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SubmissionResult:
    """Outcome of pushing predictions to a leaderboard."""

    submitted: bool
    url: str | None = None
    message: str = ""


# ── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class BenchmarkRunner(Protocol):
    """Plug-in contract for a benchmark runner.

    Implementations should be cheap to construct (no network IO in
    ``__init__``) so the CLI can list available runners without doing real
    work. All four methods may raise; the orchestrator catches and reports
    so a broken runner can't take down ``llm_router benchmark`` for everyone.
    """

    name: str

    def load_dataset(self, split: str) -> list[Prompt]:
        """Return prompts for the named split (e.g. ``"sub_10"``, ``"full"``)."""

    def format_prediction(self, prompt: Prompt, prediction: Prediction) -> dict:
        """Serialise one (prompt, prediction) pair for submission/evaluation."""

    def evaluate(self, predictions: list[Prediction], dataset: list[Prompt]) -> BenchmarkResult:
        """Score the predictions against the dataset's references."""

    def submit(self, predictions: list[Prediction]) -> SubmissionResult | None:
        """Push results to a leaderboard. Optional — return ``None`` if N/A."""


# ── Registry ────────────────────────────────────────────────────────────────


_runners: dict[str, BenchmarkRunner] = {}


def register_runner(runner: BenchmarkRunner) -> None:
    """Register a runner under its declared ``name``.

    Overwriting an existing entry is allowed and intentional — tests that
    swap in a fake runner shouldn't need a tear-down helper. The trade-off
    is that a name clash between two real benchmarks would silently win
    based on import order; the test suite covers ``list_runners`` so a
    surprising override is at least visible.
    """
    _runners[runner.name] = runner


def get_runner(name: str) -> BenchmarkRunner:
    """Return the registered runner or raise ``KeyError`` with a hint."""
    try:
        return _runners[name]
    except KeyError as err:
        registered = ", ".join(sorted(_runners)) or "<none>"
        raise KeyError(
            f"No benchmark runner registered as {name!r}. Registered: {registered}"
        ) from err


def list_runners() -> list[str]:
    """Names of currently-registered runners, sorted alphabetically."""
    return sorted(_runners)


# ── Auto-register bundled runners ───────────────────────────────────────────
# Import-for-side-effects pattern: importing the runner module triggers its
# ``register_runner(...)`` call. Wrapped in try/except so a broken runner
# (e.g. missing optional dependency) can't break the whole CLI.
try:  # pragma: no cover — import-time wiring
    from llm_router.benchmark.runners import routerarena as _routerarena  # noqa: F401
except Exception:  # pragma: no cover — defensive
    pass
