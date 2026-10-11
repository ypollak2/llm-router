"""O3 "offload share": the part of the work served by a local model or by Claude
Haiku and NOT redone. Pure functions over rows; ``commands/kpi.py`` feeds them and
renders the result. The definition is in ``docs/repo_goals/KPIS.md`` (O3).

    O3 = (units served by a local model or by Haiku, and not redone) / (all units)

HEADLINE UNIT = HUMAN TURN. ~88% of proxy calls are tool-result continuations (about 8.5
calls per human turn), so a per-call denominator is inflated by agent loops. The headline
counts turns: a turn is offloaded when its FIRST call is served by Haiku or local and the turn
is not redone (the redo test below is applied to that first call, and covers its own turn and
the next 2). Per-call figures are kept as a secondary line.

A TURN is the first proxy call of a human turn (PLAN section 1.2 O3; P1.9 repaired M0.3b): a proxy row that is
not a side call, whose ``step_class`` is ``turn_first`` (a null label is not a kind: pre-GE1 rows and any
unlabelled row are never a turn; ``subagent_first`` / ``subagent_turn`` are not turns unless the transcript puts
them on the main thread as a typed prompt's answer) and that is the first answer to something a person typed.
M0.3 kept two kinds of row IN, and measured the cost (PR #295, session b9f04425, W0): 548 of 813 "turns" were
calls in no message of the session's transcript (permission classifier, prompt suggestion, side query) and the
typed-prompt side counted task notifications (113 of 209). P1.9 takes them out. The transcript join
(``o3_transcripts``: the proxy ``msg_id`` joined to the transcript assistant ``message.id``) decides: a row whose
message is a sub-agent call (``sidechain``), is in no message of its session's transcript (``orphan``) or answers
injected input (``meta``: task notification, peer message, slash command) is not a turn. They are counted
(``subagent_first``, ``unjoined``, ``meta_first``). A session with no transcript is decided by the ledger: a
``harness_turn`` row, or a ``turn_origin`` that is not human text, is not a turn (``ledger_not_human``); the rest
are turns on the proxy-only rule (``no_transcript``).

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
  counted;
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
   ``step_class`` is ``turn_first``; every other row, null-step rows included, is a follow-up;
2. receipt band: ``user_signals.jsonl`` holds a ``redone`` press (last press per key wins)
   for the unit's ``msg_id``;
3. usage_outcome: ``usage_outcome`` has a ``redone`` verdict for a routed event of the same
   session whose timestamp is within ``OUTCOME_JOIN_S`` of the unit (local units only: a
   verdict exists only for routed MCP events), each verdict used at most once.

4. detector (source 4, ``redo_signal.py``, OFF while ``REDO_SOURCE4_ENABLED`` is False): a human
   prompt in the NEXT ``REDO_TURNS`` human turns (not the unit's own) that the transcript detector flags as a
   re-ask, correction or repeat. A flagged prompt is placed on the ledger's turn count by timestamp (the first
   human-turn row at or after it, within ``DETECTOR_MAP_MAX_GAP_S``); a flag with no such row is counted as
   unmapped, never guessed. While the constant is False the flags are counted (``n_detector_flags``) and do
   not change any unit. It is turned on by a PR that cites a validation run on blind labels (PLAN M0-7b).

A unit whose 2 following human turns have not happened yet and that shows no redo is
counted as NOT redone and also counted in ``window_open``: it can still turn into a redo,
so the number is stated with that count, never silently final.

LOCAL UNIT = ONE CALL. Every ``route_and_call`` writes a row, so one MCP call can write
several (``llm_edit`` retries up to 3 times; ``llm_act`` plans, then acts), all with the
same ``tool_use_id``. Rows sharing a ``tool_use_id`` are one unit, at the time of the
first; a row with no id is its own unit. Rows the router flagged ``success=0`` (its own
verdict: degraded, a failure finish reason, unusable output) are not served work: a call
with no successful row is counted in ``local_failed``, never a unit.

LOCAL ANSWER JOIN (``tool_use_id``). A usage.db local unit written by the MCP path carries
the ``tool_use`` id of the call (``call_identity``). When a ``usage_outcome`` row has that
id as its ``event_id`` (the transcript's ``tool_use`` id), the join is exact: its session
id (the transcript's own) replaces the stamped one, its verdict is the unit's
usage_outcome redo (the 120 s time join is not used for it), and its ``turns_after`` (human
turns that followed, from the transcript) closes the window once it reaches
``REDO_TURNS``. That makes a local unit judgeable in a session the proxy never saw.
A forked session (``claude --fork-session``) copies the history, ids included, so one id
can sit in several transcripts: the copy in the stamped session wins; with none, a
``redone`` copy wins; ties break on session id, never on file order. A verdict of
``unknown`` for any reason but ``window_open`` (no_result, no_pairs, not_applied_seen,
partly_applied) is never rounded to either side: the unit is ``unjudged``. An open-window
verdict whose ``so_far`` says the edit is not applied yet stays pending.

LOCAL ANSWERS ACCEPTED (``local_answers``; reporting only, never NS: "used" needs a
passing test, owner rule 2026-10-05). Over every local answer: the local turns (proxy-served,
zero-Claude) AND the local MCP answers (``local_assist``), which are not O3 turns but are
still local answers:

* redone   -- the unit is redone by the definition above;
* accepted -- not redone, and either its window is closed (``REDO_TURNS`` human turns of
  the same session followed with no re-ask, correction or redo) or the person's last
  receipt-band press for its ``msg_id`` is ``kept``. A keep still loses to a redo that
  lands later in the window: redone is decided first on every read;
* unjudged -- not redone, and its exactly joined usage_outcome verdict is an unknown that
  will not resolve (see the join above). Counted, never folded into either side;
* pending  -- neither yet. Counted, never folded into either side.

The accept rate is accepted / (accepted + redone); pending, unjudged and failed units and
units with no session id are outside it and are stated beside it.
"""
from __future__ import annotations

