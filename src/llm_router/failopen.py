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
from dataclasses import dataclass, field

from llm_router.paths import state_path

__all__ = ["record", "snapshot", "FailOpenCounts", "store_path", "clear", "reset_cache"]

_STORE_FILENAME = "fail_open.jsonl"

#: Cap the store; older lines are dropped on read. See coverage.py for the same
#: reasoning — an unattended machine must not grow this without bound.
_MAX_EVENTS = 20_000

_cached: FailOpenCounts | None = None


@dataclass(frozen=True)
class FailOpenCounts:
    """How often each swallowed-exception site fired."""

    by_code: dict[str, int] = field(default_factory=dict)
    readable: bool = True

    @property
    def total(self) -> int | None:
        """Total swallowed exceptions, or ``None`` when the store is unreadable.

        ``None`` rather than 0: a store we cannot read is not a period with no
        failures, and reporting it as one is the RED2-02 shape.
        """
        if not self.readable:
            return None
        return sum(self.by_code.values())

    def render_total(self) -> str:
        t = self.total
        return "Unknown" if t is None else str(t)


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
    except Exception:  # noqa: BLE001 — see the module docstring; this must not throw
        pass
    # Structured log too, so a live session surfaces it without reading the store.
    try:
        import structlog

        structlog.get_logger("llm_router.failopen").debug(
            "fail_open", code=code, exc=type(exc).__name__ if exc else None
        )
    except Exception:  # noqa: BLE001
        pass


def clear() -> None:
    """Delete the store. Test helper; never called in production."""
    global _cached
    _cached = None
    try:
        store_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


def reset_cache() -> None:
    global _cached
    _cached = None


def snapshot() -> FailOpenCounts:
    """Aggregate the store. Unreadable content yields ``readable=False``."""
    global _cached
    if _cached is not None:
        return _cached

    path = store_path()
    if not path.exists():
        _cached = FailOpenCounts()
        return _cached

    by_code: dict[str, int] = {}
    malformed = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()[-_MAX_EVENTS:]
    except Exception:  # noqa: BLE001
        _cached = FailOpenCounts(readable=False)
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
        _cached = FailOpenCounts(readable=False)
        return _cached
    _cached = FailOpenCounts(by_code=by_code)
    return _cached
