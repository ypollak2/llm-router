"""Outcome ledger for ``llm_text_job`` (commit messages, PR descriptions,
long-output summaries routed to a local model with a Claude fallback).

Mirrors ``edit_ledger.py``'s shape and reasoning exactly: one append-only JSONL
row per call, written to ``~/.llm-router/text_job_outcomes.jsonl`` (or
``$LLM_ROUTER_HOME/text_job_outcomes.jsonl`` — see ``paths.py``), so live
acceptance can be measured instead of assumed. This feature shipped with ZERO
real-traffic evidence that Claude actually calls it (same as ``llm_edit``'s
documented 30-day zero-use finding in ``edit_ledger.py``) — the ledger is what
would tell us otherwise.

Row shape::

    {"ts": 1759392000.0, "session_id": "abc123", "job": "commit_message",
     "model": "ollama/qwen3.5:latest", "status": "accepted", "attempts": 1}

``status`` is one of:

* ``"accepted"``   — a local attempt was made, its acceptance check passed,
  the result is returned for direct use.
* ``"rejected"``   — one or more local attempts were made but none passed the
  acceptance check; the caller is told to fall back (to Claude, or to the raw
  input) rather than use the local text.
* ``"fallback"``   — no local attempt was made at all (feature flag off, or
  the local backend could not be reached) — distinct from "rejected" because
  nothing was judged.

``session_id`` is resolved via :func:`llm_router.session_store.resolve_session_id`
— the same resolver every other MCP-path ledger writer uses (see
``edit_ledger.py``'s module docstring for why a direct env read is wrong here:
the MCP server is a long-lived process that does not reliably see
``CLAUDE_SESSION_ID``). ``None`` (JSON ``null``) means unresolved; never ``""``.

Fail-silent: a broken ledger write must never break the ``llm_text_job`` call
the caller is waiting on.
"""

from __future__ import annotations

import errno
import json
import re
import socket
import time
from collections.abc import Iterable

from llm_router import capped_log, paths

LEDGER_FILENAME = "text_job_outcomes.jsonl"
# Eight times the demotion reader's 512 KiB tail; two generations are kept.
MAX_LEDGER_BYTES = 4 * 1024 * 1024

STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_FALLBACK = "fallback"


def _resolve_session_id() -> str | None:
    """Never raises: an identity-resolution failure must not break the call."""
    try:
        from llm_router.session_store import resolve_session_id
        return resolve_session_id()
    except Exception:
        return None


def record_text_job_outcome(*, job: str, model: str, status: str, attempts: int) -> None:
    """Append one ledger row. Best-effort: never raises."""
    row = {
        "ts": time.time(),
        "session_id": _resolve_session_id(),
        "job": job,
        "model": model,
        "status": status,
        "attempts": int(attempts),
    }
    try:
        path = paths.state_path(LEDGER_FILENAME)
        capped_log.append(
            path, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"),
            MAX_LEDGER_BYTES,
        )
    except Exception:
        pass


# ---- LC-8b: per-attempt LOCAL outcomes, the demotion monitor's input ---------------------------------
#
# The row above holds only the FINAL served model. A local model that errored or timed out and was
# rescued by the next chain entry shows there under the rescuer, and an attempt-1 reject followed by an
# attempt-2 accept reads as accepted. So a dead or slow Ollama never demoted (review of #675). This
# sidecar holds one row per LOCAL attempt: ``{"ts", "job", "model", "outcome"}`` (plus ``jid``, ``try`` and
# ``more``, below), outcome ``ok`` (acceptance check passed), ``rejected`` (answered, check failed), ``error``
# (the local model was tried and the router moved on: error, contract rejection) or ``infra`` (the attempt failed
# for an infrastructure reason: queueing timeout, connection failure, server busy or 5xx, model not loaded; see
# :func:`is_infra_error`). No prompt text.
#
# ``infra`` is NOT a quality signal. The demotion monitor (``local_check.routing.demotion_check``) never counts
# a job that has an ``infra`` row and no ``ok`` row: parallel sub-agents queueing on a single-slot Ollama time out
# for reasons that say nothing about the model's answers, and counting them switched a good lane off. Rows written
# before ``infra`` existed read exactly as before.
#
# D-367: the monitor counts JOBS, not rows. Each ``llm_text_job`` call has a ``jid`` (short uuid hex) written on
# all its rows with the ``try`` number; a row also says ``more: true`` when further tries of that job follow. A job
# is ok when any row is ``ok``; failed when its rows are all ``rejected``/``error``; not counted at all when it has
# an ``infra`` row (and no ``ok``) or when its last row says ``more`` (no verdict yet). A row with no ``jid``
# (written before this) is its own one-try job.