import bisect
from typing import Any, Iterable

CLASS_HAIKU = "haiku"
CLASS_LOCAL = "local"
CLASS_CLAUDE = "claude"
# transcript roles of a proxy call (o3_transcripts); duplicated names, not imported, to keep this module pure
ROLE_TURN, ROLE_META, ROLE_SIDECHAIN, ROLE_ORPHAN = "turn", "meta", "sidechain", "orphan"
STEP_TURN_FIRST = "turn_first"   # proxy.steps.STEP_TURN_FIRST, duplicated to keep this module pure
# TURNFIRST-1 split two kinds out of turn_first (proxy.steps, duplicated): a main-thread turn whose newest
# user message is only a notification / command echo (still a turn here: the owner's definition keeps
# injected input such as a sub-agent hand-back IN n, see ``meta_first``), and a later sub-agent call (not a
# turn unless the transcript puts it on the main thread, the same rule as ``subagent_first``).
STEP_HARNESS_TURN = "harness_turn"
STEP_SUBAGENT_TURN = "subagent_turn"
_SUBAGENT_STEPS = frozenset({"subagent_first", STEP_SUBAGENT_TURN})
STEP_UNKNOWN = "unknown"  # labelling raised (proxy.steps.STEP_UNKNOWN, step_error): not a kind, never a turn
_BEGINS_TURN_KINDS = frozenset({STEP_TURN_FIRST, STEP_HARNESS_TURN})
#: ``turn_origin`` (proxy.steps.TURN_ORIGINS, duplicated) values that are not a person's typed prompt.
NOT_HUMAN_ORIGINS = frozenset({"task_notification", "command", "other_tag", "subagent_brief"})
ESCALATION_REASONS = frozenset({"escalation", "escalation_under_pressure"})
NOT_APPLIED_YET = frozenset({"not_applied_seen", "partly_applied"})  # usage_outcome so_far
REDO_TURNS = 2          # the unit's own turn + the next 2 human turns
REDO_SOURCE4_ENABLED = False   # PLAN M0.9: flipped by a follow-up PR that cites the validation result
DETECTOR_MAP_SLACK_S = 2.0      # a prompt may be stamped this much AFTER the ledger row it caused
DETECTOR_MAP_MAX_GAP_S = 120.0  # ... and at most this much before it (hooks run between prompt and call)
OUTCOME_JOIN_S = 120.0  # usage_outcome event ts vs local unit ts
TARGET = 0.60


def _num(x: Any) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) < 1e11:
        return float(x)
    return None


