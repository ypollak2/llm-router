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

PHASES (M4.1). A hook may also name where its time went. ``phase(name)`` is a
context manager that adds the block's wall time to ``phases_ms[name]``; the row
then carries ``"phases_ms":{"import":12.1,"session_io":3.4,"zce":0.2}``.
``mark_main_start()`` records ``import``: the time from the clock's start (before
the first ``llm_router`` import) to the moment ``main()`` begins. Phases are
plain milliseconds that a reader subtracts from ``elapsed_ms`` to get "other"; a
phase nested inside another is the reader's business (``cold_wait`` lives inside
``draft_chain`` or ``zce``). A run that names no phase writes the same row as before.
Outside a hook process (no ``begin`` call: the MCP server, tests) every phase call
is a no-op, so nothing accumulates in a long-lived process. Cost: two clock reads
and one dict update per phase, measured in ``scripts/bench_hook_latency.py micro``.

LOAD (PLAN v16 P0.9-g). A hook row also carries ``load1``, the 1-minute load
average (``os.getloadavg()[0]``) read in the exit handler after the clock has
stopped, so it is not inside ``elapsed_ms``. A reader excludes and counts rows
above the contention bar (``hook_wall.MAX_LOAD``) instead of averaging over a
burst (AMEND R8 A.3). ``record-raw`` rows (the statusline) do not carry it.

``timed_out`` means ``elapsed_ms >= HOOK_TIMEOUTS_MS[hook]`` (the host's
timeout, 60 s by default): the invocation used all the time the host gives it.
The PRD latency bar a hook is judged against is ``HOOK_BUDGETS_MS``.

ROUTER-ADDED TIME (PLAN v16 P0.9). A row whose run named a model phase
(``MODEL_PHASES``: ``draft_chain``, ``zce_model``, ``codex_delegation``, ``cold_wait``) also carries
``router_added_ms`` = ``elapsed_ms`` minus the model time, with a ``cold_wait``
reported inside another model phase subtracted once. ``set_session(id)`` adds
``session_id`` so a reader can count sessions. ``host`` names the CLI that ran the
hook (``claude_code``, ``codex``, ``gemini``) and is written only on positive evidence
(:func:`detect_host`: the installed script's name or directory, the plugin root the host
exported, Claude Code's own environment); a row whose host could not be told has no
``host`` (P0.14-d: a Codex turn never goes through the Claude Code proxy, so the
ledger-silence alert must not count it, and Codex runs this repo's own plugin as
``auto-route.py``). ``base_url`` is set when the hook process inherited a non-empty
``ANTHROPIC_BASE_URL``: ``loopback:<port>`` or ``other``, never the value itself (a
session launched with its own base URL bypasses the proxy on purpose). A row written
before either field existed has neither. A process the host KILLS at its timeout never reaches
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
import sys
import time
from pathlib import Path

from llm_router import capped_log
from llm_router.paths import state_path

__all__ = [
    "HOOK_BUDGETS_MS",
    "DEFAULT_BUDGET_MS",
    "STORE_FILENAME",
    "begin",
    "phase",
    "add_phase",
    "mark_main_start",
    "record",
    "read_rows",
    "store_path",
    "budget_ms",
    "timeout_ms",
    "max_bytes",
    "HOOK_TIMEOUTS_MS",
    "MODEL_PHASES",
    "set_session",
    "model_ms",
    "router_added_ms",
]

STORE_FILENAME = "hook_latency.jsonl"

#: Per-generation size cap. ~110 bytes a row, so ~38k rows; two generations are
#: kept. Sized so a busy day of per-tool-call hooks (a few thousand rows) leaves
#: roughly a week in the window ``llm-router kpi`` reads.
_DEFAULT_MAX_BYTES = 4 * 1024 * 1024

#: THE ONE TABLE of hook budgets, in milliseconds: the PRD's latency bars
#: (NFR-LAT, metric S6; PLAN v16 P0.9). A hook's p95 is held to its bar by
#: ``llm-router kpi`` (G1), judged on ``router_added_ms`` -- the elapsed time
#: minus the time spent waiting on a model (``MODEL_PHASES``), because a local
#: draft that runs for 12 s is the answer, not overhead the router added.
#:
#: * every synchronous hook: 300 ms ("+300 ms sync p95");
#: * ``session-start``: 2,000 ms ("session-start 2 s");
#: * ``statusline``: 100 ms ("statusline 100 ms"). Not a hook: the statusline
#:   shell command records a sampled timing row through ``record-raw`` (see
#:   ``hooks/statusline-command.sh``, ``LLM_ROUTER_STATUSLINE_TIMING``).
#:
#: These used to be the host's timeouts (auto-route 60 s, agent-route 320 s) or
#: declared 2-10 s budgets, so the scorecard called a 16 s auto-route p95
#: "within budget". The host timeouts still decide ``timed_out`` (the row field
#: means "used the whole time the host gives it"); they live in
#: ``HOOK_TIMEOUTS_MS`` below.
HOOK_BUDGETS_MS: dict[str, int] = {
    "agent-route": 300,
    "auto-route": 300,
    "status-bar": 300,
    "enforce-route": 300,
    "subagent-start": 300,
    "agent-depth-release": 300,
    "cc-usage-track": 300,
    "usage-refresh": 300,
    "playwright-compress": 300,
    "bash-compress": 300,
    "session-start": 2_000,
    "session-end": 300,
    "statusline": 100,
}

#: A hook not in the table is held to the PRD's sync bar, rather than to no bar.
DEFAULT_BUDGET_MS = 300

#: What ``timed_out`` is measured against: the wall-clock time the HOST gives
#: the hook before it kills it.
#:
#: * ``agent-route`` -- 320 s, ``install_hooks._AGENT_ROUTE_HOOK_TIMEOUT_SEC``
#:   (the Codex delegation can run for 300 s; a test pins the two together).
#: * ``auto-route`` -- 60 s, the ``timeout`` registered in settings.json. The
#:   hook budgets itself to 55 s so it exits with an answer rather than being
#:   killed holding one.
#: * the rest register no timeout, so the host default applies (60 s,
#:   MEASUREMENT.md).
HOOK_TIMEOUTS_MS: dict[str, int] = {
    "agent-route": 320_000,
}

#: The host's default hook timeout (MEASUREMENT.md).
DEFAULT_TIMEOUT_MS = 60_000

#: Phases that are time spent waiting on a model, not time the router added:
#: ``draft_chain`` (the whole DIRECT draft chain), ``zce_model`` (the
#: zero-Claude edit's model call) and ``cold_wait`` (Ollama's own model-load
#: time). ``cold_wait`` is usually reported from INSIDE one of the other two;
#: the recorder knows when it is (``_model_depth``) and subtracts it once.
MODEL_PHASES: tuple[str, ...] = ("draft_chain", "zce_model", "codex_delegation", "cold_wait")

_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})

