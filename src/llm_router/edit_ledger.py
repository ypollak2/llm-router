"""North Star lever 1 (``llm_edit``) outcome ledger.

NORTH_STAR.md's primary metric is: of every session's prompts and LLM calls,
what fraction is routed to a non-Claude model AND used as-is (no Claude
redo)? The lever map found ``llm_edit`` — a cheap model returns
``old_string``/``new_string`` pairs Claude applies mechanically — has ZERO
uses in 30 days of real traffic, despite being the one pattern proven to work
on this machine (rsi-engine's scaffold, see ``edit.py``'s module docstring).

This module is the missing signal: one JSONL row per file an ``llm_edit``
call touched, written to ``~/.llm-router/edit_outcomes.jsonl`` (or
``$LLM_ROUTER_HOME/edit_outcomes.jsonl`` — see ``paths.py`` for why the
resolution happens at call time and is never cached).

Row shape::

    {"ts": 1758975600.0, "session_id": "abc123", "file": "src/foo.py",
     "model": "ollama/qwen3.5:latest", "applied": true, "survived": null,
     "session_kind": "organic", "source": "zero_claude", "turn_id": "<16 hex>"}

``session_kind`` is the KPI tag from :mod:`llm_router.session_kind`
(organic / research / harness / headless), or null if the session was never tagged.

Field notes:

* ``source`` / ``turn_id`` (M0.3) — see :data:`SOURCES`. ``turn_id`` is ``prompt_key.key(prompt)``,
  a text-free hash of the human turn; O3 counts one local turn per (session_id, turn_id) among
  applied ``zero_claude`` rows.
* ``applied`` — True iff :func:`llm_router.edit.apply_edits` accepted the
  edit for this file: exact-once match, syntax-clean. This is "ready to use
  as-is", NOT proof Claude actually applied it — ``llm_edit`` never writes to
  disk itself, only the caller's own Edit tool does. Claiming more than that
  here would be exactly the overclaim NORTH_STAR's honesty section warns
  against.
* ``survived`` — always written as ``None``. Whether the edit was kept
  without a Claude redo is unknowable at call time (the call returns before
  Claude decides what to do with the result). ``scripts/northstar/
  edit_survival.py`` answers it after the fact by reading git history / the
  file on disk, and NS1's metric build (branch ``feat/northstar-metric``)
  reads this ledger for its routed+used-as-is signal.
* ``session_id`` — resolved via :func:`llm_router.session_store.resolve_session_id`,
  NOT ``os.environ.get("CLAUDE_SESSION_ID")`` directly. This call runs inside
  the MCP server, a long-lived stdio process shared by one Claude Code
  session; unlike a hook (which receives ``session_id`` in its own payload
  every invocation), the server process does not reliably see
  ``CLAUDE_SESSION_ID``/``CLAUDE_CODE_SESSION_ID`` in its environment (audit
  2026-09-28: a real ``llm_edit`` call wrote ``"session_id": ""``).
  ``resolve_session_id`` falls back to the ``current_session.json`` pointer
  a hook wrote for this session, which is how ``routing_quality.jsonl``'s
  MCP-path rows already carry a real session id (``router.py``'s
  ``_resolve_context_identity`` / ``stamp_trace`` call the same resolver).
  When even that pointer is missing or stale, the row carries ``None``
  (JSON ``null``) — never ``""``. ``""`` is indistinguishable from "resolved
  to the empty string"; ``null`` means "unknown", which is what it is.
  ``northstar.py``'s ``_fold_orphan_edit_rows`` documents the fallback join
  for rows that still land here with ``null``.

Fail-silent, matching every other best-effort telemetry writer in this
codebase (``tools/text.py``'s ``_cache_result``, ``_record_quality``) — a
broken ledger write must never break the ``llm_edit`` call the user is
waiting on.
"""

from __future__ import annotations

import json
import time

from llm_router import paths

LEDGER_FILENAME = "edit_outcomes.jsonl"


def _resolve_session_id() -> str | None:
    """Same resolver every other MCP-path writer uses (see module docstring).

    Never raises: an identity-resolution failure must not break the
    ``llm_edit`` call the user is waiting on.
    """
    try:
        from llm_router.session_store import resolve_session_id
        return resolve_session_id()
    except Exception:
        return None


def _session_kind_of(session_id: str | None) -> str | None:
    try:
        from llm_router import session_kind

        return session_kind.kind_of(session_id)
    except Exception:  # noqa: BLE001 - a tag must never cost the ledger row
        return None


#: Who produced the row. ``zero_claude``: the UserPromptSubmit hook applied the edit and the
#: whole turn was served locally, so O3 counts it as ONE local turn per (session_id, turn_id).
#: ``llm_edit``: the MCP tool answered inside a Claude turn, so it is never an O3 turn.
#: A row with no source (written before M0.3) is treated like ``llm_edit``: never a turn.
SOURCES = frozenset({"zero_claude", "llm_edit"})


def record_edit_outcome(*, file: str, model: str, applied: bool, source: str | None = None,
                        session_id: str | None = None, turn_id: str | None = None) -> float | None:
    """Append one ledger row; return its ``ts`` (None if nothing was written). Never raises.

    ``source`` is one of :data:`SOURCES` (anything else is stored as null). ``session_id`` is the
    caller's own id (the hook passes its payload's id). When omitted, an ``llm_edit`` row (written
    by the MCP server, which has no payload) is resolved through the pointer file, as before; a
    ``zero_claude`` row stores NULL: the pointer names whichever session wrote last, and a guess
    would credit the turn to the wrong session (O3 puts such rows in ``edit_no_session``).
    ``turn_id`` is ``prompt_key.key(prompt)`` of the human turn, a text-free hash, so one turn that
    edits several files can be counted once."""
    if isinstance(session_id, str) and session_id:
        sid = session_id
    elif source == "zero_claude":
        sid = None
    else:
        sid = _resolve_session_id()
    ts = time.time()
    row = {
        "ts": ts,
        "session_id": sid,
        "session_kind": _session_kind_of(sid),
        "file": file,
        "model": model,
        "applied": bool(applied),
        "survived": None,
        "source": source if source in SOURCES else None,
        "turn_id": turn_id if isinstance(turn_id, str) and turn_id else None,
    }
    try:
        path = paths.state_path(LEDGER_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", opener=paths.private_opener) as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return None
    return ts