def _ledger_not_human(row: dict) -> bool:
    """The proxy's own reading of a turn-first row: its newest user message is not human text."""
    return row.get("step_class") == STEP_HARNESS_TURN or row.get("turn_origin") in NOT_HUMAN_ORIGINS


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

    def __init__(self, rows: list[dict], begins_turn=None) -> None:
        """``begins_turn(row)``: the row starts a human turn (default: its ``step_class`` is
        ``turn_first`` or ``harness_turn``). Side calls are not part of the conversation."""
        begins = begins_turn or (lambda r: r.get("step_class") in _BEGINS_TURN_KINDS)
        ordered = sorted((r for r in rows if _num(r.get("ts")) is not None and not _is_side_call(r)),
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
        # (ts of the first row of each human turn); human turn k starts at starts[k - 1]
        self.starts = [self.ts[i] for i, r in enumerate(ordered) if begins(r)]

    def unit_turn(self, ts: float) -> int:
        i = bisect.bisect_right(self.ts, ts)
        return self.turn[i - 1] if i > 0 else 0

    def turn_of_prompt(self, prompt_ts: float) -> int | None:
        """Human turn number of a prompt stamped ``prompt_ts`` in a transcript: the first turn whose first
        row is at or after it (minus the slack) and within DETECTOR_MAP_MAX_GAP_S. None when there is none."""
        i = bisect.bisect_left(self.starts, prompt_ts - DETECTOR_MAP_SLACK_S)
        if i >= len(self.starts) or self.starts[i] - prompt_ts > DETECTOR_MAP_MAX_GAP_S:
            return None
        return i + 1

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
                thread_of=None, detector_flags=None,
                band_kept: set[str] | frozenset = frozenset()) -> dict:
    """Classified, redo-judged units in the window.

    ``kind_of(session_id, stamp)`` returns the resolved session kind or None.
    ``band_redone``: msg_ids whose last receipt-band press is ``redone``.
    ``band_kept``: msg_ids whose last receipt-band press is ``kept`` (sets ``kept``).
    ``outcome_redos``: usage_outcome rows (any outcome). Joined exactly by ``event_id`` to a
    local unit's ``tool_use_id``; otherwise only ``redone`` rows are read, by time.
    ``edit_rows``: ``edit_outcomes.jsonl`` rows; only applied ``source=zero_claude`` rows with a
    session id and a turn id make a unit (one local turn per (session_id, turn_id)).
    ``thread_of(session_id, msg_id)``: the transcript role of the call (``o3_transcripts``: ``turn``,
    ``continuation``, ``meta``, ``sidechain``, ``orphan``), or None when the session has no
    transcript. Only ``sidechain`` changes a turn; the rest are counted (see the module docstring).
    It is called only for turn-first rows of admitted sessions inside the window. None for every row
    (the default) skips the join: the proxy-only rule decides.
    ``detector_flags(session_id)``: ``[(prompt ts, signal)]`` from ``redo_signal`` (source 4), called once
    per admitted session. Counted always; it marks units redone only when ``REDO_SOURCE4_ENABLED``.
    Returns ``{"units": [...], "local_assist": [...], "side_call_excluded", "untagged",
    "other_kind", "local_no_session", "local_failed", "local_untagged", "local_other_kind",
    "n_escalations", "subagent_first", "meta_first", "unjoined",
    "no_transcript", "edit_no_session", "edit_no_turn_id", "zero_claude_turns", "n_detector_flags",
    "n_detector_unmapped", "no_step_class", "step_subagent_first", "step_subagent_turn",
    "step_side_call"}``. ``step_side_call`` counts rows labelled
    ``side_call`` whose ``tier_reason`` is not (so ``side_call_excluded`` missed them). ``no_step_class``,
    ``step_subagent_first`` and ``step_subagent_turn`` count the admitted proxy rows left out of the turns
    because their ``step_class`` is null (pre-GE1 rows) or ``unknown`` (labelling raised; both in
    ``no_step_class``), ``subagent_first`` or ``subagent_turn``. ``units`` holds
    turns and the other calls (``first`` False); ``local_assist`` holds the local MCP units (never
    turns). ``subagent_first``, ``meta_first`` and ``unjoined`` count the turn-first rows
    by what the transcript says, and ALL THREE are taken out of the turns (P1.9): ``subagent_first``,
    ``meta_first`` (first answer to injected input: task notification, peer message, slash command) and
    ``unjoined`` (no message of the session's transcript holds the call). ``ledger_not_human`` counts the
    turn-first rows of a session with no transcript that the ledger itself says were not typed
    (``harness_turn`` / ``turn_origin``), also taken out. ``no_transcript`` counts the turn rows kept on the
    proxy-only rule because their session has no transcript (or the row has no message id).
    ``local_untagged`` / ``local_other_kind`` are the local answers among the ``untagged`` /
    ``other_kind`` exclusions (the local answers line states them). A local answer is a local MCP
    answer, a proxy-served row (``decision == "served"``) or a zero-Claude edit turn: the same
    three sources as the line's served count, and counted per TURN like it (a proxy row only when
    it begins a turn, an edit turn once however many rows it wrote).
    ``n_escalations`` counts escalation rows among the admitted units: how much redo signal exists
    at all. A local unit with no ``session_id`` (usage.db rows written without one) cannot be
    scoped to organic sessions: it is excluded and counted in ``local_no_session``, so the local
    figures are then a lower bound."""
    since = now - days * 86400.0
    by_session: dict[str, list[dict]] = {}
    for r in proxy_rows:
        sid = r.get("session_id")
        ts = _num(r.get("ts"))
        # Only rows inside [since, now] matter: turn numbers are only compared with each other, so
        # rows before the window cannot change a redo verdict. This also bounds the transcript reads.
        if isinstance(sid, str) and sid and ts is not None and since <= ts <= now:
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
        # A human turn (P1.9): not a side call, step_class turn_first, and the first answer to something a
        # person typed. Taken out: sub-agent calls, calls the session's transcript does not hold (orphan:
        # permission classifier, prompt suggestion, side query) and first answers to injected input
        # (meta: task notification, peer message, slash command). With no transcript the ledger's own
        # reading of the newest user message decides (``harness_turn`` / ``turn_origin``).
        # A null step_class is not a kind: only rows written before GE1 (#313) carry one.
        step = r.get("step_class")
        role = role_of(r)
        if step in ("subagent_first", STEP_SUBAGENT_TURN):
            # The proxy infers this label from a missing Agent/Task launcher in the tool list, so a main
            # session run with that tool disabled gets it too. Only a transcript join that places the
            # message on the main thread as a typed prompt's answer keeps the row a turn.
            return role == ROLE_TURN
        if step not in _BEGINS_TURN_KINDS:
            return False
        if role in (ROLE_SIDECHAIN, ROLE_ORPHAN, ROLE_META):
            return False
        return role is not None or not _ledger_not_human(r)

    convs: dict[str, "_Conversation"] = {}

    def conv_of(sid: str) -> "_Conversation | None":
        """The session's conversation, built on first use: only sessions that have an admitted
        unit pay for the transcript join."""
        if sid not in by_session:
            return None
        if sid not in convs:
            convs[sid] = _Conversation(by_session[sid], begins_turn)
        return convs[sid]

    calls: dict[Any, list[dict]] = {}  # one local unit per MCP call (module doc)
    for i, u in enumerate(local_units):
        tid = u.get("tool_use_id")
        calls.setdefault(tid if isinstance(tid, str) and tid else ("row", i), []).append(u)
    outcomes = list(outcome_redos)
    copies: dict[str, list[dict]] = {}
    for o in outcomes:
        if isinstance(o.get("event_id"), str):
            copies.setdefault(o["event_id"], []).append(o)
    joined = set(calls) & set(copies)
    redo_events: dict[str, list[float]] = {}
    for o in outcomes:
        if o.get("event_id") in joined:
            continue  # judged through its exact join; never reused by the time join
        ts, sid = _num(o.get("ts")), o.get("session_id")
        if o.get("outcome") == "redone" and ts is not None and isinstance(sid, str):
            redo_events.setdefault(sid, []).append(ts)
    for v in redo_events.values():
        v.sort()

    units: list[dict] = []
    assist: list[dict] = []
    side = untagged = other = local_no_session = local_failed = n_escalations = 0
    local_untagged = local_other = 0
    subagent_first = meta_first = unjoined = no_transcript = edit_no_session = edit_no_turn_id = 0
    ledger_not_human = 0
    no_step_class = step_subagent_first = step_subagent_turn = step_side_call = 0

    det_turns: dict[str, list[int]] = {}
    n_det = n_det_unmapped = 0

    def detector_turns(sid) -> list[int]:
        """Human turn numbers of the session's flagged prompts (in the window), computed once per session."""
        nonlocal n_det, n_det_unmapped
        c = conv_of(sid) if detector_flags is not None and isinstance(sid, str) else None
        if c is None:
            return []
        if sid not in det_turns:
            try:
                flags = list(detector_flags(sid))
            except Exception:  # noqa: BLE001 -- an unreadable transcript yields no flags
                flags = []
            turns: set[int] = set()
            for ft in sorted({f[0] for f in flags if _num(f[0]) is not None and since <= f[0] <= now}):
                t = c.turn_of_prompt(ft)
                if t is None:
                    n_det_unmapped += 1
                else:
                    turns.add(t)
            n_det += len(turns)
            det_turns[sid] = sorted(turns)
        return det_turns[sid]

    def detector_redone(sid, ts) -> bool:
        turns = detector_turns(sid)      # always evaluated, so the flags are counted while disabled
        c = conv_of(sid) if isinstance(sid, str) else None
        if not REDO_SOURCE4_ENABLED or c is None:
            return False
        ut = c.unit_turn(ts)
        return any(ut < t <= ut + REDO_TURNS for t in turns)

    def admit(sid, stamp, *, local: bool = False) -> bool:
        nonlocal untagged, other, local_untagged, local_other
        kind = kind_of(sid, stamp)
        if kind is None:
            untagged += 1
            local_untagged += 1 if local else 0
            return False
        if kind not in allowed:
            other += 1
            local_other += 1 if local else 0
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
        if cls is None:
            continue
        # A local ANSWER is a turn, as in the line's served count (turn_units: first-of-turn
        # only), so a continuation or sub-agent-first row of a dropped session is not counted.
        if not admit(r.get("session_id"), r.get("session_kind"),
                     local=cls == CLASS_LOCAL and begins_turn(r)):
            continue
        sid = r.get("session_id")
        conv = conv_of(sid) if isinstance(sid, str) else None
        hit, open_ = conv.redone_after(ts) if conv else (False, True)
        why = "escalation" if hit else None
        if why is None and isinstance(r.get("msg_id"), str) and r["msg_id"] in band_redone:
            why = "receipt_band"
        first = begins_turn(r)
        step = r.get("step_class")
        if step is None or step == STEP_UNKNOWN:
            no_step_class += 1          # not a turn (first is False): counted, never guessed
        elif step == "subagent_first" and not first:
            step_subagent_first += 1    # the proxy's own label, no main-thread join: not a turn
        elif step == STEP_SUBAGENT_TURN and not first:
            step_subagent_turn += 1     # a later sub-agent call (TURNFIRST-1): not a turn
        elif step == "side_call":
            step_side_call += 1         # labelled side call whose tier_reason is not side_call
        if step in _BEGINS_TURN_KINDS and (thread_of is not None or not first):
            # a turn-first row: say what the transcript (or, with none, the ledger) makes of it
            role = role_of(r)
            if role == ROLE_SIDECHAIN:
                subagent_first += 1     # taken out of the turns (first is False)
            elif role == ROLE_META:
                meta_first += 1         # taken out (P1.9), counted
            elif role == ROLE_ORPHAN:
                unjoined += 1           # taken out (P1.9), counted
            elif role is None:
                if first:
                    no_transcript += 1  # kept in on the proxy-only rule: counted, and said so
                else:
                    ledger_not_human += 1   # taken out on the ledger's own reading, counted
        if why is None and detector_redone(sid, ts):
            why = "detector"
        units.append({"class": cls, "ts": ts, "session_id": sid, "redone": why is not None,
                      "why": why, "window_open": open_ and why is None, "first": first,
                      "msg_id": r.get("msg_id"), "step": step, "decision": r.get("decision"),
                      "role": role_of(r) if step in _BEGINS_TURN_KINDS or step in _SUBAGENT_STEPS else None,
                      "model": r.get("served_model") or r.get("requested_model"),
                      "task_type": r.get("tier_task_type"), "prev_tool_class": r.get("prev_tool_class"),
                      "kept": isinstance(r.get("msg_id"), str) and r["msg_id"] in band_kept})
        if r.get("tier_reason") in ESCALATION_REASONS:
            n_escalations += 1

    for key, rows in calls.items():
        ts = _num(_iso_ts(rows[0].get("ts")))
        if ts is None or ts < since or ts > now:
            continue
        stamp = rows[0].get("session_id")
        ev = _pick_copy(copies.get(key, ()), stamp)
        sid = stamp
        if ev is not None and isinstance(ev.get("session_id"), str) and ev["session_id"]:
            sid = ev["session_id"]  # the transcript's own session id wins over the stamp
        if not (isinstance(sid, str) and sid):
            local_no_session += 1  # cannot be scoped to organic: counted, never guessed
            continue
        if not admit(sid, None, local=True):
            continue
        if all(_failed(r) for r in rows):
            local_failed += 1  # the router's own verdict: no usable answer was served
            continue
        conv = conv_of(sid)
        hit, open_ = conv.redone_after(ts) if conv else (False, True)
        why = "escalation" if hit else None
        unjudged = False
        if ev is not None:
            outcome, reason = ev.get("outcome"), ev.get("reason")
            if why is None and outcome == "redone":
                why = "usage_outcome"
            after = ev.get("turns_after")
            if isinstance(after, int) and not isinstance(after, bool):
                # The transcript's human turns win both ways. The proxy counts every
                # non-continuation request, a Task subagent's first call included, so it can
                # close a window the transcript shows still open.
                open_ = after < REDO_TURNS
            if outcome not in ("used", "redone"):
                if reason != "window_open":
                    unjudged = True  # usage_outcome's unknown: never rounded to either side
                elif ev.get("so_far", reason) in NOT_APPLIED_YET:
                    open_ = True  # not applied so far, may still be: pending
        elif why is None and _consume_event(redo_events.get(sid, []), ts):
            why = "usage_outcome"
        if why is None and detector_redone(sid, ts):
            why = "detector"
        assist.append({"class": CLASS_LOCAL, "ts": ts, "session_id": sid, "redone": why is not None,
                       "why": why, "window_open": open_ and why is None, "first": True,
                       "kept": False, "joined": ev is not None, "unjudged": unjudged and why is None,
                       "model": rows[0].get("model"), "provider": rows[0].get("provider"),
                       "task_type": rows[0].get("task_type"), "reason_code": rows[0].get("reason_code")})

    # Zero-Claude edits: one local TURN per (session_id, turn_id). The hook served the whole turn,
    # so there is no proxy row for it; a `claude:` re-ask in the next turns marks it redone.
    seen_turns: set[tuple[str, str]] = set()
    dropped_turns: set[tuple[str, str]] = set()  # turns admit() refused: counted once, not per row
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
        if (sid, tid) in seen_turns or (sid, tid) in dropped_turns:
            continue  # one turn, however many edit rows it wrote
        if not admit(sid, e.get("session_kind"), local=True):
            dropped_turns.add((sid, tid))
            continue
        seen_turns.add((sid, tid))
        conv = conv_of(sid)
        hit, open_ = conv.redone_after(ts) if conv else (False, True)
        why = "escalation" if hit else None
        units.append({"class": CLASS_LOCAL, "ts": ts, "session_id": sid, "redone": why is not None,
                      "why": why, "window_open": open_ and why is None, "first": True,
                      "zero_claude": True})
    return {"units": units, "local_assist": assist, "side_call_excluded": side, "untagged": untagged,
            "other_kind": other, "local_no_session": local_no_session, "local_failed": local_failed,
            "local_untagged": local_untagged, "local_other_kind": local_other,
            "n_escalations": n_escalations,
            "subagent_first": subagent_first, "meta_first": meta_first, "unjoined": unjoined,
            "no_transcript": no_transcript, "ledger_not_human": ledger_not_human, "edit_no_session": edit_no_session,
            "edit_no_turn_id": edit_no_turn_id, "zero_claude_turns": len(seen_turns),
            "n_detector_flags": n_det, "n_detector_unmapped": n_det_unmapped,
            "no_step_class": no_step_class, "step_subagent_first": step_subagent_first,
            "step_subagent_turn": step_subagent_turn, "step_side_call": step_side_call}


