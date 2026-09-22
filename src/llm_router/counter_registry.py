"""The registry of instrumentation counters, and the guarantee that each is read.

R12 (audit 2026-09-22). The repeated CLASS-A defect in this codebase is not a
counter that reports the wrong number — it is a counter that reports to nobody:

* ``failopen.record()`` had **58 call sites in src/ and zero readers** outside
  the test suite. Every swallowed exception in the money, routing and telemetry
  paths was counted into a file nothing opened.
* ``execution_ledger.dropped_event_count()`` counts ledger events lost under
  contention. Only ``tests/reliability/test_ledger_concurrency.py`` ever called
  it, so in production a dropped event still left no trace anyone could see.
* ``session_store.lock_timeout_count()`` — same shape, same silence.
* ``prompt_capture.counters()`` is assembled into a ``status()`` dict that no
  command, hook or surface imports.
* The hook terminal-outcome invariant ("every invocation logging ``prompt_len=``
  logs exactly one terminal outcome") is written down in CLAUDE.md and enforced
  by a test, but the **rate was never computed on real traffic**. When it was
  computed by hand during the audit it came out at 5.5%.

Each of those was built in good faith, in a commit that believed it was adding
observability. None of them could inform an operator. Writing a number down is
not instrumentation; a number someone can READ is.

So this module holds two things:

1. :data:`REGISTRY` — every instrumentation counter, its reader, and the
   operator surface it appears on. Adding a counter means adding a line here.
2. :func:`readings` / :func:`render_lines` — the reader itself. ``llm-router
   doctor`` renders this, which is what makes the registry load-bearing rather
   than documentation: delete the doctor section and the R12 tests fail.

``tests/test_r12_every_counter_has_a_reader.py`` enforces both directions —
a registered counter whose value never moves when its writer fires, and a
counter-shaped accessor in ``src/`` that is not registered, both fail.

**Unknown is not zero**, throughout. A reading whose source is unavailable
carries ``value=None``, never 0 — the same rule that :mod:`llm_router.failopen`
and :mod:`llm_router.coverage` already follow, and for the same reason: an
empty measurement rendered as a healthy zero is how a broken counter looks
exactly like a clean run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "Counter",
    "CounterReading",
    "REGISTRY",
    "counter_ids",
    "read_one",
    "readings",
    "render_lines",
]


@dataclass(frozen=True)
class CounterReading:
    """What a counter says right now.

    ``value is None`` means UNKNOWN — the store was unreadable, the module
    would not import, the log does not exist. It never means zero.
    """

    value: float | None
    #: True when the number itself indicates degradation an operator should act
    #: on. Separate from ``value > 0`` because for some counters (prompts
    #: captured) a non-zero value is the healthy case.
    alarming: bool = False
    #: Why the value is None, when it is.
    unknown_reason: str = ""
    detail: tuple[str, ...] = ()
    #: Lines this counter wants escalated to the health check VERBATIM, because
    #: the specific wording is the actionable part.
    #:
    #: T-07 established that "fail-opens that could NOT be recorded" must reach
    #: `doctor`'s issue list, not merely its printout — a line in a long report
    #: that nothing acts on is how 58 call sites stayed invisible. Folding that
    #: into a generic "<id>=<n>" issue would have kept the exit code and lost
    #: the one sentence telling an operator the STATE DIRECTORY IS UNWRITABLE,
    #: which is a different emergency from a high fail-open count.
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class Counter:
    """One instrumentation counter and the surface an operator reads it on."""

    id: str
    #: What goes wrong in the world when this number is not zero.
    makes_visible: str
    #: Where the number comes from, as ``module:attribute``, for the discovery
    #: scan to match against. Not used to call anything — ``reader`` does that.
    source: str
    #: Reads the current value. MUST NOT raise: a doctor section that throws
    #: takes the other counters down with it.
    reader: Callable[[], CounterReading]
    #: The human-facing surface. Free text, but it has to name something real —
    #: the R12 test checks the doctor path actually renders this registry.
    surface: str = "llm-router doctor"
    #: Formats the value. Default is a plain integer count.
    unit: str = "event(s)"
    tags: tuple[str, ...] = field(default_factory=tuple)


# ── Readers ───────────────────────────────────────────────────────────────
#
# Each is a thin adapter over the module that owns the number. They live here,
# not in the owning modules, so that the owning modules keep no dependency on
# the reporting layer — the coupling runs one way, registry → subsystem.
#
# All of them convert failure to `value=None`, never to 0.


def _read_failopen() -> CounterReading:
    try:
        from llm_router import failopen

        counts = failopen.snapshot()
        total = counts.total
        unpersisted = counts.unpersisted_total
        report = tuple(counts.render_report())
        escalate = tuple(
            ln for ln in report
            if "could NOT be recorded" in ln or "UNREADABLE" in ln
        )
        if total is None:
            return CounterReading(
                None,
                alarming=True,
                unknown_reason="fail-open store present but unparseable",
                detail=report,
                issues=escalate,
            )
        return CounterReading(
            float(total + unpersisted),
            alarming=bool(unpersisted),
            detail=report,
            issues=escalate,
        )
    except Exception as exc:  # noqa: BLE001
        return CounterReading(None, unknown_reason=type(exc).__name__)


def _read_dropped_events() -> CounterReading:
    """Ledger events lost under write contention.

    In-process only — `execution_ledger` counts into a module global, so this
    reports what THIS process dropped, not what the machine has dropped since
    install. That is a real limitation and it is stated rather than papered
    over: in a hook, which is one short-lived process per invocation, a non-zero
    reading means the loss happened in the very invocation being examined.
    """
    try:
        from llm_router import execution_ledger

        n = execution_ledger.dropped_event_count()
        return CounterReading(float(n), alarming=n > 0)
    except Exception as exc:  # noqa: BLE001
        return CounterReading(None, unknown_reason=type(exc).__name__)


def _read_lock_timeouts() -> CounterReading:
    """Session-store writes abandoned because the lock could not be taken."""
    try:
        from llm_router import session_store

        n = session_store.lock_timeout_count()
        return CounterReading(float(n), alarming=n > 0)
    except Exception as exc:  # noqa: BLE001
        return CounterReading(None, unknown_reason=type(exc).__name__)


def _read_capture_outcomes() -> CounterReading:
    """Ground-truth prompt capture: what happened to each candidate prompt.

    The interesting number is not "how many were captured" but how many were
    REFUSED and why — a capture path that silently rejects everything looks
    identical to a quiet day, which is the denominator-disappearance shape this
    repo has hit three times.
    """
    try:
        from llm_router import prompt_capture

        c = prompt_capture.counters()
        total = sum(c.values())
        captured = int(c.get("captured", 0))
        refused = total - captured
        detail = tuple(f"{k}={v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))
        return CounterReading(
            float(total),
            # Every candidate refused, with candidates present, is the failure.
            alarming=bool(total and not captured),
            detail=detail + ((f"refused={refused}",) if refused else ()),
        )
    except Exception as exc:  # noqa: BLE001
        return CounterReading(None, unknown_reason=type(exc).__name__)


def _read_unterminated_invocations() -> CounterReading:
    """Hook invocations that logged `prompt_len=` and NO terminal outcome.

    The CLAUDE.md invariant has had a test since the `ENFORCE=off` branch cost a
    day of investigation, but the test proves the code CAN log an outcome — it
    says nothing about whether real traffic did. This computes the rate on the
    actual log, which is the only form of the claim worth anything.
    """
    try:
        from llm_router.routing_report import unterminated_invocations

        n, total = unterminated_invocations()
        if total == 0:
            return CounterReading(
                None,
                unknown_reason="no real user prompts in auto-route-debug.log yet",
            )
        pct = 100.0 * n / total
        return CounterReading(
            float(n),
            # A handful is noise (a session that ended mid-turn); a systematic
            # rate means a routing branch is logging nothing at all, which is
            # exactly the bug that hid `ENFORCE=off`.
            alarming=pct >= 2.0,
            detail=(f"{n} of {total} real invocations ({pct:.1f}%)",),
        )
    except Exception as exc:  # noqa: BLE001
        return CounterReading(None, unknown_reason=type(exc).__name__)


def _read_interception_gaps() -> CounterReading:
    """Tool output the compressor declined to intercept, by reason.

    `coverage.snapshot()` already has production readers (the dashboard and the
    cost report). It is registered anyway: the registry is the inventory, and an
    inventory with holes in it cannot be used to answer "is anything unread?".
    """
    try:
        from llm_router import coverage

        snap = coverage.snapshot()
        if not snap.readable:
            return CounterReading(
                None, alarming=True, unknown_reason="coverage store unparseable"
            )
        by = dict(snap.by_reason)
        return CounterReading(
            float(sum(by.values())),
            alarming=bool(getattr(snap, "is_degraded", False)),
            detail=tuple(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])),
        )
    except Exception as exc:  # noqa: BLE001
        return CounterReading(None, unknown_reason=type(exc).__name__)



def _read_hook_kills() -> CounterReading:
    """Hooks killed mid-invocation, from markers they could not clear.

    R9. This is the counter that makes "routing stopped working" answerable.
    Every other signal the hook produces requires the hook to still be running
    when it produces it.
    """
    try:
        from llm_router import hook_liveness

        n = hook_liveness.orphan_count()
        return CounterReading(float(n), alarming=n > 0)
    except Exception as exc:  # noqa: BLE001
        return CounterReading(None, unknown_reason=type(exc).__name__)


REGISTRY: tuple[Counter, ...] = (
    Counter(
        id="fail_open_events",
        makes_visible="an exception was swallowed and llm-router carried on degraded",
        source="llm_router.failopen:snapshot",
        reader=_read_failopen,
        tags=("degradation",),
    ),
    Counter(
        id="ledger_events_dropped",
        makes_visible="an execution-ledger event was lost, so a route is missing "
                      "from every accounting query that reads it",
        source="llm_router.execution_ledger:dropped_event_count",
        reader=_read_dropped_events,
        tags=("data-loss",),
    ),
    Counter(
        id="session_lock_timeouts",
        makes_visible="a session-store write was abandoned; the session's state "
                      "on disk is behind what actually happened",
        source="llm_router.session_store:lock_timeout_count",
        reader=_read_lock_timeouts,
        tags=("data-loss",),
    ),
    Counter(
        id="capture_outcomes",
        makes_visible="ground-truth capture refusing every candidate, which is "
                      "indistinguishable from a quiet day in the dataset size",
        source="llm_router.prompt_capture:counters",
        reader=_read_capture_outcomes,
        unit="candidate(s)",
        tags=("denominator",),
    ),
    Counter(
        id="hook_kills",
        makes_visible="the hook exceeded its budget and was killed, which looks "
                      "exactly like it choosing not to route",
        source="llm_router.hook_liveness:orphan_count",
        reader=_read_hook_kills,
        unit="killed invocation(s)",
        tags=("invariant", "liveness"),
    ),
    Counter(
        id="unterminated_invocations",
        makes_visible="a routing branch that skips without logging a reason — the "
                      "defect that hid ENFORCE=off for a day",
        source="llm_router.routing_report:unterminated_invocations",
        reader=_read_unterminated_invocations,
        unit="invocation(s)",
        tags=("invariant",),
    ),
    Counter(
        id="interception_gaps",
        makes_visible="tool output that passed the allowlist and was still not "
                      "compressed, so the coverage denominator is not the claim",
        source="llm_router.coverage:snapshot",
        reader=_read_interception_gaps,
        unit="observation(s)",
        tags=("denominator",),
    ),
)


def counter_ids() -> tuple[str, ...]:
    return tuple(c.id for c in REGISTRY)


def read_one(counter_id: str) -> CounterReading:
    for c in REGISTRY:
        if c.id == counter_id:
            return c.reader()
    raise KeyError(counter_id)


def readings() -> list[tuple[Counter, CounterReading]]:
    """Every counter and its current value. Never raises."""
    out: list[tuple[Counter, CounterReading]] = []
    for c in REGISTRY:
        try:
            out.append((c, c.reader()))
        except Exception as exc:  # noqa: BLE001 — one bad reader must not
            # take the rest of the report with it; that would turn a partial
            # outage into total blindness, which is the whole failure mode.
            out.append((c, CounterReading(None, unknown_reason=type(exc).__name__)))
    return out


def render_lines(*, detail: bool = True) -> list[str]:
    """Operator-readable lines. The doctor section is a thin wrapper over this."""
    lines: list[str] = []
    for c, r in readings():
        if r.value is None:
            why = f" ({r.unknown_reason})" if r.unknown_reason else ""
            lines.append(f"{c.id}: Unknown{why}")
        else:
            n = int(r.value) if float(r.value).is_integer() else r.value
            mark = "  ← " + c.makes_visible if r.alarming else ""
            lines.append(f"{c.id}: {n} {c.unit}{mark}")
        if detail:
            for d in r.detail:
                lines.append(f"    {d}")
    return lines
