"""Lowest-level execution trace: what the local model was asked, and what it did.

The agent loop had no instrumentation at all. When it returned "Agent reached
maximum iterations", nothing recorded which tools it had called, what they
returned, or whether it had ever read the file it was asked about — so a
failure and a stall were indistinguishable from the outside, and a benchmark
could score a stall as a pass without anyone being able to tell.

This is deliberately not the `logging` module. A trace is a *stream of facts
about one run*, correlated by a run id and readable after the fact by a tool
that knows the schema. Log lines are prose for a human reading in real time;
these are records for a viewer.

Off unless asked:

    LLM_ROUTER_TRACE=1                  # → ~/.llm-router/trace.jsonl
    LLM_ROUTER_TRACE_FILE=/tmp/x.jsonl  # → anywhere

Every function here swallows its own errors. Tracing must never be the reason a
routing decision fails.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_TRUTHY = ("1", "on", "true", "yes")
_RUN_ID = uuid.uuid4().hex[:12]
_seq = 0


def enabled() -> bool:
    if os.environ.get("LLM_ROUTER_TRACE", "").strip().lower() in _TRUTHY:
        return True
    return bool(os.environ.get("LLM_ROUTER_TRACE_FILE", "").strip())


def trace_path() -> Path:
    raw = os.environ.get("LLM_ROUTER_TRACE_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "trace.jsonl"


def _clip(value, limit: int = 600):
    """Bound a field. A trace that is too big to read is not evidence."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…<+{len(value) - limit}>"
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, default=str)
        except Exception:                                    # noqa: BLE001
            text = repr(value)
        return text if len(text) <= limit else text[:limit] + f"…<+{len(text) - limit}>"
    return value


def emit(event: str, **fields) -> None:
    """Append one fact. Never raises."""
    if not enabled():
        return
    global _seq
    try:
        _seq += 1
        record = {
            "ts": round(time.time(), 3),
            "run": _RUN_ID,
            "seq": _seq,
            "pid": os.getpid(),
            "event": event,
        }
        record.update({k: _clip(v) for k, v in fields.items()})
        path = trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:                                        # noqa: BLE001
        pass


@contextmanager
def span(event: str, **fields):
    """Emit `event`.start and `event`.end, the latter carrying a duration.

    The end record is emitted even when the body raises, and carries the
    exception — a span that simply stops is the failure mode this exists to make
    visible.
    """
    t0 = time.monotonic()
    emit(f"{event}.start", **fields)
    try:
        yield
    except BaseException as exc:                             # noqa: BLE001
        emit(f"{event}.end", ok=False, error=f"{type(exc).__name__}: {exc}",
             ms=int((time.monotonic() - t0) * 1000), **fields)
        raise
    else:
        emit(f"{event}.end", ok=True, ms=int((time.monotonic() - t0) * 1000), **fields)