def _failed(row: dict) -> bool:
    """The router flagged this answer failed (``success=0``); None (no column) is not."""
    ok = row.get("success")
    return ok is not None and not ok


def _pick_copy(rows: Iterable[dict], stamp: Any) -> dict | None:
    """The verdict for one tool_use id that may sit in several transcripts (a fork copies
    the history): the copy in the stamped session, else a redone copy, else the lowest
    session id. Never the order the transcripts were read in."""
    rows = list(rows)
    mine = [o for o in rows if isinstance(stamp, str) and stamp and o.get("session_id") == stamp]
    return min(mine or rows, key=lambda o: (o.get("outcome") != "redone", str(o.get("session_id") or "")),
               default=None)


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


def local_answers(units: list[dict]) -> dict:
    """Counts only: local units served, accepted, redone, unjudged, pending (module doc).
    Pass ``built["units"] + built["local_assist"]``: the proxy-served and zero-Claude local turns
    AND the local MCP answers, which O3 keeps out of its turns but which are local answers."""
    out = {"served": 0, "accepted": 0, "redone": 0, "unjudged": 0, "pending": 0, "kept": 0, "joined": 0}
    for u in turn_units(units):
        if u["class"] != CLASS_LOCAL:
            continue
        out["served"] += 1
        out["kept"] += 1 if u.get("kept") else 0
        out["joined"] += 1 if u.get("joined") else 0
        if u["redone"]:
            out["redone"] += 1
        elif u.get("unjudged"):
            out["unjudged"] += 1
        elif not u["window_open"] or u.get("kept"):
            out["accepted"] += 1
        else:
            out["pending"] += 1
    return out


