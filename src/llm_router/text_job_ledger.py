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
