"""How long each hook invocation actually ran -- the missing half of KPI G1.

KPI G1 (added latency) had two halves. The proxy half was measured
(``added_latency_s`` on every ``proxy_calls.jsonl`` row). The hook half was not:
``llm-router kpi`` printed "hook latency is not instrumented", because nothing
recorded how long a hook ran. A hook sits in front of every prompt
(UserPromptSubmit), every tool call (PreToolUse) and every session start, so
the half nobody timed is the half the user feels most often.

WHAT IS RECORDED. One line per invocation, appended when the process exits::

    {"hook":"enforce-route","event":"PreToolUse","elapsed_ms":212.4,
     "timed_out":false,"ts":1790606488.625}

``elapsed_ms`` runs from the hook script's first statement (``begin`` is called
with a ``time.monotonic()`` taken BEFORE the first ``llm_router`` import, so the
package import is inside the number) to the ``atexit`` handler. It does NOT
include interpreter start-up before the script's first line or interpreter
teardown after ``atexit``; both are fixed costs the hook cannot see. The
measured gap against an external stopwatch is in the PR that added this file.

``timed_out`` means ``elapsed_ms >= HOOK_BUDGETS_MS[hook]``: the invocation used
its whole budget. A process the host KILLS at its timeout never reaches
``atexit``, so a kill leaves no row here; kills are counted separately
(``CHZ-HOOK-KILLED`` in the fail-open store, timestamped since the same PR) and
``llm-router kpi`` prints them beside this log.

THE COST OF RECORDING IT -- the guardrail this module answers to. The write is
one ``os.open(O_APPEND)`` + one ``os.write`` + one ``os.fstat`` + one
``os.close`` of a ~110 byte line, no lock, no read, no ``mkdir`` on the warm
path. A concurrent writer cannot tear a line (single ``O_APPEND`` write of less
than a page) and cannot make another writer wait. The size cap is checked with
the ``fstat`` that is already there; the only lock in the module is taken
non-blocking on the rare rotation, and a writer that cannot get it simply skips
the rotation and tries again next time.

BOUNDED. Two generations are kept (``hook_latency.jsonl`` and ``.1``), each at
most ``LLM_ROUTER_HOOK_LATENCY_MAX_BYTES`` (default 4 MiB, ~38k rows). Rotation
is ``os.replace`` of the full file, decided by whichever writer wins the
non-blocking lock AFTER it re-checks the size, so two writers cannot rotate the
same file twice and overwrite the previous generation with a nearly empty one.

FAIL-OPEN. Every public function swallows its own errors. A failed write is
counted (``CHZ-FO-HOOK-LATENCY-WRITE``) rather than printed: a hook's stdout is
parsed as JSON by the host, and its stderr is shown to the user.

``LLM_ROUTER_HOOK_LATENCY=off`` turns the recorder off entirely.
"""

from __future__ import annotations

import atexit
import json
import os
import time
from pathlib import Path

from llm_router import capped_log
from llm_router.paths import state_path

__all__ = [
    "HOOK_BUDGETS_MS",
    "DEFAULT_BUDGET_MS",
    "STORE_FILENAME",
    "begin",
    "record",
    "read_rows",
    "store_path",
    "budget_ms",
    "max_bytes",
]

STORE_FILENAME = "hook_latency.jsonl"

#: Per-generation size cap. ~110 bytes a row, so ~38k rows; two generations are
#: kept. Sized so a busy day of per-tool-call hooks (a few thousand rows) leaves
#: roughly a week in the window ``llm-router kpi`` reads.
_DEFAULT_MAX_BYTES = 4 * 1024 * 1024

#: THE ONE TABLE of hook budgets, in milliseconds. A hook's budget is the
#: wall-clock time it may use before it counts as ``timed_out`` here, and the
#: bar ``llm-router kpi`` holds that hook's p95 against.
#:
#: Where the host enforces a timeout, the budget IS that timeout:
#:
#: * ``agent-route`` -- 320 s, ``install_hooks._AGENT_ROUTE_HOOK_TIMEOUT_SEC``
#:   (the Codex delegation can run for 300 s; a test pins the two together).
#: * ``auto-route`` -- 60 s, the ``timeout`` registered in settings.json. The
#:   hook budgets itself to 55 s so it exits with an answer rather than being
#:   killed holding one. Its p95 is dominated by DIRECT local-model runs
#:   (11-28 s per model, measured), so this is deliberately the host's number
#:   and not a fast-path target.
#:
#: The rest register no timeout, so the host default (60 s, MEASUREMENT.md)
#: applies; that number is far too large to say anything about "added latency",
#: so each carries a DECLARED budget instead: per-tool-call and per-prompt
#: hooks 2 s, the ones that do real work 5-10 s. They are declared, not derived
#: from live traffic -- there was none to derive them from before this log
#: existed. The fast-path wall times measured when the log was added (35-50 ms
#: for the per-call hooks, ~0.4 s for session-end) sit far below them. Re-set
#: them from the first week of real p95s.
#:
#: ``context-capture`` is the one registered hook not listed: it imports
#: ``llm_router`` only inside functions, so arming the recorder would add the
#: package import to every call that otherwise exits early. It needs a
#: stdlib-only recorder first.
HOOK_BUDGETS_MS: dict[str, int] = {
    "agent-route": 320_000,
    "auto-route": 60_000,
    "status-bar": 2_000,
    "enforce-route": 2_000,
    "subagent-start": 2_000,
    "agent-depth-release": 2_000,
    "cc-usage-track": 2_000,
    "usage-refresh": 5_000,
    "playwright-compress": 5_000,
    "bash-compress": 5_000,
    "session-start": 10_000,
    "session-end": 10_000,
}

