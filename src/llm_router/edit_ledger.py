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
     "model": "ollama/qwen3.5:latest", "applied": true, "survived": null}

Field notes:

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

Fail-silent, matching every other best-effort telemetry writer in this
codebase (``tools/text.py``'s ``_cache_result``, ``_record_quality``) — a
broken ledger write must never break the ``llm_edit`` call the user is
waiting on.
"""

from __future__ import annotations

import json
import os
import time

from llm_router import paths

LEDGER_FILENAME = "edit_outcomes.jsonl"


def record_edit_outcome(*, file: str, model: str, applied: bool) -> None:
    """Append one ledger row. Best-effort: never raises."""
    row = {
        "ts": time.time(),
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
        "file": file,
        "model": model,
        "applied": bool(applied),
        "survived": None,
    }
    try:
        path = paths.state_path(LEDGER_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", opener=paths.private_opener) as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
