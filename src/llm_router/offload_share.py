"""O3 "offload share": the part of the work served by a local model or by Claude
Haiku and NOT redone. Pure functions over rows; ``commands/kpi.py`` feeds them and
renders the result. The definition is in ``docs/repo_goals/KPIS.md`` (O3).

    O3 = (units served by a local model or by Haiku, and not redone) / (all units)

Units (organic sessions only, ``[now - days, now]``):

* proxy-served Claude calls: ``proxy_calls.jsonl`` rows with ``decision`` forwarded or
  fallback and a non-error upstream status; class ``haiku`` when the serving model
  (``served_model``, else ``requested_model``) names Haiku, else ``claude``.
  Claude Code's own side calls (``tier_reason == "side_call"``: titles, summaries) are
  NOT work the router could offload or the user could redo, so they are excluded and
  counted;
* proxy rows with ``decision == "served"``: answered by a local backend, class ``local``;
* local units from ``northstar.local_shadow_units()`` (usage.db, ``final_provider='ollama'``),
  class ``local``.

REDONE (one definition, shared with the Haiku redo guard):

A unit is redone when ANY of:

1. escalation: a later proxy row of the same conversation (session), in the unit's own
   human turn or the next 2 human turns, has ``tier_reason`` ``escalation`` or
   ``escalation_under_pressure``. That is the proxy's own detector: a ``claude:`` /
   ``native:`` / ``opus:`` re-ask, a contradiction, or failed tools
   (``proxy/escalation.py``). A human turn starts at a non-side-call row whose
   ``step_class`` is not ``continuation`` (a tool-result follow-up);
2. receipt band: ``user_signals.jsonl`` holds a ``redone`` press (last press per key wins)
   for the unit's ``msg_id``;
3. usage_outcome: ``usage_outcome`` has a ``redone`` verdict for a routed event of the same
   session whose timestamp is within ``OUTCOME_JOIN_S`` of the unit (local units only: a
   verdict exists only for routed MCP events), each verdict used at most once.

A unit whose 2 following human turns have not happened yet and that shows no redo is
counted as NOT redone and also counted in ``window_open``: it can still turn into a redo,
so the number is stated with that count, never silently final.
"""
from __future__ import annotations

import bisect
from typing import Any, Iterable

CLASS_HAIKU = "haiku"
CLASS_LOCAL = "local"
CLASS_CLAUDE = "claude"
ESCALATION_REASONS = frozenset({"escalation", "escalation_under_pressure"})
REDO_TURNS = 2          # the unit's own turn + the next 2 human turns
OUTCOME_JOIN_S = 120.0  # usage_outcome event ts vs local unit ts
TARGET = 0.60


def _num(x: Any) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) < 1e11:
        return float(x)
    return None


def _is_side_call(row: dict) -> bool:
    return row.get("tier_reason") == "side_call"


def _proxy_class(row: dict) -> str | None:
    """Class of a proxy row that counts as a unit, else None (not a unit)."""
    if _is_side_call(row):
        return None
    decision = row.get("decision")
    if decision == "served":
        return CLASS_LOCAL
    if decision not in ("forwarded", "fallback"):
        return None
    status = row.get("upstream_status")
    if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
        return None  # the call failed: no work was served
    model = row.get("served_model") or row.get("requested_model") or ""
    return CLASS_HAIKU if isinstance(model, str) and "haiku" in model.lower() else CLASS_CLAUDE


class _Conversation:
    """One session's proxy rows in time order, with human-turn numbers."""

    def __init__(self, rows: list[dict]) -> None:
        ordered = sorted((r for r in rows if _num(r.get("ts")) is not None and not _is_side_call(r)),
                         key=lambda r: r["ts"])
        self.ts = [float(r["ts"]) for r in ordered]
        self.turn: list[int] = []
        t = 0
        for r in ordered:
            if r.get("step_class") != "continuation":
                t += 1
            self.turn.append(t)
        self.escalations = [(self.ts[i], self.turn[i]) for i, r in enumerate(ordered)
                            if r.get("tier_reason") in ESCALATION_REASONS]
        self.last_turn = t

    def redone_after(self, ts: float) -> tuple[bool, bool]:
        """(escalation in the unit's turn or the next REDO_TURNS, window still open)."""
        i = bisect.bisect_right(self.ts, ts)
        unit_turn = self.turn[i - 1] if i > 0 else 0
        hit = any(et > ts and tn <= unit_turn + REDO_TURNS for et, tn in self.escalations)
        return hit, self.last_turn - unit_turn < REDO_TURNS


def policy_start(proxy_rows: Iterable[dict], version: str) -> float | None:
    """ts of the first proxy row stamped ``tier_policy_version == version``."""
    times = [t for r in proxy_rows if r.get("tier_policy_version") == version
             and (t := _num(r.get("ts"))) is not None]
    return min(times) if times else None