# Indirection so tests drive a fake clock without touching the global ``time``
# module (the process clock is shared with pytest, xdist and the logger).
_monotonic = time.monotonic
_wall = time.time
_getloadavg = getattr(os, "getloadavg", None)  # absent on Windows: no load1 on the row

#: The one invocation this process is timing: set by ``begin``, read at exit.
_pending: dict | None = None
_registered = False
#: Milliseconds per named phase for the invocation in ``_pending`` (M4.1).
_phases: dict[str, float] = {}
#: How many model phases (``MODEL_PHASES`` opened with ``phase``) are open now,
#: and the model time reported while one was already open: that time is inside
#: the enclosing phase and must not be subtracted twice.
_model_depth = 0
_nested_model_ms = 0.0
#: The host's session id for this invocation, when the hook has read it.
_session_id: str | None = None


def store_path() -> Path:
    return state_path(STORE_FILENAME)


def budget_ms(hook: str) -> int:
    """The hook's PRD latency bar (``HOOK_BUDGETS_MS``)."""
    return HOOK_BUDGETS_MS.get(hook, DEFAULT_BUDGET_MS)


def timeout_ms(hook: str) -> int:
    """The time the host gives the hook before killing it (``HOOK_TIMEOUTS_MS``)."""
    return HOOK_TIMEOUTS_MS.get(hook, DEFAULT_TIMEOUT_MS)


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


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/\\")
    if not root:
        return False
    if path == root or path.startswith(root + os.sep) or path.startswith(root + "/"):
        return True
    try:
        real, real_root = os.path.realpath(path), os.path.realpath(root)
    except (OSError, ValueError):
        return False
    return real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep)


