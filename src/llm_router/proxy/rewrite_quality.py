"""Quality signal for tier rewrites (owner D-626): what follows a rewritten Claude call, next to what
follows one that was not. Read-only over ``proxy_calls.jsonl``; numeric and enum fields only (D-281):
no prompt, answer or tool text is read, and none is printed.

A STEP is a Claude call (``decision`` forwarded or fallback, with a ``tier_reason``), ordered by ``ts``
inside its ``session_id``. It is REWRITTEN when ``served_model != requested_model``. Its NEXT step is the
session's next such call. Per model pair (requested -> served; a not-rewritten call is the pair
``m -> m``) the reader reports:

* ``stop_reason`` of the step itself (end_turn / tool_use / max_tokens / refusal / none);
* next-step tool-error rate: the next step's ``prev_tool_error`` (a tool result with ``is_error`` answered
  this step's tool call). ``prev_tool_error`` is written only since the D-626 build, so older rows are
  UNKNOWN and are left out of the rate's n, never counted as no error;
* redo rate: the next step's ``prev_tool_targets`` (name + salted hash of path/command of the tool calls
  this step made) equals this step's own ``prev_tool_targets``: the same tool call on the same target made
  twice running. A hash comparison, no text.

LIMITS, always printed: observational, not causal. A rewrite is chosen by policy (sticky, quota pressure,
research sessions), so rewritten and other steps differ in task mix and in how long the session has run;
a gap is a lead, not a measured harm. The redo rate cannot see a redo through a different tool call, and
the tool-error rate needs ``prev_tool_error`` rows."""
from __future__ import annotations

import json
from pathlib import Path

from llm_router.proxy import ledger as L

_CLAUDE = (L.DECISION_FORWARDED, L.DECISION_FALLBACK)
LIMITS = ("Observational, not causal: rewrites are chosen by policy (sticky tier, quota pressure, session kind), so "
          "the two groups differ in task mix and session age. Tool-error rate counts only rows written since "
          "prev_tool_error existed (older rows are unknown, not 'no error'); redo = the same tool call on the same "
          "target twice running, by salted hash. Rows are paired by session: parallel sub-agents that share a "
          "session_id can interleave, so a next-step pairing may cross conversations.")


def _targets_key(row: dict):
    t = row.get("prev_tool_targets")
    if not isinstance(t, list) or not t:
        return None
    try:
        return tuple(sorted((str(x.get("name")), str(x.get("h"))) for x in t if isinstance(x, dict)))
    except (AttributeError, TypeError):
        return None


def collect(rows) -> dict:
    """``{pair: counters}`` from an iterable of ledger rows (any order)."""
    by_session: dict[str, list[tuple]] = {}
    for r in rows:
        if r.get("event") or r.get("decision") not in _CLAUDE or not r.get("tier_reason"):
            continue
        sid, ts = r.get("session_id"), r.get("ts")
        if not isinstance(sid, str) or not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        req = r.get("requested_model") or "unknown"
        srv = r.get("served_model") or req
        err = r.get("prev_tool_error")
        by_session.setdefault(sid, []).append(
            (ts, req, srv, r.get("stop_reason") or "none", err if isinstance(err, bool) else None, _targets_key(r)))
    out: dict[str, dict] = {}
    for steps in by_session.values():
        steps.sort(key=lambda s: s[0])
        for i, (_ts, req, srv, stop, _e, tk) in enumerate(steps):
            c = out.setdefault(f"{req}>{srv}", {"rewritten": req != srv, "n": 0, "stops": {}, "next_n": 0,
                                                "err_known": 0, "err_n": 0, "redo_known": 0, "redo_n": 0})
            c["n"] += 1
            c["stops"][stop] = c["stops"].get(stop, 0) + 1
            if i + 1 >= len(steps):
                continue
            c["next_n"] += 1
            _t, _r, _s, _st, nerr, ntk = steps[i + 1]
            if nerr is not None:
                c["err_known"] += 1
                c["err_n"] += 1 if nerr else 0
            if ntk is not None and tk is not None:
                c["redo_known"] += 1
                c["redo_n"] += 1 if ntk == tk else 0
    return out


def read_window(ledger_path, bounds) -> tuple[dict, int]:
    """``(collect(...), rows_in_window)`` over the ledger rows whose ``ts`` ``bounds`` covers; one line at
    a time (the ledger is 100+ MB)."""
    rows: list[dict] = []
    n = 0
    try:
        fh = Path(ledger_path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {}, 0
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts") if isinstance(r, dict) else None
            if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not bounds.covers(ts):
                continue
            n += 1
            # keep only the numeric/enum fields the reader needs: nothing else of the row is held
            rows.append({k: r.get(k) for k in ("event", "decision", "tier_reason", "session_id", "ts",
                                               "requested_model", "served_model", "stop_reason",
                                               "prev_tool_error", "prev_tool_targets")})
    return collect(rows), n


def _pct(k: int, n: int) -> str:
    return f"{100.0 * k / n:.1f}% ({k}/{n})" if n else "n/a (n=0)"


def render(stats: dict, *, window: str, source: str, rows_in_window: int) -> str:
    steps = sum(c["n"] for c in stats.values())
    out = [f"Tier rewrite quality: {window}, source {source}, {rows_in_window:,} ledger rows, {steps:,} Claude steps",
           f"LIMITS: {LIMITS}"]
    for label, want in (("REWRITTEN", True), ("NOT REWRITTEN", False)):
        out.append(f"== {label} ==")
        for pair, c in sorted(((p, c) for p, c in stats.items() if c["rewritten"] is want),
                              key=lambda kv: -kv[1]["n"]):
            f, t = pair.split(">")
            stops = ", ".join(f"{k} {v}" for k, v in sorted(c["stops"].items(), key=lambda kv: -kv[1]))
            out.append(f"  {f} -> {t}: n={c['n']:,}; stop_reason: {stops}")
            out.append(f"      next-step tool error {_pct(c['err_n'], c['err_known'])} of {c['next_n']:,} next steps "
                       f"(known only for rows with prev_tool_error); redo {_pct(c['redo_n'], c['redo_known'])}")
    return "\n".join(out)
