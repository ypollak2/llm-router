"""``skips.jsonl``: why a call or a session did NOT pass a routing decision point.

One row per skip, ``{"ts", "session_id", "door", "reason"}``. A skip with no
reason is refused, so "every skip has a reason" (PLAN-v16 P2.2-a) holds by
construction rather than by a later audit. ``scripts/coverage_check.py`` is the
reader. The writer never raises: a lost row must not cost a call.

``REASONS`` and ``DOORS`` are the closed sets the first sites use; ``record``
refuses only an empty or non-string value, so a new site adds its name here and
in the reader, not by editing the writer. The coverage check reports a row whose
reason or door is outside the sets.
"""

from __future__ import annotations

import json
import time

from llm_router import capped_log, failopen, paths

FILE_NAME = "skips.jsonl"
#: Two generations of this size are kept. A default install writes one row per
#: proxy call (``routing_off``), so the cap is sized for weeks, not days.
MAX_BYTES = 8 * 1024 * 1024

REASONS = frozenset({"headless", "base_url_override", "tiers_off", "routing_off"})
DOORS = frozenset({"agent-route", "session-start", "proxy"})

_FAILOPEN_CODE = "CHZ-FO-SKIPS-REASON"


def skips_path():
    return paths.state_path(FILE_NAME)


def record(door: str, reason: str, session_id: str | None = None) -> bool:
    """Append one skip row. True when written. Never raises."""
    try:
        if not (isinstance(door, str) and door.strip() and isinstance(reason, str) and reason.strip()):
            failopen.record(_FAILOPEN_CODE, detail="a skip needs a door and a reason; nothing written")
            return False
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id if isinstance(session_id, str) and session_id else None,
            "door": door,
            "reason": reason,
        }
        capped_log.append(skips_path(), (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"),
                          MAX_BYTES)
        return True
    except Exception as exc:  # noqa: BLE001 - fail-open: a skip row never costs a call
        failopen.record("CHZ-FO-SKIPS-WRITE", exc)
        return False
