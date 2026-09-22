"""Fail-open accounting — a swallowed exception must still leave a trace.

RED8-09. The codebase carries ~810 broad `except Exception` handlers, and in the
money, routing, verification and telemetry paths a meaningful number ended in a
bare `pass`. Fail-open is often the RIGHT behaviour there — a hook that raises
kills the user's turn, and a telemetry write that raises turns observability into
an outage — so the fix is not to remove the catches. It is to stop them being
SILENT.

A silent catch is indistinguishable from the happy path in every surface we have.
That is the same defect this audit kept finding elsewhere: the ledger drop that
looked like no traffic, the classification bypass that looked like a clean run,
the savings query failure that rendered as "$0.00 saved". A caught exception is
information; discarding it converts a known failure into an unknown one.

So every retained broad catch in a protected module calls :func:`record` with a
STABLE event code. The code is the contract — it is greppable, it survives
refactors, and it lets an operator ask "how often is this actually firing?"
rather than guessing.

Design constraints, all learned from earlier work packages:

* **Never raises.** This runs inside the handlers that exist because raising is
  unacceptable. An accounting call that can throw would turn a fail-open into a
  crash — strictly worse than the silence it replaces.
* **Unknown is not zero.** An unreadable counter file reports ``None``, not 0.
* **Append-only, one small write per event.** Same reasoning as
  :mod:`llm_router.coverage`: these are separate short-lived processes and this sits
  on paths that must not take a lock.
* **No `other` bucket.** Codes are declared at the call site; an undeclared code
  is still recorded, but the lint requires call sites to name one.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field

from llm_router.paths import state_path

__all__ = ["record", "snapshot", "FailOpenCounts", "store_path", "clear", "reset_cache"]

_STORE_FILENAME = "fail_open.jsonl"

#: Cap the store; older lines are dropped on read. See coverage.py for the same
#: reasoning — an unattended machine must not grow this without bound.
_MAX_EVENTS = 20_000

_cached: FailOpenCounts | None = None

#: T-07 (audit 2026-09-22). Fail-opens whose OWN store write failed.
#:
#: The store is on disk, and the condition most likely to cause a burst of
#: fail-opens — an unwritable or full state directory — is exactly the condition
#: that stops them being recorded. `record()` swallowed its own write failure
#: and fell back to `structlog.debug` while the effective level was WARNING, so
#: the losses were recorded nowhere and printed nowhere. Three call sites were
#: added on 2026-09-21 believing they made losses visible.
#:
#: This counter lives in process memory and needs no filesystem. It cannot
#: survive a restart, which is the trade: a channel that works when the disk
#: does not is worth more than one that is durable and silent.
_unpersisted: dict[str, int] = {}
_unpersisted_lock = threading.Lock()


@dataclass(frozen=True)
class FailOpenCounts:
    """How often each swallowed-exception site fired."""

    by_code: dict[str, int] = field(default_factory=dict)
    readable: bool = True
    #: Fail-opens this process could not write to the store (T-07). Separate
    #: from `by_code` because their provenance is different: these are known to
    #: have happened but are not durable, and merging them would make a restart
    #: look like the failures stopped.
    unpersisted_by_code: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int | None:
        """Total swallowed exceptions, or ``None`` when the store is unreadable.

        ``None`` rather than 0: a store we cannot read is not a period with no
        failures, and reporting it as one is the RED2-02 shape.
        """
        if not self.readable:
            return None
        return sum(self.by_code.values())

    @property
    def unpersisted_total(self) -> int:
        """Fail-opens this process saw but could not record (T-07)."""
        return sum(self.unpersisted_by_code.values())

    def render_total(self) -> str:
        """The PERSISTED total only: "0", a count, or "Unknown".

        Deliberately does not fold in `unpersisted_total`. The two channels
        answer different questions — "what did the store record" and "what did
        this process see but fail to record" — and a caller that wants both
        asks `render_report()`. Mixing them here broke the contract two callers
        already relied on, which is its own small lesson about widening the
        meaning of an existing accessor instead of adding one.
        """
        t = self.total
        return "Unknown" if t is None else str(t)

    def render_report(self, *, limit: int = 8) -> list[str]:
        """Human-readable lines for `doctor` / `status`. Never raises.

        T-07: 58 `record()` call sites existed and 0 readers outside tests. A
        counter nothing reads is not instrumentation.
        """
        lines: list[str] = []
        if not self.readable:
            lines.append("fail-open counters: UNREADABLE (store present but unparseable)")
        total = self.total
        if total:
            lines.append(f"fail-open events recorded: {total}")
        elif self.readable and not self.unpersisted_total:
            lines.append("fail-open events recorded: 0")
        if self.unpersisted_total:
            lines.append(
                f"fail-open events that could NOT be recorded: {self.unpersisted_total} "
                "(the state store was unwritable — this is the serious case)"
            )
        merged: dict[str, int] = dict(self.by_code)
        for code, n in self.unpersisted_by_code.items():
            merged[code] = merged.get(code, 0) + n
        for code, n in sorted(merged.items(), key=lambda kv: -kv[1])[:limit]:
            mark = " *" if code in self.unpersisted_by_code else ""
            lines.append(f"  {n:>6}  {code}{mark}")
        if len(merged) > limit:
            lines.append(f"  … {len(merged) - limit} more site(s)")
        return lines


def store_path():
    """Path to the fail-open counter store, inside LLM_ROUTER_HOME when isolated."""
    return state_path(_STORE_FILENAME)


def _append(payload: dict) -> None:
    """Append one JSON line. Split out so a test can force the failure path."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def record(code: str, exc: BaseException | None = None, *, detail: str = "") -> None:
    """Account for a deliberately swallowed exception.

    ``code`` is a stable identifier for the SITE, not for the exception type —
    "CHZ-FO-COST-DB-WRITE" tells an operator which behaviour degraded; "OSError"
    does not.

    Never raises, by construction. Every caller is already inside a handler that
    exists because propagating was unacceptable.
    """
    global _cached
    try:
        payload = {"c": code}
        if exc is not None:
            payload["e"] = type(exc).__name__
        if detail:
            payload["d"] = detail[:200]
        _append(payload)
        _cached = None
        _persisted = True
    except Exception as store_exc:  # noqa: BLE001 — must not throw
        # T-07: do not lose the loss. Count it where no filesystem is involved.
        _persisted = False
        try:
            with _unpersisted_lock:
                _unpersisted[code] = _unpersisted.get(code, 0) + 1
            _cached = None
        except Exception:  # noqa: BLE001
            pass
        try:
            import structlog

            # WARNING, not debug. A fail-open we could not even record is the
            # one an operator most needs to see, and DEBUG is below the
            # effective level in every configuration this ships with.
            structlog.get_logger("llm_router.failopen").warning(
                "fail_open_unrecorded", code=code,
                exc=type(exc).__name__ if exc else None,
                store_error=type(store_exc).__name__,
            )
        except Exception:  # noqa: BLE001
            pass
    # Structured log too, so a live session surfaces it without reading the store.
    if _persisted:
        try:
            import structlog

            # T-07: INFO, not DEBUG. `llm-router doctor` and `status` read the
            # store, but a live session should not need to.
            structlog.get_logger("llm_router.failopen").info(
                "fail_open", code=code, exc=type(exc).__name__ if exc else None
            )
        except Exception:  # noqa: BLE001
            pass


