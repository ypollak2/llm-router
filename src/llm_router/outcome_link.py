"""Link each ledger decision to an outcome within 24 h (PLAN v16 P1.7 task 2, MUST P1.7-b).

``python -m llm_router.outcome_link [--now EPOCH] [--dry-run]`` (the SessionEnd background
child runs ``link()``): for every decision in ``decisions.jsonl`` that has no outcome yet it
looks, in the same session and within :data:`WINDOW_S` after the decision, at three sources and
appends ``{decision_id, outcome, source, ts, linked_at}`` to ``decision_outcomes.jsonl``:

* **escalated**: a proxy row (``proxy_calls.jsonl``) with ``tier_reason == "escalation"``;
  the proxy moved the conversation up because the cheaper tier did not hold.
* **redone**: a ``usage_outcomes.jsonl`` verdict ``redone``, or a verify record ``fail``.
* **success**: a ``usage_outcomes.jsonl`` verdict ``used``, or a verify record ``pass_*``.
* **unknown**: nothing matched and the 24 h window has closed. Until then the decision stays
  pending (no row), because a later signal could still arrive.

When several signals match, the strongest evidence that the decision was too low wins:
escalated > redone > success. ``source`` names the file the winning signal came from.

Join key: ``session_id`` plus time. A decision with no ``session_id`` cannot be joined and is
``unknown`` after the window; it is never guessed. Rows hold ids, enums and times only.
A decision is linked once; the file is append-only and a re-run adds nothing for a linked id.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from llm_router import decision_ledger as DL
from llm_router import paths

OUTCOMES_FILENAME = "decision_outcomes.jsonl"
WINDOW_S = 24 * 3600
OUTCOMES = ("success", "redone", "escalated", "unknown")
_PRECEDENCE = {"escalated": 3, "redone": 2, "success": 1}


def outcomes_path() -> Path:
    return paths.state_path(OUTCOMES_FILENAME)


def _epoch(v) -> float | None:
    """Epoch seconds from a number or an ISO-8601 string; None when neither."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def signals(*, usage_rows: list[dict], proxy_rows: list[dict], unit_rows: list[dict]) -> list[dict]:
    """Normalise the three sources to ``{session_id, ts, outcome, source}`` signals."""
    out: list[dict] = []
    for r in usage_rows:
        o = {"redone": "redone", "used": "success"}.get(r.get("outcome"))
        ts = _epoch(r.get("ts"))
        if o and ts is not None and r.get("session_id"):
            out.append({"session_id": r["session_id"], "ts": ts, "outcome": o, "source": "usage_outcomes"})
    for r in proxy_rows:
        ts = _epoch(r.get("ts"))
        if r.get("tier_reason") == "escalation" and ts is not None and r.get("session_id"):
            out.append({"session_id": r["session_id"], "ts": ts, "outcome": "escalated",
                        "source": "proxy_calls"})
    # A unit row stores no unit_id (it is derived, northstar.unit_id); a verify row stores only it.
    from llm_router import northstar

    units: dict[str, dict] = {}
    for u in unit_rows:
        if "verify" in u:
            continue
        ts = _epoch(u.get("ts"))
        uid = u.get("unit_id") or northstar.unit_id(u.get("session_id"), str(u.get("kind")), ts)
        if isinstance(uid, str):
            units[uid] = u
    verdict: dict[str, str] = {}
    for r in unit_rows:  # a verify record is a row with a "verify" dict; the last one wins
        v = r.get("verify")
        if isinstance(v, dict) and isinstance(r.get("unit_id"), str):
            verdict[r["unit_id"]] = str(v.get("verify_status"))
    for uid, status in verdict.items():
        u = units.get(uid, {})
        ts = _epoch(u.get("ts") or u.get("timestamp"))
        o = "success" if status.startswith("pass") else "redone" if status == "fail" else None
        if o and ts is not None and u.get("session_id"):
            out.append({"session_id": u["session_id"], "ts": ts, "outcome": o, "source": "verify"})
    return out


def link_decisions(decisions: list[dict], sigs: list[dict], *, now: float,
                   already: set[str] = frozenset()) -> list[dict]:
    """Pure: the outcome rows to append for ``decisions`` not in ``already``."""
    by_session: dict[str, list[dict]] = {}
    for s in sigs:
        by_session.setdefault(s["session_id"], []).append(s)
    rows = []
    for d in decisions:
        did, dts = d.get("decision_id"), _epoch(d.get("ts"))
        if not did or did in already or dts is None:
            continue
        hit = [s for s in by_session.get(d.get("session_id") or "", [])
               if dts <= s["ts"] <= dts + WINDOW_S]
        if hit:
            best = max(hit, key=lambda s: (_PRECEDENCE[s["outcome"]], -s["ts"]))
            outcome, source = best["outcome"], best["source"]
        elif now >= dts + WINDOW_S:
            outcome, source = "unknown", "none"
        else:
            continue  # window still open: a later signal could arrive
        rows.append({"decision_id": did, "outcome": outcome, "source": source, "ts": dts,
                     "linked_at": round(now, 3)})
    return rows


def link(*, now: float | None = None, dry_run: bool = False, state: Path | None = None) -> dict:
    """Read the ledgers under ``state`` (default: the state dir), append new outcome rows.
    Returns ``{n_decisions, n_linked_before, n_new, by_outcome}``. Never raises on bad input."""
    now = time.time() if now is None else now
    base = state or paths.llm_router_home()
    decisions = DL.read_jsonl(base / DL.LEDGER_FILENAME)
    done = {r.get("decision_id") for r in DL.read_jsonl(base / OUTCOMES_FILENAME)}
    sigs = signals(usage_rows=DL.read_jsonl(base / "usage_outcomes.jsonl"),
                   proxy_rows=DL.read_jsonl(base / "proxy_calls.jsonl"),
                   unit_rows=DL.read_jsonl(base / "north_star_units.jsonl"))
    new = link_decisions(decisions, sigs, now=now, already=done)
    if new and not dry_run:
        DL._append_many(base / OUTCOMES_FILENAME, new)
    by: dict[str, int] = {}
    for r in new:
        by[r["outcome"]] = by.get(r["outcome"], 0) + 1
    return {"n_decisions": len(decisions), "n_linked_before": len(done), "n_new": len(new), "by_outcome": by}


def linked_share(decisions: list[dict], outcomes: list[dict], *, now: float) -> dict:
    """P1.7-b numbers over decisions whose 24 h window has closed (a younger decision cannot
    be judged late). Two shares are returned and the stricter one is the verdict basis:

    * ``share`` = decisions linked to a REAL signal (success / redone / escalated) / due;
      ``unknown`` counts as not linked. This is the P1.7-b number.
    * ``share_any_row`` = decisions with any outcome row (including ``unknown``) / due. It
      measures the linker's coverage, and is near 1.0 by construction, so it proves nothing
      about outcomes.

    ``n_due == 0`` gives ``share: None`` (not informative, never a pass)."""
    out_by = {o.get("decision_id"): o.get("outcome") for o in outcomes}
    due = [d for d in decisions if (_epoch(d.get("ts")) or now) + WINDOW_S <= now]
    real = sum(1 for d in due if out_by.get(d.get("decision_id")) in ("success", "redone", "escalated"))
    anyrow = sum(1 for d in due if d.get("decision_id") in out_by)
    return {"n_due": len(due), "n_linked": real, "n_unknown": anyrow - real,
            "share": (real / len(due)) if due else None,
            "share_any_row": (anyrow / len(due)) if due else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llm_router.outcome_link")
    ap.add_argument("--now", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(link(now=a.now, dry_run=a.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