LOCAL_ATTEMPTS_FILENAME = "text_job_local_attempts.jsonl"
OUTCOME_OK = "ok"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"
OUTCOME_INFRA = "infra"

#: Class names (lower case, matched along the exception's MRO, or on the name in a router chain-failure
#: line) that positively identify an infrastructure failure. litellm: ``Timeout`` (a subclass of
#: ``APIConnectionError``), ``APIConnectionError``, ``ServiceUnavailableError``, ``InternalServerError``,
#: ``BadGatewayError``; the router: ``WallClockExceeded`` / ``DeadlineExceeded`` (both ``TimeoutError``) and
#: ``AttemptCapExceeded`` (the MCP door's per-attempt cap); builtins and httpx/aiohttp network errors.
#: Anything not named here is NOT infrastructure: it stays ``error``.
_INFRA_NAMES = frozenset({
    "timeout", "apitimeouterror", "apiconnectionerror", "serviceunavailableerror", "internalservererror",
    "badgatewayerror", "gatewaytimeouterror", "attemptcapexceeded", "wallclockexceeded", "deadlineexceeded",
    "timeouterror", "connectionerror", "connectionrefusederror", "connectionreseterror",
    "connectionabortederror", "brokenpipeerror", "gaierror", "connecterror", "connecttimeout",
    "readtimeout", "writetimeout", "pooltimeout", "timeoutexception", "readerror", "writeerror",
    "remoteprotocolerror", "networkerror", "serverdisconnectederror", "servertimeouterror",
    "clientconnectorerror",
})
#: HTTP statuses that mean the server, not the answer: request timeout, 5xx other than 501/505.
_INFRA_STATUS = frozenset({408, 500, 502, 503, 504})
_INFRA_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTDOWN})
#: The router lists every failed chain entry as ``  N. <model>: <ErrorType>: <message>``.
_CHAIN_FAILURE_RE = re.compile(r"(?m)^\s+\d+\.\s+(\S+?):\s+(\w+):\s?(.*)$")
_CAUSE_DEPTH = 8


def _infra_signal(names: Iterable[str], status: object, message: str) -> bool:
    """True only on a positive match: an infra class name, an infra HTTP status, or a model-not-found 404."""
    names = set(names)
    if names & _INFRA_NAMES:
        return True
    if isinstance(status, int) and not isinstance(status, bool) and status in _INFRA_STATUS:
        return True
    low = message.lower()
    return "notfounderror" in names and "model" in low and "not found" in low


