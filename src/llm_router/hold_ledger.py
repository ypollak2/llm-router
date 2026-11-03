"""A text-free ledger of hold decisions: "BLOCKED per 100 prompts" and the probes (BIND W2).

The success bar of BIND W2 is "BLOCKED per 100 prompts under 1 and no 'Reply OK' probes",
measured on ORGANIC sessions (D-358). The audit measured it from transcripts (a
``Routing directive BLOCKED`` tool result per counted human prompt); this ledger lets the
hooks count both sides themselves, so the number is available without reading a transcript
and without a model.

ONE ROW PER EVENT, appended to ``hold_events.jsonl`` under ``LLM_ROUTER_HOME`` (two
generations kept, ``capped_log``). Never any prompt, command or tool-argument text:
ids, counts, codes and yes/no verdicts only. The ``enforce-route`` events also carry
``tool_use_id`` (the id of the tool call the host sent, when it sent one), so a row joins
to the ``tool_use`` block of the transcript exactly.

====================  =======  ============================================================
event                 writer   fields (besides ``ts``, ``event``, ``session_id``)
====================  =======  ============================================================
``prompt``            auto-    ``words``: the prompt's word count. One row per human
                      route    prompt the hook sees; the denominator. Background-task
                               notifications and sub-agent reports are not prompts; slash
                               commands and ``!`` shell entries are counted.
``hold``              auto-    ``task``, ``complexity``, ``mode``, ``words``: a hold
                      route    (pending route) was written for the prompt.
``exempt``            auto-    ``reason``, ``words``, ``task``: the prompt would have held
                      route    and did not (``hold_policy.hold_exempt_reason``).
``blocked``           enforce- ``tool``, ``task``, ``complexity``, ``violation``: a tool
                      route    call was refused (the ``Routing directive BLOCKED`` text).
``probe``             enforce- ``tool``, ``kind``, ``call_words``: an ``llm_*`` call made
                      route    while a hold was in force that did NOT release it. ``kind``
                               is ``door_unrelated`` (a routing door whose input does not
                               carry the owner's prompt) or ``non_door`` (a status, admin
                               or session tool). ``call_words`` is the length of the
                               request text, so a one-line ack is told from a real call.
``door_call``         enforce- ``tool``, ``call_words``, ``held`` (a pending route existed),
                      route    ``forwarded`` (``route``: the call carries the owner's
                               prompt; ``unrelated``: it does not; ``unknown``: no hold, so
                               no prompt to compare), ``ack`` (the call is a fixed-reply
                               probe, ``hold_policy.is_ack_probe``). One row for EVERY
                               routing-door call the hook sees, held or not: the probes a
                               ROUTE hint provokes outside any hold are here, and only here.
                               Recorded in soft, suggest, smart, hard and strict; the hook
                               exits earlier in off, shadow and advise.
``released``          enforce- ``how``: ``route`` (a door call forwarded the owner's
                      route    prompt), ``unverifiable`` (no owner prompt was recorded, or
                               the policy module was unavailable), ``expected_tool`` /
                               ``expected_server`` (a non-LLM tool the directive named),
                               ``trap`` / ``loop`` / ``count`` (the free auto-pivots, no
                               model call).
====================  =======  ============================================================

READING IT. :func:`summarize` returns the counts and the rates; ``python -m
llm_router.hold_ledger --days 30 --organic`` prints them as JSON.

* ``blocked_per_100_prompts`` is ``None``, never 0, when no prompt row exists in the window:
  an empty ledger is unknown.
* ORGANIC. D-358 is measured on organic sessions. A row carries only ``session_id``, so the
  kind of each session is read at summary time from :mod:`llm_router.session_kind` (the
  owner's override file first, then the session's tag): ``kinds=("organic",)`` or
  ``--organic`` keeps those sessions, a session never tagged is its own bucket
  (``untagged``) and is NOT counted as organic. ``by_kind`` is always present and gives each
  kind's prompts, BLOCKED and rate, so the organic line is visible even without the filter.
* THE PROBE BAR. ``ack_probes`` counts ``door_call`` rows that are fixed-reply probes and do
  not carry the owner's prompt, held or not: that is "0 'Reply OK' probes".
  ``probes``/``probes_short`` count only calls made while a hold was in force (a diagnostic,
  not the bar), because the hook cannot see a hold that does not exist. The shape test is a
  heuristic (``hold_policy.is_ack_probe``): a fixed reply of at most two words in 12 words or
  fewer; it can miss a probe phrased another way, and the transcript audit stays the check.

The ledger has no off switch, because a metric that can be turned off is a metric that
can be missing. Writes are fail-open and counted (``CHZ-FO-HOLD-LEDGER-WRITE`` in the
fail-open store), never printed: a hook's stdout is parsed as JSON by the host.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Iterable

from llm_router import capped_log
from llm_router.paths import state_path

__all__ = ["EVENTS", "STORE_FILENAME", "read_rows", "record", "store_path", "summarize"]

STORE_FILENAME = "hold_events.jsonl"
EVENTS = ("prompt", "hold", "exempt", "blocked", "probe", "door_call", "released")

#: 100-231 bytes a row (docs/RETENTION.md); 2 MiB a generation holds 9k-20k rows, weeks of a busy machine.
_MAX_BYTES = 2 * 1024 * 1024
#: Row fields a writer may pass. Anything else is dropped, so a caller cannot put text in by accident.
_FIELDS = frozenset({
    "session_id", "words", "task", "complexity", "mode", "reason", "tool", "violation",
    "kind", "call_words", "how", "tool_use_id", "held", "forwarded", "ack",
})
#: A probe whose request is this many words or fewer is "ack-shaped" for ``probes_short``.
_SHORT_PROBE_WORDS = 12
UNTAGGED = "untagged"


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


def _kind_resolver():
    """session id -> kind (``organic``, ``research``, ``harness``, ``headless``) or ``untagged``.

    The owner's override file wins over the session's tag (``session_kind.kind_of``). A
    session that was never tagged, or a reader that cannot be imported, is ``untagged``:
    unknown is never organic."""
    try:
        from llm_router import session_kind

        reader = session_kind.kind_of
    except Exception:  # noqa: BLE001 -- no reader: every session is untagged
        reader = None
    cache: dict[Any, str] = {}

    def kind(session_id: Any) -> str:
        if session_id not in cache:
            try:
                cache[session_id] = (reader(session_id) if reader and isinstance(session_id, str) else None) or UNTAGGED
            except Exception:  # noqa: BLE001 -- an unreadable tag is untagged
                cache[session_id] = UNTAGGED
        return cache[session_id]

    return kind


def _blank() -> dict:
    return {"n": {e: 0 for e in EVENTS}, "exempt_by": {}, "released_by": {}, "probe_by": {},
            "probe_short": 0, "ack_probes": 0, "sessions": set()}


def _add(acc: dict, row: dict) -> None:
    ev = row["event"]
    acc["n"][ev] += 1
    if isinstance(row.get("session_id"), str):
        acc["sessions"].add(row["session_id"])
    if ev == "exempt":
        acc["exempt_by"][str(row.get("reason"))] = acc["exempt_by"].get(str(row.get("reason")), 0) + 1
    elif ev == "released":
        acc["released_by"][str(row.get("how"))] = acc["released_by"].get(str(row.get("how")), 0) + 1
    elif ev == "probe":
        acc["probe_by"][str(row.get("kind"))] = acc["probe_by"].get(str(row.get("kind")), 0) + 1
        # ack-shaped: a routing-door call of 12 words or fewer. A status view under a hold is a
        # probe too, but it is not an ack and has no request text.
        if (row.get("kind") == "door_unrelated" and isinstance(row.get("call_words"), (int, float))
                and row["call_words"] <= _SHORT_PROBE_WORDS):
            acc["probe_short"] += 1
    elif ev == "door_call" and row.get("ack") is True and row.get("forwarded") != "route":
        acc["ack_probes"] += 1


def _headline(acc: dict) -> dict:
    prompts = acc["n"]["prompt"]
    return {
        "sessions": len(acc["sessions"]),
        "prompts": prompts,
        "blocked": acc["n"]["blocked"],
        "blocked_per_100_prompts": _per_100(acc["n"]["blocked"], prompts),
        "ack_probes": acc["ack_probes"],
    }


def summarize(days: float | None = 30, now: float | None = None,
              kinds: Iterable[str] | None = None) -> dict:
    """Counts and rates over the last *days* days (all rows when None).

    *kinds* keeps only the sessions of those kinds (``("organic",)`` for D-358); the
    top-level numbers are then over those sessions alone. ``by_kind`` always covers every
    kind, so the organic rate is visible whether or not the filter is used."""
    rows = read_rows(days, now)
    kind_of = _kind_resolver()
    want = None if kinds is None else frozenset(kinds)
    total = _blank()
    per_kind: dict[str, dict] = {}
    n_rows = 0
    for r in rows:
        if r.get("event") not in EVENTS:
            continue
        k = kind_of(r.get("session_id"))
        _add(per_kind.setdefault(k, _blank()), r)
        if want is None or k in want:
            _add(total, r)
            n_rows += 1
    n = total["n"]
    prompts = n["prompt"]
    return {
        "days": days,
        "kinds": sorted(want) if want is not None else None,
        "rows": n_rows,
        "sessions": len(total["sessions"]),
        "prompts": prompts,
        "holds": n["hold"],
        "exempt": n["exempt"],
        "exempt_by_reason": total["exempt_by"],
        "blocked": n["blocked"],
        "probes": n["probe"],
        "probes_by_kind": total["probe_by"],
        "probes_short": total["probe_short"],
        "door_calls": n["door_call"],
        "ack_probes": total["ack_probes"],
        "released": n["released"],
        "released_by_how": total["released_by"],
        "holds_per_100_prompts": _per_100(n["hold"], prompts),
        "blocked_per_100_prompts": _per_100(n["blocked"], prompts),
        "probes_per_100_prompts": _per_100(n["probe"], prompts),
        "by_kind": {k: _headline(v) for k, v in sorted(per_kind.items())},
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m llm_router.hold_ledger",
        description="Hold counters: BLOCKED and probes per 100 prompts (text-free).",
    )
    parser.add_argument("--days", type=float, default=30.0, help="window in days (0 = all rows)")
    parser.add_argument("--organic", action="store_true",
                        help="count organic sessions only (the D-358 population)")
    parser.add_argument("--kinds", default="",
                        help="comma-separated session kinds to count (organic, research, harness, headless, untagged)")
    args = parser.parse_args(argv)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if args.organic:
        kinds.append("organic")
    summary = summarize(None if args.days <= 0 else args.days, kinds=kinds or None)
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