def clear() -> None:
    """Delete the store AND the in-process counter. Test helper; never production.

    Both, or a test that forces the unwritable-store path leaks its count into
    every test that follows and the next assertion on `unpersisted_total` reads
    someone else's failure.
    """
    global _cached
    _cached = None
    with _unpersisted_lock:
        _unpersisted.clear()
    try:
        store_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


def reset_unpersisted() -> None:
    """Drop the in-process unrecorded counter (T-07). Test helper.

    `record()` increments this when its own store write fails, and the counter
    is module-global. A test that deliberately forces that path leaks its count
    into every test that follows unless one of these is called — which is how
    `test_recording_never_raises_on_a_weird_exception` (whose exception explodes
    during serialisation) silently changed the next two tests' expectations.
    """
    with _unpersisted_lock:
        _unpersisted.clear()


def reset_cache() -> None:
    global _cached
    _cached = None


def snapshot() -> FailOpenCounts:
    """Aggregate the store. Unreadable content yields ``readable=False``."""
    global _cached
    if _cached is not None:
        return _cached

    with _unpersisted_lock:
        unpersisted = dict(_unpersisted)

    path = store_path()
    if not path.exists():
        _cached = FailOpenCounts(unpersisted_by_code=unpersisted)
        return _cached

    by_code: dict[str, int] = {}
    malformed = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()[-_MAX_EVENTS:]
    except Exception:  # noqa: BLE001
        _cached = FailOpenCounts(readable=False, unpersisted_by_code=unpersisted)
        return _cached

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            code = str(json.loads(line).get("c", ""))
        except Exception:  # noqa: BLE001
            malformed += 1
            continue
        if not code:
            malformed += 1
            continue
        by_code[code] = by_code.get(code, 0) + 1

    if malformed and not by_code:
        _cached = FailOpenCounts(readable=False, unpersisted_by_code=unpersisted)
        return _cached
    _cached = FailOpenCounts(by_code=by_code, unpersisted_by_code=unpersisted)
    return _cached