def detect_host(script: str) -> str | None:
    """The CLI that ran this hook process, or None when nothing says.

    In order: the installed name (``install`` copies ``auto-route.py`` to
    ``codex-auto-route.py`` / ``gemini-cli-auto-route.py``); a ``.codex`` / ``.gemini``
    directory in the script's path (Codex caches plugins under ``~/.codex/plugins``); the
    plugin root the host exported (``CODEX_PLUGIN_ROOT`` / ``CLAUDE_PLUGIN_ROOT``) when it
    contains the script; Claude Code's own environment (``CLAUDE_CODE_ENTRYPOINT``,
    ``CLAUDECODE``); a ``.claude`` directory in the path. A bare ``auto-route.py`` is NOT
    Claude Code's by name: ``.codex-plugin/hooks.json`` runs that same file under Codex.
    The Codex signals come first: Codex started from a Claude Code shell inherits
    ``CLAUDECODE``."""
    path = str(script or "")
    name = os.path.basename(path).lower()
    if name.startswith("codex-"):
        return "codex"
    if name.startswith("gemini"):
        return "gemini"
    parts = {p.lower() for p in path.replace("\\", "/").split("/")}
    if ".codex" in parts:
        return "codex"
    if ".gemini" in parts:
        return "gemini"
    codex_root = (os.environ.get("CODEX_PLUGIN_ROOT") or "").strip()
    if codex_root and _under(path, codex_root):
        return "codex"
    claude_root = (os.environ.get("CLAUDE_PLUGIN_ROOT") or "").strip()
    if claude_root and _under(path, claude_root):
        return "claude_code"
    if (os.environ.get("CLAUDE_CODE_ENTRYPOINT") or "").strip() or (os.environ.get("CLAUDECODE") or "").strip():
        return "claude_code"
    if ".claude" in parts:
        return "claude_code"
    return None


def base_url_class() -> str | None:
    """``ANTHROPIC_BASE_URL`` as this hook process inherited it, reduced to
    ``loopback:<port>`` / ``loopback`` / ``other``; None when unset or blank. Never the
    value: it can carry a key pasted into the wrong field."""
    value = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
    if not value:
        return None
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(value if "//" in value else "//" + value)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "other"
    if host in ("127.0.0.1", "localhost", "::1"):
        return f"loopback:{port}" if port else "loopback"
    return "other"


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
        _pending = {"hook": hook, "event": event, "t0": _monotonic() if t0 is None else t0,
                    "host": detect_host(sys.argv[0] if sys.argv else ""),
                    "base_url": base_url_class()}
        if not _registered:
            # atexit runs handlers last-in-first-out, and this is registered
            # before any the hook adds later (hook_liveness.clear_marker), so it
            # runs after them and their cost is inside the number.
            atexit.register(_finish)
            _registered = True
    except Exception:  # noqa: BLE001 -- timing must never break the hook
        _pending = None


def set_event(event: str) -> None:
    """Relabel the event of the invocation being timed. For a script registered
    on two events (session-end: Stop and SessionEnd) that learns which one fired
    only after reading stdin. No-op when nothing is being timed. Never raises."""
    try:
        if _pending is not None:
            _pending["event"] = event
    except Exception:  # noqa: BLE001 -- timing must never break the hook
        return


def add_phase(name: str, ms: float) -> None:
    """Add ``ms`` to the named phase of the invocation being timed. No-op when
    nothing is being timed (not a hook process). Never raises."""
    global _nested_model_ms
    try:
        if _pending is None:
            return
        _phases[name] = _phases.get(name, 0.0) + float(ms)
        if name in MODEL_PHASES and _model_depth > 0:
            _nested_model_ms += float(ms)
    except Exception:  # noqa: BLE001 -- timing must never break the hook
        return


def set_session(session_id: object) -> None:
    """Name the host session this invocation belongs to, so a reader can count
    sessions and split organic from research rows (PLAN v16 §1.4 rules 4-5).
    A non-string or empty id is ignored. Never raises."""
    global _session_id
    try:
        if isinstance(session_id, str) and session_id.strip():
            _session_id = session_id.strip()[:128]
    except Exception:  # noqa: BLE001
        return


class _Phase:
    """``with phase("zce"): ...`` -- never suppresses an exception or SystemExit."""

    __slots__ = ("name", "t")

    def __init__(self, name: str) -> None:
        self.name = name
        self.t = 0.0

    def __enter__(self) -> "_Phase":
        global _model_depth
        if self.name in MODEL_PHASES:
            _model_depth += 1
        self.t = _monotonic()
        return self

    def __exit__(self, *exc: object) -> bool:
        global _model_depth
        ms = (_monotonic() - self.t) * 1000.0
        if self.name in MODEL_PHASES:
            _model_depth = max(0, _model_depth - 1)
        add_phase(self.name, ms)
        return False


def phase(name: str) -> _Phase:
    return _Phase(name)


def mark_main_start() -> None:
    """Record ``import``: clock start (before the first llm_router import) to now.
    Call once, as the first statement of ``main()``. Never raises."""
    try:
        pending = _pending
        if pending is not None and "import" not in _phases:
            _phases["import"] = (_monotonic() - pending["t0"]) * 1000.0
    except Exception:  # noqa: BLE001
        return