#: A hook not in the table is held to this, rather than to no budget at all.
DEFAULT_BUDGET_MS = 5_000

_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})

# Indirection so tests drive a fake clock without touching the global ``time``
# module (the process clock is shared with pytest, xdist and the logger).
_monotonic = time.monotonic
_wall = time.time

#: The one invocation this process is timing: set by ``begin``, read at exit.
_pending: dict | None = None
_registered = False


def store_path() -> Path:
    return state_path(STORE_FILENAME)


def budget_ms(hook: str) -> int:
    return HOOK_BUDGETS_MS.get(hook, DEFAULT_BUDGET_MS)


def max_bytes() -> int:
    raw = os.environ.get("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_BYTES
    return value if value > 0 else _DEFAULT_MAX_BYTES


def _disabled() -> bool:
    return os.environ.get("LLM_ROUTER_HOOK_LATENCY", "").strip().lower() in _OFF_VALUES


# -- write side ---------------------------------------------------------------


def begin(hook: str, event: str, t0: float | None = None) -> None:
    """Start timing this invocation; the row is written when the process exits.

    ``t0`` is a ``time.monotonic()`` the hook took before importing ``llm_router``
    (so the import is inside the measurement); omitted, it is taken now. Calling
    this twice in one process keeps the first call's start. Never raises.
    """
    global _pending, _registered
    try:
        if _disabled() or _pending is not None:
            return
        _pending = {"hook": hook, "event": event, "t0": _monotonic() if t0 is None else t0}
        if not _registered:
            # atexit runs handlers last-in-first-out, and this is registered
            # before any the hook adds later (hook_liveness.clear_marker), so it
            # runs after them and their cost is inside the number.
            atexit.register(_finish)
            _registered = True
    except Exception:  # noqa: BLE001 -- timing must never break the hook
        _pending = None


def _finish() -> None:
    pending = _pending
    if pending is None:
        return
    record(pending["hook"], pending["event"], (_monotonic() - pending["t0"]) * 1000.0)


def record(hook: str, event: str, elapsed_ms: float, *, now: float | None = None) -> bool:
    """Append one row. Returns True when it was written. Never raises."""
    try:
        if _disabled():
            return False
        elapsed_ms = max(0.0, float(elapsed_ms))
        row = {
            "hook": hook,
            "event": event,
            "elapsed_ms": round(elapsed_ms, 1),
            "timed_out": elapsed_ms >= budget_ms(hook),
            "ts": round(_wall() if now is None else now, 3),
        }
        capped_log.append(
            store_path(), (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"), max_bytes()
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _note_write_failure(exc)
        return False


def _note_write_failure(exc: BaseException) -> None:
    try:
        from llm_router import failopen

        failopen.record("CHZ-FO-HOOK-LATENCY-WRITE", exc)
    except Exception:  # noqa: BLE001 -- the accounting of an accounting failure
        # cannot itself be allowed to fail the hook; failopen.record already
        # never raises, so this only guards the import.
        return


# -- read side ----------------------------------------------------------------


def read_rows(since: float | None = None, until: float | None = None) -> list[dict]:
    """Rows with a numeric ``ts`` and ``elapsed_ms`` inside ``[since, until]``,
    oldest first, from both generations. A malformed or half-written line is
    skipped, never read as zero. Never raises."""
    try:
        rows = []
        for row in capped_log.read_dicts(store_path()):
            ts, ms = row.get("ts"), row.get("elapsed_ms")
            if isinstance(ts, bool) or isinstance(ms, bool):
                continue
            if not isinstance(ts, (int, float)) or not isinstance(ms, (int, float)):
                continue
            if (since is not None and ts < since) or (until is not None and ts > until):
                continue
            rows.append(row)
        rows.sort(key=lambda r: r["ts"])
        return rows
    except Exception:  # noqa: BLE001 -- a reader never raises into the scorecard
        return []