def build_units(proxy_rows: list[dict], local_units: Iterable[dict], *, now: float, days: float,
                kind_of, allowed: frozenset, band_redone: set[str] | frozenset = frozenset(),
                outcome_redos: Iterable[dict] = ()) -> dict:
    """Classified, redo-judged units in the window.

    ``kind_of(session_id, stamp)`` returns the resolved session kind or None.
    ``band_redone``: msg_ids whose last receipt-band press is ``redone``.
    ``outcome_redos``: usage_outcome rows (any outcome; only ``redone`` is read).
    Returns ``{"units": [...], "side_call_excluded", "untagged", "other_kind",
    "local_no_session"}``. A local unit with no ``session_id`` (usage.db rows written
    without one) cannot be scoped to organic sessions: it is excluded and counted in
    ``local_no_session``, so O3 is then a lower bound and the local share is unknown."""
    since = now - days * 86400.0
    by_session: dict[str, list[dict]] = {}
    for r in proxy_rows:
        sid = r.get("session_id")
        if isinstance(sid, str) and sid:
            by_session.setdefault(sid, []).append(r)
    conv = {sid: _Conversation(rows) for sid, rows in by_session.items()}

    redo_events: dict[str, list[float]] = {}
    for o in outcome_redos:
        ts, sid = _num(o.get("ts")), o.get("session_id")
        if o.get("outcome") == "redone" and ts is not None and isinstance(sid, str):
            redo_events.setdefault(sid, []).append(ts)
    for v in redo_events.values():
        v.sort()

    units: list[dict] = []
    side = untagged = other = local_no_session = 0

    def admit(sid, stamp) -> bool:
        nonlocal untagged, other
        kind = kind_of(sid, stamp)
        if kind is None:
            untagged += 1
            return False
        if kind not in allowed:
            other += 1
            return False
        return True

    for r in proxy_rows:
        ts = _num(r.get("ts"))
        if ts is None or ts < since or ts > now:
            continue
        if _is_side_call(r):
            side += 1
            continue
        cls = _proxy_class(r)
        if cls is None or not admit(r.get("session_id"), r.get("session_kind")):
            continue
        sid = r.get("session_id")
        hit, open_ = conv[sid].redone_after(ts) if sid in conv else (False, True)
        why = "escalation" if hit else None
        if why is None and isinstance(r.get("msg_id"), str) and r["msg_id"] in band_redone:
            why = "receipt_band"
        units.append({"class": cls, "ts": ts, "session_id": sid, "redone": why is not None,
                      "why": why, "window_open": open_ and why is None})

    for u in local_units:
        ts = _num(_iso_ts(u.get("ts")))
        if ts is None or ts < since or ts > now:
            continue
        sid = u.get("session_id")
        if not (isinstance(sid, str) and sid):
            local_no_session += 1  # cannot be scoped to organic: counted, never guessed
            continue
        if not admit(sid, None):
            continue
        hit, open_ = conv[sid].redone_after(ts) if sid in conv else (False, True)
        why = "escalation" if hit else None
        if why is None and _consume_event(redo_events.get(sid, []), ts):
            why = "usage_outcome"
        units.append({"class": CLASS_LOCAL, "ts": ts, "session_id": sid, "redone": why is not None,
                      "why": why, "window_open": open_ and why is None})
    return {"units": units, "side_call_excluded": side, "untagged": untagged, "other_kind": other,
            "local_no_session": local_no_session}


def _iso_ts(raw: Any) -> Any:
    if isinstance(raw, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None
    return raw


def _consume_event(events: list[float], ts: float) -> bool:
    """Use (remove) the nearest unused redone event within OUTCOME_JOIN_S of ``ts``."""
    best = None
    for i, et in enumerate(events):
        if abs(et - ts) <= OUTCOME_JOIN_S and (best is None or abs(et - ts) < abs(events[best] - ts)):
            best = i
    if best is None:
        return False
    events.pop(best)
    return True


def summarize(units: list[dict]) -> dict:
    """Counts only: n, per-class n and redone, offload-and-not-redone, window_open."""
    out = {"n": len(units), "offload_kept": 0, "window_open": 0,
           CLASS_HAIKU: {"n": 0, "redone": 0}, CLASS_LOCAL: {"n": 0, "redone": 0},
           CLASS_CLAUDE: {"n": 0, "redone": 0}, "newest_ts": None}
    for u in units:
        c = out[u["class"]]
        c["n"] += 1
        c["redone"] += 1 if u["redone"] else 0
        if u["class"] != CLASS_CLAUDE and not u["redone"]:
            out["offload_kept"] += 1
        out["window_open"] += 1 if u["window_open"] else 0
        if out["newest_ts"] is None or u["ts"] > out["newest_ts"]:
            out["newest_ts"] = u["ts"]
    return out