def model_ms(phases_ms: dict | None, nested_ms: float = 0.0) -> float:
    """Milliseconds of ``phases_ms`` spent waiting on a model: the sum of the
    ``MODEL_PHASES`` present, minus ``nested_ms`` already inside another."""
    if not isinstance(phases_ms, dict):
        return 0.0
    total = 0.0
    for name in MODEL_PHASES:
        v = phases_ms.get(name)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            total += float(v)
    return max(0.0, total - max(0.0, float(nested_ms)))


def router_added_ms(row: dict) -> float | None:
    """The time the router added on one row: ``router_added_ms`` when the
    recorder wrote it; else, for a row written before it did,
    ``elapsed_ms`` minus the outer model phases (``draft_chain``, ``zce_model``,
    ``codex_delegation``), plus ``cold_wait`` only when
    neither of those is present (older rows cannot say where ``cold_wait`` was
    nested, so it is never subtracted twice: the reading errs high, not low);
    else ``elapsed_ms``. None for a row without a numeric ``elapsed_ms``."""
    elapsed = row.get("elapsed_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        return None
    given = row.get("router_added_ms")
    if isinstance(given, (int, float)) and not isinstance(given, bool):
        return max(0.0, float(given))
    phases = row.get("phases_ms")
    if not isinstance(phases, dict):
        return max(0.0, float(elapsed))
    outer = {k: phases[k] for k in MODEL_PHASES if k != "cold_wait" and k in phases}
    if not outer and "cold_wait" in phases:
        outer = {"cold_wait": phases["cold_wait"]}
    return max(0.0, float(elapsed) - model_ms(outer))


def _finish() -> None:
    pending = _pending
    if pending is None:
        return
    elapsed = (_monotonic() - pending["t0"]) * 1000.0
    phases = dict(_phases) if _phases else None
    added = None
    if phases and any(k in phases for k in MODEL_PHASES):
        added = max(0.0, elapsed - model_ms(phases, _nested_model_ms))
    record(pending["hook"], pending["event"], elapsed, phases_ms=phases,
           router_added_ms=added, session_id=_session_id, host=pending.get("host"),
           base_url=pending.get("base_url"), load1=_load1())


def _load1() -> float | None:
    """The 1-minute load average, or None where the OS cannot say. Never raises."""
    try:
        return _getloadavg()[0]
    except Exception:  # noqa: BLE001 -- timing must never break the hook
        return None


def record(
    hook: str, event: str, elapsed_ms: float, *, now: float | None = None,
    phases_ms: dict[str, float] | None = None, router_added_ms: float | None = None,
    session_id: str | None = None, host: str | None = None, base_url: str | None = None,
    load1: float | None = None,
) -> bool:
    """Append one row. Returns True when it was written. Never raises."""
    try:
        if _disabled():
            return False
        elapsed_ms = max(0.0, float(elapsed_ms))
        row = {
            "hook": hook,
            "event": event,
            "elapsed_ms": round(elapsed_ms, 1),
            "timed_out": elapsed_ms >= timeout_ms(hook),
            "ts": round(_wall() if now is None else now, 3),
        }
        if phases_ms:
            row["phases_ms"] = {str(k): round(float(v), 1) for k, v in phases_ms.items()}
        if router_added_ms is not None:
            row["router_added_ms"] = round(max(0.0, float(router_added_ms)), 1)
        if isinstance(session_id, str) and session_id:
            row["session_id"] = session_id
        if isinstance(host, str) and host:
            row["host"] = host
        if isinstance(base_url, str) and base_url:
            row["base_url"] = base_url
        if isinstance(load1, (int, float)) and not isinstance(load1, bool) and load1 == load1:
            row["load1"] = round(float(load1), 2)
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


# -- command line -------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    """``python -m llm_router.hook_latency record-raw <hook> <event> <elapsed_ms> [<session_id>]``

    Appends one row for a process that cannot import this module in-line (the
    statusline shell command). The optional session id goes on the row, so a
    reader can count sessions and drop research / executor ones (PLAN v16 §1.4
    rules 4 and 8); an empty one is left off. Silent: exit 0 when written, 1
    when not, 2 on a usage error. Never prints on success, since a caller may be
    a status line."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (4, 5) or args[0] != "record-raw":
        print("usage: python -m llm_router.hook_latency record-raw <hook> <event> <elapsed_ms>"
              " [<session_id>]", file=sys.stderr)
        return 2
    _cmd, hook, event, raw = args[:4]
    sid = args[4].strip()[:128] if len(args) == 5 else ""
    try:
        elapsed = float(raw)
    except ValueError:
        return 2
    if not hook or elapsed != elapsed or elapsed < 0:  # NaN or negative
        return 2
    return 0 if record(hook, event, elapsed, session_id=sid or None) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