def _exc_is_infra(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    if isinstance(exc, OSError) and exc.errno in _INFRA_ERRNOS:
        return True
    names = {c.__name__.lower() for c in type(exc).__mro__}
    return _infra_signal(names, getattr(exc, "status_code", None), str(exc))


def _cause_chain(exc: BaseException | None) -> Iterable[BaseException]:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(seen) < _CAUSE_DEPTH:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__


def is_infra_error(exc: BaseException | None) -> bool:
    """True when ``exc`` or anything on its ``__cause__`` chain is POSITIVELY an infrastructure failure.

    Infrastructure = the call never produced an answer for a reason outside the model's quality: a timeout
    (queueing or contention), a refused or dropped connection, a busy or 5xx server, a model that is not
    installed or loaded. Conservative: an unknown exception type is ``False`` (stays an ``error``), and so
    is a cancellation (``CancelledError`` is a ``BaseException`` that records no attempt at all). Never raises.
    """
    try:
        return any(_exc_is_infra(e) for e in _cause_chain(exc))
    except Exception:  # noqa: BLE001 -- a classifier fault must read as "not infra", never break the job
        return False


_ENTRY_RE = re.compile(r"\s*(\w+):\s?(.*)", re.S)


def _model_is_infra(exc: BaseException | None, model: str, chain_errors: Iterable[tuple[str, str]] | None = None) -> bool:
    """Whether the failure of ONE local model in a multi-entry chain was infrastructure.

    The router lists each entry's own ``<ErrorType>: <message>``: on the response it served (``chain_errors``, a
    rescued failure) or inside the exhaustion error's text (``exc``), so a model that failed on its answer is not
    excused by a different entry's timeout (the exception's cause is only the LAST error). A model with no entry
    falls back to :func:`is_infra_error` of ``exc`` (a call cut off mid-dispatch); with neither, it is NOT infra.
    """
    try:
        want = _norm(model)
        found: tuple[str, str] | None = None
        if chain_errors is not None:
            for m, text in chain_errors:
                if _norm(m) == want:
                    parsed = _ENTRY_RE.match(text)
                    found = (parsed.group(1), parsed.group(2)) if parsed else ("", text)
        else:
            for e in _cause_chain(exc):
                for m in _CHAIN_FAILURE_RE.finditer(str(e)):
                    if _norm(m.group(1)) == want:
                        found = (m.group(2), m.group(3))
        if found is None:
            return exc is not None and is_infra_error(exc)
        return _infra_signal({found[0].lower()}, None, found[1])
    except Exception:  # noqa: BLE001 -- see is_infra_error
        return False


def _norm(model: str) -> str:
    from llm_router.local_check.routing import norm_model
    return norm_model(model)


def _local_name(model: str) -> str | None:
    head, sep, rest = model.partition("/")
    return rest if sep and head in ("ollama", "ollama_chat") else None


def _append_local_attempt(job: str, model: str, outcome: str, *, jid: str | None = None,
                          attempt: int | None = None, more: bool = False, acc: bool = False) -> None:
    row: dict = {"ts": time.time(), "job": job, "model": model, "outcome": outcome}
    try:    # who asked: lets a reader match this row to the call and session it belongs to (never by model and time)
        from llm_router import call_identity

        sid, tool = _resolve_session_id(), call_identity.tool_use_id()
        if sid:
            row["session_id"] = sid
        if tool:
            row["tool_use_id"] = tool
    except Exception:  # noqa: BLE001 -- identity is a refinement of the row; the row itself must still be written
        pass
    if jid:
        row["jid"] = jid  # D-367: the demotion monitor counts JOBS (every row of one call), not rows
        if attempt is not None:
            row["try"] = int(attempt)
        if more:
            row["more"] = True  # more tries of this job follow: it has no verdict yet
        if acc:
            row["acc"] = True  # this model failed, another one answered, and the job was ACCEPTED: the job is ok
    try:
        path = paths.state_path(LOCAL_ATTEMPTS_FILENAME)
        capped_log.append(
            path, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"),
            MAX_LEDGER_BYTES,
        )
    except Exception:
        pass


def record_local_attempts(
    *, job: str, served_model: str, chain_attempts: list[str], accepted: bool, exc: BaseException | None = None,
    jid: str | None = None, attempt: int | None = None, more: bool = False,
    chain_errors: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Record one ``llm_text_job`` attempt's local outcomes; returns the local models touched.

    Every ``ollama/*`` entry in ``chain_attempts`` other than the served model was tried and failed
    (``error``), even though a later entry rescued the call. If the served model is local, the
    acceptance check decides ``ok`` or ``rejected``. ``exc`` is the route error when the whole call failed:
    a local model whose own failure is positively infrastructure (see :func:`is_infra_error`) is recorded
    as ``infra`` instead of ``error``. For a RESCUED failure (a later entry served the call) the same test reads
    ``chain_errors``, the served response's per-entry failures; a failed entry with no recorded error stays
    ``error``. ``jid`` / ``attempt`` tag every row with the job (one
    ``llm_text_job`` call) and try number it belongs to; ``more`` says further tries of that job follow, so
    the rows are not its verdict yet. A failed entry's row also says ``acc`` when the call was accepted anyway
    (``accepted`` with another model serving): the job is ok, so a rescued failure never fails an accepted job.
    Best-effort: never raises.
    """
    try:
        from llm_router.quality_feedback import is_skip_marker
        from llm_router.local_check.routing import norm_model
        served_n = norm_model(served_model) if isinstance(served_model, str) else ""
        local: dict[str, str] = {}
        for m in chain_attempts or []:
            if isinstance(m, str) and not is_skip_marker(m) and _local_name(m) is not None:
                local.setdefault(norm_model(m), m)
        touched: list[str] = []
        for n, m in local.items():
            if n != served_n:
                infra = (exc is not None or isinstance(chain_errors, list)) and _model_is_infra(
                    exc, m, chain_errors if isinstance(chain_errors, list) else None)
                _append_local_attempt(job, m, OUTCOME_INFRA if infra else OUTCOME_ERROR,
                                      jid=jid, attempt=attempt, more=more, acc=accepted)
                touched.append(m)
        if served_n in local or (served_model.startswith(("ollama/", "ollama_chat/")) if isinstance(served_model, str) else False):
            _append_local_attempt(job, served_model, OUTCOME_OK if accepted else OUTCOME_REJECTED,
                                  jid=jid, attempt=attempt, more=more)
            touched.append(served_model)
        return touched
    except Exception:
        return []


def record_job_unfinished(*, job: str, models: Iterable[str], jid: str, attempt: int | None = None) -> None:
    """Mark a job that ended with no verdict (cancelled, or an error before its try was judged) as ``infra`` for
    each local model it already wrote rows for, so the demotion monitor never counts a half-finished job as
    failed (D-367). Best-effort: never raises."""
    for m in models:
        _append_local_attempt(job, m, OUTCOME_INFRA, jid=jid, attempt=attempt)