# ── S1 offload per ACTION (PLAN v16 section 1.1, P1.9) ────────────────────────────────────────────────
#
# An ACTION is any non-side-call model call of an organic session: a proxy turn-first, continuation or
# sub-agent call, an MCP ``llm()`` call, a DIRECT hook draft, a zero-Claude edit turn, and a technical op a
# local model condensed. Deterministic filtering (``served_by=local_filter``), raw passthrough and ops under
# 4 KB are not model actions and are in neither side. Each action gets a LANE (``lane_quality``) and a served
# class; it is in the NUMERATOR when it was served by a local / Haiku / other very-cheap model, was not redone,
# not escalated, not re-read (technical ops), and its lane is quality-sampled. Work served by another Frontier
# subscription (Codex gpt-5.5, Gemini Pro) is ``spread``: denominator only, reported on its own line (D-24).

DOOR_PROXY, DOOR_SUBAGENT, DOOR_MCP, DOOR_DIRECT, DOOR_TECHOP, DOOR_HOOK = (
    "proxy", "subagent", "mcp", "direct", "techop", "hook")
SC_LOCAL, SC_HAIKU, SC_CHEAP, SC_SPREAD, SC_PAID = "local", "haiku", "very_cheap", "spread", "paid"
OFFLOADED_CLASSES = frozenset({SC_LOCAL, SC_HAIKU, SC_CHEAP})
TG_QA, TG_CODE, TG_TECH = "qa", "code", "tech_op"
TECHOP_MIN_BYTES = 4096     # PLAN P2.1 task 3: smaller outputs are passed through, never condensed
TECHOP_FILE = "technical_ops.jsonl"
_SPREAD_RE = None


