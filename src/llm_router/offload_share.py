"""O3 "offload share": the part of the work served by a local model or by Claude
Haiku and NOT redone. Pure functions over rows; ``commands/kpi.py`` feeds them and
renders the result. The definition is in ``docs/repo_goals/KPIS.md`` (O3).

    O3 = (units served by a local model or by Haiku, and not redone) / (all units)

HEADLINE UNIT = HUMAN TURN. ~88% of proxy calls are tool-result continuations (about 8.5
calls per human turn), so a per-call denominator is inflated by agent loops. The headline
counts turns: a turn is offloaded when its FIRST call is served by Haiku or local and the turn
is not redone (the redo test below is applied to that first call, and covers its own turn and
the next 2). Per-call figures are kept as a secondary line.

A TURN is the first proxy call of a human turn: a proxy row that is not a side call and whose
``step_class`` is not ``continuation``, then corrected by the transcript (M0.3b, ``o3_transcripts``:
the proxy ``msg_id`` joined to the session's transcript). When the join is available it decides:

* a row is a turn only when its message is the first answer to a TYPED prompt in the main thread
  (``turn``); a later answer in the same turn (``continuation``), a sub-agent call (``sidechain``)
  and the first answer to injected input such as a slash command or a sub-agent hand-back
  (``meta``) are not turns;
* a row whose message id is in NO message of a session that has a transcript (``orphan``: a
  permission classifier, a prompt suggestion, a side query) was never part of the conversation:
  not a turn. Counted in ``unjoined``;
* a row the proxy flagged ``side_call`` (no client tools) but the transcript shows as the first
  answer to a typed prompt IS a turn: the flag is a request-shape heuristic, the transcript is
  the conversation;
* a session with no transcript at all keeps the proxy-only rule above (``no_transcript``).

Two more rules (M0.3):

* a local unit from usage.db (an MCP ``llm()`` call made INSIDE a Claude turn) is NOT a turn:
  Claude made the first call. It is reported apart (``local_assist``), outside both sides;
* a zero-Claude edit (the hook applied it and the whole turn was served locally) is ONE local
  turn per (session_id, turn_id), from the applied ``source=zero_claude`` rows of
  ``edit_outcomes.jsonl``. ``llm_edit`` rows are never turns.

Units (organic sessions only, ``[now - days, now]``):

* proxy-served Claude calls: ``proxy_calls.jsonl`` rows with ``decision`` forwarded or
  fallback and a non-error upstream status; class ``haiku`` when the serving model
  (``served_model``, else ``requested_model``) names Haiku, else ``claude``.
  Claude Code's own side calls (``tier_reason == "side_call"``: titles, summaries) are
  NOT work the router could offload or the user could redo, so they are excluded and
  counted (unless the transcript shows the call as a typed prompt's first answer, above);
* proxy rows with ``decision == "served"``: answered by a local backend, class ``local``;
* local units from ``northstar.local_shadow_units()`` (usage.db, ``final_provider='ollama'``):
  class ``local`` but NOT turns (``local_assist``, see above);
* applied ``source=zero_claude`` rows of ``edit_outcomes.jsonl``: one class ``local`` turn per
  (session_id, turn_id).

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
# transcript roles of a proxy call (o3_transcripts); duplicated names, not imported, to keep this module pure
ROLE_TURN, ROLE_META, ROLE_SIDECHAIN, ROLE_ORPHAN = "turn", "meta", "sidechain", "orphan"
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


def _proxy_class(row: dict, *, allow_side_call: bool = False) -> str | None:
    """Class of a proxy row that counts as a unit, else None (not a unit)."""
    if _is_side_call(row) and not allow_side_call:
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

    def __init__(self, rows: list[dict], begins_turn=None, counted=None) -> None:
        """``begins_turn(row)``: the row starts a human turn (default: its ``step_class`` is not
        ``continuation``). ``counted(row)``: the row is part of the conversation at all (default: it
        is not a side call). Both let the transcript join overrule the proxy-only guess."""
        begins = begins_turn or (lambda r: r.get("step_class") != "continuation")
        keep = counted or (lambda r: not _is_side_call(r))
        ordered = sorted((r for r in rows if _num(r.get("ts")) is not None and keep(r)),
                         key=lambda r: r["ts"])
        self.ts = [float(r["ts"]) for r in ordered]
        self.turn: list[int] = []
        t = 0
        for r in ordered:
            # A sub-agent's (or an injected prompt's) first call is not a human turn: it must not end
            # the unit's redo window.
            if begins(r):
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
                outcome_redos: Iterable[dict] = (), edit_rows: Iterable[dict] = (),
                thread_of=None) -> dict:
    """Classified, redo-judged units in the window.

    ``kind_of(session_id, stamp)`` returns the resolved session kind or None.
    ``band_redone``: msg_ids whose last receipt-band press is ``redone``.
    ``outcome_redos``: usage_outcome rows (any outcome; only ``redone`` is read).
    ``edit_rows``: ``edit_outcomes.jsonl`` rows; only applied ``source=zero_claude`` rows with a
    session id and a turn id make a unit (one local turn per (session_id, turn_id)).
    ``thread_of(session_id, msg_id)``: the transcript role of the call (``o3_transcripts``: ``turn``,
    ``continuation``, ``meta``, ``sidechain``, ``orphan``), or None when the session has no
    transcript. None for every row (the default) skips the join: the proxy-only rule decides.
    Returns ``{"units": [...], "local_assist": [...], "side_call_excluded", "untagged",
    "other_kind", "local_no_session", "n_escalations", "subagent_first", "meta_first", "unjoined",
    "no_transcript", "edit_no_session", "edit_no_turn_id", "zero_claude_turns"}``. ``units`` holds
    turns and the other calls (``first`` False); ``local_assist`` holds the local MCP units (never
    turns). ``subagent_first``, ``meta_first`` and ``unjoined`` count the rows the proxy-only rule
    would have counted as turns and the transcript says are not; ``no_transcript`` counts the turn
    rows kept on the proxy-only rule because their session has no transcript. ``n_escalations``
    counts escalation rows among the admitted units: how much redo signal exists at all. A local
    unit with no ``session_id`` (usage.db rows written without one) cannot be scoped to organic
    sessions: it is excluded and counted in ``local_no_session``, so the local figures are then a
    lower bound."""
    since = now - days * 86400.0
    by_session: dict[str, list[dict]] = {}
    for r in proxy_rows:
        sid = r.get("session_id")
        if isinstance(sid, str) and sid:
            by_session.setdefault(sid, []).append(r)
    role_cache: dict[int, str | None] = {}

    def role_of(r: dict) -> str | None:
        """Transcript role of a proxy row, None when there is no join (no ``thread_of``, no ids, or
        the session has no transcript). Memoised per row."""
        if thread_of is None:
            return None
        k = id(r)
        if k not in role_cache:
            mid = r.get("msg_id")
            role_cache[k] = thread_of(r.get("session_id"), mid) if isinstance(mid, str) and mid else None
        return role_cache[k]

    def begins_turn(r: dict) -> bool:
        role = role_of(r)
        return (role == ROLE_TURN) if role is not None else r.get("step_class") != "continuation"

    def counted(r: dict) -> bool:
        return not _is_side_call(r) or role_of(r) == ROLE_TURN

    conv = {sid: _Conversation(rows, begins_turn, counted) for sid, rows in by_session.items()}

    redo_events: dict[str, list[float]] = {}
    for o in outcome_redos:
        ts, sid = _num(o.get("ts")), o.get("session_id")
        if o.get("outcome") == "redone" and ts is not None and isinstance(sid, str):
            redo_events.setdefault(sid, []).append(ts)
    for v in redo_events.values():
        v.sort()

    units: list[dict] = []
    assist: list[dict] = []
    side = untagged = other = local_no_session = n_escalations = 0
    subagent_first = meta_first = unjoined = no_transcript = edit_no_session = edit_no_turn_id = 0

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
        role = role_of(r)
        if _is_side_call(r) and role != ROLE_TURN:
            side += 1
            continue
        cls = _proxy_class(r, allow_side_call=True)
        if cls is None or not admit(r.get("session_id"), r.get("session_kind")):
            continue
        sid = r.get("session_id")
        hit, open_ = conv[sid].redone_after(ts) if sid in conv else (False, True)
        why = "escalation" if hit else None
        if why is None and isinstance(r.get("msg_id"), str) and r["msg_id"] in band_redone:
            why = "receipt_band"
        first = begins_turn(r)
        if thread_of is not None and r.get("step_class") != "continuation" and not first:
            # the proxy-only rule would have counted this row as a turn; the transcript says no
            if role == ROLE_SIDECHAIN:
                subagent_first += 1
            elif role == ROLE_META:
                meta_first += 1
            elif role == ROLE_ORPHAN:
                unjoined += 1
        elif thread_of is not None and first and role is None:
            no_transcript += 1  # stays in on the proxy-only rule: counted, and said so
        units.append({"class": cls, "ts": ts, "session_id": sid, "redone": why is not None,
                      "why": why, "window_open": open_ and why is None, "first": first,
                      "msg_id": r.get("msg_id")})
        if r.get("tier_reason") in ESCALATION_REASONS:
            n_escalations += 1

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
        assist.append({"class": CLASS_LOCAL, "ts": ts, "session_id": sid, "redone": why is not None,
                       "why": why, "window_open": open_ and why is None, "first": True})

    # Zero-Claude edits: one local TURN per (session_id, turn_id). The hook served the whole turn,
    # so there is no proxy row for it; a `claude:` re-ask in the next turns marks it redone.
    seen_turns: set[tuple[str, str]] = set()
    for e in sorted((e for e in edit_rows if isinstance(e, dict)), key=lambda e: _num(e.get("ts")) or 0.0):
        if e.get("source") != "zero_claude" or e.get("applied") is not True:
            continue  # llm_edit rows (and rows with no source) happen inside Claude turns
        ts = _num(e.get("ts"))
        if ts is None or ts < since or ts > now:
            continue
        sid, tid = e.get("session_id"), e.get("turn_id")
        if not (isinstance(sid, str) and sid):
            edit_no_session += 1
            continue
        if not (isinstance(tid, str) and tid):
            edit_no_turn_id += 1
            continue
        if (sid, tid) in seen_turns or not admit(sid, e.get("session_kind")):
            continue
        seen_turns.add((sid, tid))
        hit, open_ = conv[sid].redone_after(ts) if sid in conv else (False, True)
        why = "escalation" if hit else None
        units.append({"class": CLASS_LOCAL, "ts": ts, "session_id": sid, "redone": why is not None,
                      "why": why, "window_open": open_ and why is None, "first": True,
                      "zero_claude": True})
    return {"units": units, "local_assist": assist, "side_call_excluded": side, "untagged": untagged,
            "other_kind": other, "local_no_session": local_no_session, "n_escalations": n_escalations,
            "subagent_first": subagent_first, "meta_first": meta_first, "unjoined": unjoined,
            "no_transcript": no_transcript, "edit_no_session": edit_no_session,
            "edit_no_turn_id": edit_no_turn_id, "zero_claude_turns": len(seen_turns)}


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


def turn_units(units: list[dict]) -> list[dict]:
    """The headline population: first call of each human turn (plus local MCP units)."""
    return [u for u in units if u.get("first", True)]


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
