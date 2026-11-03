"""A text-free ledger of hold decisions: "BLOCKED per 100 prompts" and the probes (BIND W2).

The success bar of BIND W2 is "BLOCKED per 100 prompts under 1 and no 'Reply OK' probes".
The audit measured it from transcripts (a ``Routing directive BLOCKED`` tool result per
counted human prompt); this ledger lets the hooks count both sides themselves, so the
number is available without reading a transcript and without a model.

ONE ROW PER EVENT, appended to ``hold_events.jsonl`` under ``LLM_ROUTER_HOME`` (two
generations kept, ``capped_log``). Never any prompt, command or tool-argument text:
ids, counts and codes only. The three ``enforce-route`` events also carry ``tool_use_id``
(the id of the tool call the host sent, when it sent one), so a row joins to the
``tool_use`` block of the transcript exactly.

====================  =======  ============================================================
event                 writer   fields (besides ``ts``, ``event``, ``session_id``)
====================  =======  ============================================================
``prompt``            auto-    ``words``: the prompt's word count. One row per human
                      route    prompt the hook sees; the denominator. Background-task
                               notifications and sub-agent reports are not prompts.
``hold``              auto-    ``task``, ``complexity``, ``mode``, ``words``: a hold
                      route    (pending route) was written for the prompt.
``exempt``            auto-    ``reason``, ``words``, ``task``: the prompt would have held
                      route    and did not (``hold_policy.hold_exempt_reason``).
``blocked``           enforce- ``tool``, ``task``, ``complexity``, ``violation``: a tool
                      route    call was refused (the ``Routing directive BLOCKED`` text).
``probe``             enforce- ``tool``, ``kind``, ``call_words``: an ``llm_*`` call made
                      route    while a hold was pending that did NOT release it. ``kind``
                               is ``door_unrelated`` (a routing door whose input does not
                               carry the owner's prompt) or ``non_door`` (a status, admin
                               or session tool). ``call_words`` is the length of the
                               request text, so a one-line ack is told from a real call.
``released``          enforce- ``how``: ``route`` (a door call forwarded the owner's
                      route    prompt), ``unverifiable`` (the owner's prompt had nothing
                               to compare, or the policy module was unavailable),
                               ``expected_tool`` / ``expected_server`` (a non-LLM tool
                               the directive named), ``trap`` / ``loop`` / ``count``
                               (the free auto-pivots, no model call).
====================  =======  ============================================================

READING IT. :func:`summarize` returns the counts and the rates (``probes_short`` is the
number of ``door_unrelated`` probes whose request was 12 words or fewer: the "Reply OK"
shape the success bar wants at zero); ``python -m
llm_router.hold_ledger --days 30`` prints them as JSON. ``blocked_per_100_prompts`` is
``None``, never 0, when no prompt row exists in the window: an empty ledger is unknown.

The ledger has no off switch, because a metric that can be turned off is a metric that
can be missing. Writes are fail-open and counted (``CHZ-FO-HOLD-LEDGER-WRITE`` in the
fail-open store), never printed: a hook's stdout is parsed as JSON by the host.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

from llm_router import capped_log
from llm_router.paths import state_path

__all__ = ["EVENTS", "STORE_FILENAME", "read_rows", "record", "store_path", "summarize"]

STORE_FILENAME = "hold_events.jsonl"
EVENTS = ("prompt", "hold", "exempt", "blocked", "probe", "released")

#: 100-205 bytes a row (docs/RETENTION.md); 2 MiB a generation holds 10k-20k rows, weeks of a busy machine.
_MAX_BYTES = 2 * 1024 * 1024
#: Row fields a writer may pass. Anything else is dropped, so a caller cannot put text in by accident.
_FIELDS = frozenset({
    "session_id", "words", "task", "complexity", "mode", "reason", "tool", "violation",
    "kind", "call_words", "how", "tool_use_id",
})


def store_path():
    return state_path(STORE_FILENAME)


def record(event: str, session_id: str | None = None, **fields: Any) -> None:
    """Append one row. Fail-open: a failed write is counted, never raised or printed."""
    try:
        if event not in EVENTS:
            raise ValueError(f"unknown hold-ledger event {event!r}")
        row: dict[str, Any] = {"ts": round(time.time(), 3), "event": event}
        if isinstance(session_id, str) and session_id:
            row["session_id"] = session_id
        for key, value in fields.items():
            if key in _FIELDS and value is not None and not isinstance(value, (dict, list)):
                row[key] = value if isinstance(value, (bool, int, float)) else str(value)[:64]
        capped_log.append(
            store_path(), (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"), _MAX_BYTES
        )
    except Exception as exc:  # noqa: BLE001 -- a metric must never break a hook
        try:
            from llm_router import failopen

            failopen.record("CHZ-FO-HOLD-LEDGER-WRITE", exc)
        except Exception:  # noqa: BLE001 -- the counter itself is unavailable; nothing else to do
            return


def read_rows(days: float | None = None, now: float | None = None) -> list[dict]:
    """Rows of both generations, oldest first; ``days`` keeps the last N days."""
    rows = capped_log.read_dicts(store_path())
    if days is None:
        return rows
    cutoff = (time.time() if now is None else now) - days * 86400.0
    return [r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] >= cutoff]


def _per_100(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 2) if den else None


def summarize(days: float | None = 30, now: float | None = None) -> dict:
    """Counts and rates over the last *days* days (all rows when None)."""
    rows = read_rows(days, now)
    n = {e: 0 for e in EVENTS}
    exempt_by: dict[str, int] = {}
    released_by: dict[str, int] = {}
    probe_by: dict[str, int] = {}
    probe_short = 0
    sessions = set()
    for r in rows:
        ev = r.get("event")
        if ev not in n:
            continue
        n[ev] += 1
        if isinstance(r.get("session_id"), str):
            sessions.add(r["session_id"])
        if ev == "exempt":
            exempt_by[str(r.get("reason"))] = exempt_by.get(str(r.get("reason")), 0) + 1
        elif ev == "released":
            released_by[str(r.get("how"))] = released_by.get(str(r.get("how")), 0) + 1
        elif ev == "probe":
            probe_by[str(r.get("kind"))] = probe_by.get(str(r.get("kind")), 0) + 1
            # ack-shaped: a routing-door call of 12 words or fewer ("Reply OK"). A status view
            # under a hold is a probe too, but it is not an ack and has no request text.
            if (r.get("kind") == "door_unrelated" and isinstance(r.get("call_words"), (int, float))
                    and r["call_words"] <= 12):
                probe_short += 1
    prompts = n["prompt"]
    return {
        "days": days,
        "rows": len(rows),
        "sessions": len(sessions),
        "prompts": prompts,
        "holds": n["hold"],
        "exempt": n["exempt"],
        "exempt_by_reason": exempt_by,
        "blocked": n["blocked"],
        "probes": n["probe"],
        "probes_by_kind": probe_by,
        "probes_short": probe_short,
        "released": n["released"],
        "released_by_how": released_by,
        "holds_per_100_prompts": _per_100(n["hold"], prompts),
        "blocked_per_100_prompts": _per_100(n["blocked"], prompts),
        "probes_per_100_prompts": _per_100(n["probe"], prompts),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m llm_router.hold_ledger",
        description="Hold counters: BLOCKED and probes per 100 prompts (text-free).",
    )
    parser.add_argument("--days", type=float, default=30.0, help="window in days (0 = all rows)")
    args = parser.parse_args(argv)
    summary = summarize(None if args.days <= 0 else args.days)
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