def _is_spread(model: Any, provider: Any = None) -> bool:
    """Another Frontier subscription: Codex (gpt-5.5, gpt-6) or Gemini Pro."""
    global _SPREAD_RE
    import re
    if _SPREAD_RE is None:
        _SPREAD_RE = re.compile(r"codex|gpt-5\.|gpt-6|gemini[\w.\-/]*pro")
    m = model.lower() if isinstance(model, str) else ""
    return (isinstance(provider, str) and provider.lower() == "codex") or bool(_SPREAD_RE.search(m))


def served_class(model: Any, *, local: bool = False, provider: Any = None) -> str:
    """``local`` | ``haiku`` | ``very_cheap`` | ``spread`` | ``paid`` for the model that served an action."""
    from llm_router import pricing
    m = model if isinstance(model, str) else ""
    if local or pricing.is_local(m) or (isinstance(provider, str) and provider.lower() == "ollama"):
        return SC_LOCAL
    if "haiku" in m.lower():
        return SC_HAIKU
    if _is_spread(m, provider):
        return SC_SPREAD
    return SC_CHEAP if pricing.is_very_cheap(m) else SC_PAID


def _task_group(task_type: Any, prev_tool_class: Any = None) -> str:
    if prev_tool_class == "technical_op":
        return TG_TECH
    return TG_CODE if task_type == "code" else TG_QA


