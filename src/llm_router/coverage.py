"""Routing coverage — how much traffic LLM Router did NOT observe.

Finding I-1. ``auto-route.py`` has twelve ``sys.exit(0)`` sites; six exit without
emitting a routing directive and without recording that they declined to. The
catch-all at the bottom of ``main()`` swallows any unhandled exception and exits
0, its debug log best-effort. A run in which nearly every prompt crashed out
through that path produced telemetry byte-identical to a clean run — the incident
this codebase documents as previously indistinguishable.

Every rate LLM Router reports is a fraction of *observed* traffic. Without a count of
the unobserved, a rate silently redefines its own denominator: 100% of the calls
we saw is not 100% of the calls that happened, and the difference is invisible
precisely when something is broken.

Design notes:

* **Unknown is not zero.** With no events at all, ``coverage_pct`` is ``None`` and
  renders ``Unknown``. A denominator of zero must never become 0% or 100% — a
  percentage over an unknown denominator is fabricated, and it fails in the
  direction that looks healthy (RED2-02's shape).
* **Recording never raises.** This runs inside a hook. An observability feature
  that can take down the turn it observes is worse than no feature.
* **Append-only, one line per event.** Hooks are separate short-lived processes,
  so a single ``O_APPEND`` write of a small line is the concurrency-safe option
  without taking a lock on the routing hot path. WP-06's ledger work is the
  cautionary tale for doing anything cleverer here.
* **Reason codes are explicit and exhaustive.** No ``other`` bucket: a new bypass
  must add a code rather than hide inside an existing count.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum

from llm_router.paths import state_path

__all__ = [
    "Reason",
    "Coverage",
    "record_observed",
    "record_unobserved",
    "snapshot",
    "store_path",
    "clear",
    "reset_cache",
    "DEGRADED_BELOW_PCT",
]

#: Coverage below this is reported as degraded. WP-07 criterion.
DEGRADED_BELOW_PCT = 90.0

#: Cap the store so an unattended machine cannot grow it without bound. Older
#: lines are dropped on rotation; the aggregate is recomputed from what remains,
#: which is why `snapshot()` describes a window rather than all time.
_MAX_EVENTS = 50_000

_STORE_FILENAME = "coverage.jsonl"

_cached_snapshot: Coverage | None = None


class Reason(Enum):
    """Why a prompt produced no routing directive.

    One code per silent-bypass site in auto-route.py. Deliberately exhaustive:
    an ``other`` bucket would let a newly-added bypass accumulate inside an
    existing count, which is how I-1 stayed invisible.
    """

    #: Empty/whitespace prompt in normal mode — nothing to route.
    EMPTY_PROMPT = "empty_prompt"
    #: A llm_router-debug prompt about llm_router itself; routing it would be circular.
    SELF_REFERENCE_BYPASS = "self_reference_bypass"
    #: User explicitly asked for Claude with the `claude:` prefix.
    EXPLICIT_CLAUDE_PREFIX = "explicit_claude_prefix"
    #: Short continuation ("keep going") handed to the host agent.
    CONTINUATION_BYPASS = "continuation_bypass"
    #: The classifier returned no result and the hook declined to guess.
    CLASSIFY_FAILED = "classify_failed"
    #: main() raised and the fail-open handler exited 0. THE incident path.
    UNHANDLED_EXCEPTION = "unhandled_exception"


@dataclass(frozen=True)
class Coverage:
    """Observed vs unobserved counts, and the rate they justify."""

    observed_n: int = 0
    unobserved_n: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    readable: bool = True
    #: Lines that parsed but could not be classified, or did not parse at all.
    #: Counted so an operator sees "3 malformed lines" rather than inferring a
    #: boolean from ``readable``. A store can be READABLE and still have some.
    malformed_n: int = 0

    @property
    def total_n(self) -> int:
        return self.observed_n + self.unobserved_n

    @property
    def coverage_pct(self) -> float | None:
        """Percentage of traffic actually observed, or ``None`` when unknowable.

        ``None`` for an empty store (no traffic yet) and for an unreadable one.
        Both are genuinely unknown; neither is 0% or 100%.
        """
        if not self.readable or self.total_n == 0:
            return None
        return 100.0 * self.observed_n / self.total_n

    @property
    def is_degraded(self) -> bool:
        """True only when coverage is KNOWN and below the threshold.

        Unknown coverage is not reported as degraded here — it is reported as
        unknown, which `render_pct` makes visible. Conflating the two would make
        a fresh install look broken.
        """
        pct = self.coverage_pct
        return pct is not None and pct < DEGRADED_BELOW_PCT

    def render_pct(self) -> str:
        """Human-facing coverage figure. ``Unknown`` when the denominator is."""
        pct = self.coverage_pct
        if pct is None:
            return "Unknown"
        return f"{pct:.1f}%"


def store_path():
    """Path to the coverage store, inside LLM_ROUTER_HOME when isolated."""
    return state_path(_STORE_FILENAME)


def _append_event(payload: dict) -> None:
    """Append one JSON line. Split out so a test can force the failure path."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    # O_APPEND: one small write per event, so concurrent hook processes cannot
    # interleave partial lines. No lock on the routing hot path.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _record(kind: str, detail: str) -> None:
    global _cached_snapshot
    try:
        _append_event({"k": kind, "d": detail})
        _cached_snapshot = None
    except Exception:  # noqa: BLE001 — see module docstring: never break the hook
        pass


