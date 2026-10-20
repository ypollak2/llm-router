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

import json
import time

from llm_router import paths

LEDGER_FILENAME = "text_job_outcomes.jsonl"

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
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", opener=paths.private_opener) as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---- LC-8b: per-attempt LOCAL outcomes, the demotion monitor's input ---------------------------------
#
# The row above holds only the FINAL served model. A local model that errored or timed out and was
# rescued by the next chain entry shows there under the rescuer, and an attempt-1 reject followed by an
# attempt-2 accept reads as accepted. So a dead or slow Ollama never demoted (review of #675). This
# sidecar holds one row per LOCAL attempt: ``{"ts", "job", "model", "outcome"}``, outcome
# ``ok`` (acceptance check passed), ``rejected`` (answered, check failed) or ``error`` (the local model
# was tried and the router moved on: error, timeout, contract rejection). No prompt text.

LOCAL_ATTEMPTS_FILENAME = "text_job_local_attempts.jsonl"
OUTCOME_OK = "ok"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"


def _local_name(model: str) -> str | None:
    head, sep, rest = model.partition("/")
    return rest if sep and head in ("ollama", "ollama_chat") else None


def _append_local_attempt(job: str, model: str, outcome: str) -> None:
    row = {"ts": time.time(), "job": job, "model": model, "outcome": outcome}
    try:
        path = paths.state_path(LOCAL_ATTEMPTS_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", opener=paths.private_opener) as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def record_local_attempts(*, job: str, served_model: str, chain_attempts: list[str], accepted: bool) -> list[str]:
    """Record one ``llm_text_job`` attempt's local outcomes; returns the local models touched.

    Every ``ollama/*`` entry in ``chain_attempts`` other than the served model was tried and failed
    (``error``), even though a later entry rescued the call. If the served model is local, the
    acceptance check decides ``ok`` or ``rejected``. Best-effort: never raises.
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
                _append_local_attempt(job, m, OUTCOME_ERROR)
                touched.append(m)
        if served_n in local or (served_model.startswith(("ollama/", "ollama_chat/")) if isinstance(served_model, str) else False):
            _append_local_attempt(job, served_model, OUTCOME_OK if accepted else OUTCOME_REJECTED)
            touched.append(served_model)
        return touched
    except Exception:
        return []