def _techop_actions(techops: Iterable[dict], *, since: float, now: float, admit) -> tuple[list[dict], dict]:
    """Model-served technical ops as actions. ``techops``: ``technical_ops.jsonl`` rows (PLAN P2.1)."""
    out: list[dict] = []
    ex = {"techop_deterministic": 0, "techop_passthrough": 0, "techop_below_4kb": 0,
          "techop_no_bytes": 0, "techop_no_session": 0}
    for t in techops:
        ts = _num(_iso_ts(t.get("ts")))
        if ts is None or ts < since or ts > now:
            continue
        by = t.get("served_by")
        if by == "local_filter":
            ex["techop_deterministic"] += 1      # not a model action
            continue
        if by != "local_model":
            ex["techop_passthrough"] += 1        # raw output: no model served it
            continue
        size = t.get("bytes_in")
        if not (isinstance(size, int) and not isinstance(size, bool)):
            ex["techop_no_bytes"] += 1           # the 4 KB rule cannot be applied: never guessed
            continue
        if size < TECHOP_MIN_BYTES:
            ex["techop_below_4kb"] += 1
            continue
        sid = t.get("session_id")
        if not (isinstance(sid, str) and sid):
            ex["techop_no_session"] += 1
            continue
        if not admit(sid, t.get("session_kind")):
            continue
        out.append({"ts": ts, "session_id": sid, "door": DOOR_TECHOP, "task_group": TG_TECH,
                    "served_class": SC_LOCAL, "model": t.get("served_model") or t.get("model"),
                    "first": False, "redone": False, "reread": bool(t.get("re_read")), "window_open": False})
    return out, ex


def action_units(since: float, until: float, *, proxy_rows: list[dict] | None = None,
                 mcp_rows: Iterable[dict] | None = None, techops: Iterable[dict] | None = None,
                 edit_rows: Iterable[dict] | None = None, kind_of=None,
                 allowed: frozenset = frozenset({"organic"}), thread_of=None,
                 band_redone: set[str] | frozenset = frozenset(), outcome_redos: Iterable[dict] | None = None,
                 quality_rows: Iterable[dict] | None = None) -> dict:
    """Every organic ACTION in ``[since, until]`` with its lane and verdict (module section above).

    Inputs default to the live files; pass them to measure something else (the fixture does):
    ``proxy_rows`` (``proxy_calls.jsonl``), ``mcp_rows`` (``northstar.local_shadow_units(all_providers=True)``:
    MCP ``llm()``/``llm_edit`` and DIRECT rows of ``usage.db``, any provider), ``techops``
    (``technical_ops.jsonl``), ``edit_rows`` (``edit_outcomes.jsonl``), ``kind_of(session_id, stamp)`` (session
    kind, default ``session_kind.KindIndex``), ``outcome_redos`` (``usage_outcome`` rows), ``quality_rows``
    (``lane_quality.jsonl``).

    Returns ``{"actions": [...], "excluded": {...}, "sampled": {lane: batch}, "built": build_units result}``.
    Each action: ``ts, session_id, door, task_group, served_class, model, first, redone, reread, window_open,
    lane, sampled, numerator, spread``. ``excluded`` counts what is NOT an action and why."""
    from llm_router import lane_quality, paths
    from llm_router.proxy import ledger as pl

    now, days = until, (until - since) / 86400.0
    if proxy_rows is None:
        proxy_rows = pl.read_rows()
    if kind_of is None:
        from llm_router import session_kind
        index = session_kind.KindIndex(proxy_rows)
        kind_of = lambda sid, stamp: index.resolve(sid, stamp=stamp).kind  # noqa: E731
    if mcp_rows is None:
        from llm_router import northstar
        mcp_rows = northstar.local_shadow_units(days=max(1, int(days) + 2), all_providers=True)
    mcp_rows = [u for u in mcp_rows if u.get("reason_code") != "cache_hit"
                and not str(u.get("model") or "").startswith("cache/")]
    if techops is None:
        techops = _read_jsonl(paths.state_path(TECHOP_FILE))
    if edit_rows is None:
        from llm_router import northstar
        edit_rows = northstar._load_edit_outcomes()
    if quality_rows is None:
        quality_rows = lane_quality.read_rows()
    built = build_units(proxy_rows, mcp_rows, now=now, days=days, kind_of=kind_of, allowed=allowed,
                        band_redone=band_redone, outcome_redos=list(outcome_redos or ()),
                        edit_rows=edit_rows or (), thread_of=thread_of)
    sampled = lane_quality.sampled_lanes(quality_rows, now)

    actions: list[dict] = []
    for u in built["units"]:
        if u.get("zero_claude"):
            door, group, cls, model = DOOR_HOOK, TG_CODE, SC_LOCAL, None
        else:
            sub = u.get("step") in _SUBAGENT_STEPS or u.get("role") == ROLE_SIDECHAIN
            door = DOOR_SUBAGENT if sub else DOOR_PROXY
            group = _task_group(u.get("task_type"), u.get("prev_tool_class"))
            model = u.get("model")
            cls = served_class(model, local=u.get("decision") == "served")
        actions.append({**_action_base(u), "door": door, "task_group": group, "served_class": cls,
                        "model": model, "reread": False})
    for u in built["local_assist"]:
        direct = u.get("reason_code") == "direct"
        cls = served_class(u.get("model"), provider=u.get("provider"))
        actions.append({**_action_base(u), "door": DOOR_DIRECT if direct else DOOR_MCP,
                        "task_group": _task_group(u.get("task_type")), "served_class": cls,
                        "model": u.get("model"), "reread": False})
    admitted = lambda sid, stamp: kind_of(sid, stamp) in allowed  # noqa: E731
    tech, tech_ex = _techop_actions(techops, since=since, now=now, admit=admitted)
    actions += tech
    for a in actions:
        lane = (a["door"], a["served_class"], a["task_group"])
        a["lane"] = lane
        a["spread"] = a["served_class"] == SC_SPREAD
        a["sampled"] = lane in sampled
        a["numerator"] = (a["served_class"] in OFFLOADED_CLASSES and not a["redone"] and not a["reread"]
                          and a["sampled"])
    excluded = {"side_call": built["side_call_excluded"], "untagged": built["untagged"],
                "other_kind": built["other_kind"], "local_no_session": built["local_no_session"],
                "local_failed": built["local_failed"], **tech_ex}
    return {"actions": actions, "excluded": excluded, "sampled": sampled, "built": built}