def record_observed(tool: str) -> None:
    """Record that a prompt produced a routing directive."""
    _record("o", tool)


def record_unobserved(reason: Reason) -> None:
    """Record that a prompt exited WITHOUT producing a routing directive."""
    _record("u", reason.name)


def clear() -> None:
    """Delete the store. Test helper; never called in production."""
    global _cached_snapshot
    _cached_snapshot = None
    try:
        store_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


def reset_cache() -> None:
    """Drop the memoised snapshot so the next read hits disk."""
    global _cached_snapshot
    _cached_snapshot = None


def snapshot() -> Coverage:
    """Aggregate the store.

    A store that exists but cannot be parsed yields ``readable=False``, which
    renders ``Unknown`` rather than zero — an unreadable store is not a quiet
    period, and reporting it as one is the RED2-02 failure shape.
    """
    global _cached_snapshot
    if _cached_snapshot is not None:
        return _cached_snapshot

    path = store_path()
    if not path.exists():
        _cached_snapshot = Coverage()
        return _cached_snapshot

    observed = 0
    unobserved = 0
    by_reason: dict[str, int] = {}
    malformed = 0
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()[-_MAX_EVENTS:]
    except Exception:  # noqa: BLE001
        _cached_snapshot = Coverage(readable=False)
        return _cached_snapshot

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:  # noqa: BLE001
            malformed += 1
            continue
        if not isinstance(event, dict):
            # Valid JSON of the wrong shape -- a bare array or string has no .get.
            # `event` is hoisted out of the try because it is read twice below, so
            # without this guard the AttributeError would ESCAPE THE LOOP and
            # discard every line counted so far, understating the total in exactly
            # the way the comment below forbids. `failopen._snapshot` keeps its
            # .get inside the try and is already correct; this is the one place
            # that could not.
            malformed += 1
            continue
        if event.get("k") == "o":
            observed += 1
        elif event.get("k") == "u":
            unobserved += 1
            name = str(event.get("d", "")) or "UNKNOWN"
            by_reason[name] = by_reason.get(name, 0) + 1
        else:
            malformed += 1

    # A store whose content is entirely unparseable is unreadable, not empty.
    # Partial corruption still reports, because a partial count beats no count
    # as long as the total is not silently understated -- which is why malformed
    # lines are not simply skipped when they are all we have.
    if malformed and observed == 0 and unobserved == 0:
        _cached_snapshot = Coverage(readable=False, malformed_n=malformed)
        return _cached_snapshot

    _cached_snapshot = Coverage(
        observed_n=observed,
        unobserved_n=unobserved,
        by_reason=by_reason,
        malformed_n=malformed,
    )
    return _cached_snapshot