def _action_base(u: dict) -> dict:
    return {"ts": u["ts"], "session_id": u.get("session_id"), "first": bool(u.get("first")),
            "redone": bool(u.get("redone")), "window_open": bool(u.get("window_open"))}


def _read_jsonl(path) -> list[dict]:
    import json
    from pathlib import Path
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def rate_line(k: int, n: int, sessions: Iterable[Any], window: dict | None = None) -> dict:
    """``{k, n, value, wilson95, n_sessions, largest_session_share, window}`` (PLAN v16 section 1.1).
    ``largest_session_share`` is the biggest session's share of ``n``; ``value`` and the share are None at n=0."""
    from collections import Counter
    from llm_router.shadow_eval import wilson
    per = Counter(sessions)
    return {"k": k, "n": n, "value": round(k / n, 4) if n else None, "wilson95": wilson(k, n),
            "n_sessions": len(per),
            "largest_session_share": round(max(per.values()) / n, 4) if n and per else None,
            "window": window}


def summarize_actions(actions: list[dict], window: dict | None = None) -> dict:
    """``{action, call, spread, by_lane, by_task_type}`` over :func:`action_units` actions.

    ``action`` every action (k = numerator); ``call`` the proxy-ledger subset (doors ``proxy`` and ``subagent``:
    what O3 called "per call"); ``spread`` the actions another Frontier subscription served (k) among all (n);
    ``by_lane`` / ``by_task_type`` the same split by lane / task group, each lane with its ``sampled`` flag."""
    def line(rows: list[dict], key: str = "numerator") -> dict:
        return rate_line(sum(1 for a in rows if a[key]), len(rows), (a["session_id"] for a in rows), window)

    by_lane: dict[tuple, list[dict]] = {}
    by_group: dict[str, list[dict]] = {}
    for a in actions:
        by_lane.setdefault(a["lane"], []).append(a)
        by_group.setdefault(a["task_group"], []).append(a)
    return {
        "action": line(actions),
        "call": line([a for a in actions if a["door"] in (DOOR_PROXY, DOOR_SUBAGENT)]),
        "spread": line(actions, "spread"),
        "by_lane": [{"door": d, "served_class": c, "task_group": g, "quality_sampled": rows[0]["sampled"],
                     **line(rows)} for (d, c, g), rows in sorted(by_lane.items())],
        "by_task_type": {g: line(rows) for g, rows in sorted(by_group.items())},
        "window_open": sum(1 for a in actions if a["window_open"]),
    }
