"""NS4 — the quality breaker: un-route a class whose routed answers keep failing.

North Star (``~/Projects/rsi-engine/docs/NORTH_STAR.md``, NS4): "A routed answer
Claude redoes counts as failure, not success; a class whose redo rate rises is
un-routed automatically." NS1 (``llm_router.northstar``) already classifies every
routed unit as ``used`` | ``redo`` | ``discarded`` | ``unknown`` | ``not_routed``.
This module turns that signal into an automatic on/off switch per CLASS =
``(lever, task_type)`` (optionally refined to ``(lever, task_type, model)`` when
that split alone has enough data — see ``_select_key``), so a lever NS3 turns on
that turns out to perform badly gets turned back off without a human watching a
dashboard.

GENERALISING, NOT ADDING A THIRD MECHANISM
-------------------------------------------
This repo already has two breaker-shaped things:

  1. ``hooks/draft_usage.py`` "I5" — a streak-based auto-revert for the draft
     lever (50 unused drafts in a row -> stop drafting). Kept **completely
     unchanged**: ``should_route("drafts", ...)`` below *delegates* to it
     (``_draft_decision``) rather than re-deriving the same judgement from
     ``northstar.units()`` with different math. Its tests, its state file
     (``draft_streak.json``), and its behaviour are untouched.

  2. ``health.py``'s per-provider circuit breaker — closed/open/half-open with
     a cooldown, and a cross-process JSON snapshot written atomically
     (tmp + ``Path.replace``) so a hook subprocess and ``llm-router doctor``
     agree on state. This module's ``ClassBreaker`` state machine and its
     ``quality_breaker.json`` snapshot literally copy that shape — cooldown
     instead of instantaneous retry, failure_rate instead of consecutive
     failures, but the same three states and the same fail-open persistence.

Every OTHER class (mcp_llm, mcp_llm_act, direct, agent_route, ...) is governed
by the ONE generic state machine here. There is no separate mechanism per
entry point — ``should_route(lever, task_type)`` is the single function every
routing surface calls, and it is this module's only public decision API.

STATE MACHINE
--------------
``closed``    routes normally. Opens when, over the class's last
              ``window()`` routed units (``northstar.units()`` filtered to
              this class, ``outcome != "not_routed"``), ``failure_rate =
              (redo + discarded) / (used + redo + discarded) >= threshold()``
              AND the classified count (``used + redo + discarded``, i.e.
              EXCLUDING unknown — S8/S9 in CLAUDE.md: unknown must never be
              read as the favourable answer, so it is also never read as the
              UNfavourable one; it is excluded from both sides of the rate and
              reported on its own) is ``>= min_n()``.
``open``      does not route; every entry point records why and does the work
              itself instead (never a silent skip — CLAUDE.md's "an unlogged
              branch will cost a day"). After ``cooldown_seconds()`` (default
              24h) elapses since opening, the NEXT evaluation moves to
              ``half_open``.
``half_open`` still routes (this IS the probe), judged against the most
              recent ``probe_size()`` routed units that occurred after entry
              into half-open (a trailing window, so it keeps sliding forward
              as new units arrive rather than freezing on a possibly-all-
              unknown first batch). Fewer than that many units so far ->
              stay half_open, still routing, undecided. Enough units and
              their failure_rate < threshold() -> ``closed``. Enough units
              and failure_rate >= threshold() -> ``open`` again (cooldown
              restarts). A probe with zero classified units (everything
              unknown) decides nothing and stays half_open — the S9 rule
              again.

STATE FILE
----------
``~/.llm-router/quality_breaker.json`` (``LLM_ROUTER_HOME``-relative, same as
every other state file — see ``paths.py``), written atomically
(tmp + ``Path.replace``). An unreadable or malformed file FAILS OPEN: every
class reads as ``closed`` with no history, `failopen.record()` notes it, and
routing proceeds normally. A state file must never be able to crash a hook.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from llm_router import paths, session_kind

STATE_FILE = "quality_breaker.json"

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

_THRESHOLD_DEFAULT = 0.5
_MIN_N_DEFAULT = 20
_WINDOW_DEFAULT = 50
_COOLDOWN_S_DEFAULT = 86400.0
_PROBE_SIZE_DEFAULT = 5
_LOOKBACK_DAYS_DEFAULT = 30

USED = "used"
REDO = "redo"
DISCARDED = "discarded"
UNKNOWN = "unknown"
NOT_ROUTED = "not_routed"
_FAILURE_OUTCOMES = frozenset({REDO, DISCARDED})

UnitsFn = Callable[..., Iterable[dict]]

#: N21 (docs/bugs/N21.md): units of these session kinds never feed the breaker. A research or
#: harness session calls ``llm()`` on purpose and throws the answer away (M0-3 rerun2,
#: 2026-10-10: 20 calls, each answered "DONE"), so its "discarded" outcome measures the
#: experiment, not the router; those 20 units opened ``mcp_llm:code`` for 24 h for every
#: session. ``headless`` (``claude -p`` doing real work), ``organic`` and an untagged
#: session (``None``) still count: dropping untagged units would blind the breaker for any
#: session whose SessionStart hook never ran.
NON_ORGANIC_KINDS = frozenset({session_kind.KIND_RESEARCH, session_kind.KIND_HARNESS})


def _counts_toward_breaker(u: dict) -> bool:
    return u.get("session_kind") not in NON_ORGANIC_KINDS


# ── env-overridable knobs (registered in env_registry.py) ───────────────────
#
# Each reads its OWN literal os.environ.get(...) call (rather than a shared
# helper parameterized by name) because tests/test_env_registry.py's AST scan
# — the independent ground truth env_registry.py is checked against — only
# recognizes a literal string argument; routing the name through a variable
# would make these reads invisible to it and the registry entries "phantom".


def _parse_float(raw: str, default: float) -> float:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_int(raw: str, default: int) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def threshold() -> float:
    return _parse_float(os.environ.get("LLM_ROUTER_QUALITY_BREAKER_THRESHOLD", ""), _THRESHOLD_DEFAULT)


def min_n() -> int:
    return _parse_int(os.environ.get("LLM_ROUTER_QUALITY_BREAKER_MIN_N", ""), _MIN_N_DEFAULT)


def window_size() -> int:
    return _parse_int(os.environ.get("LLM_ROUTER_QUALITY_BREAKER_WINDOW", ""), _WINDOW_DEFAULT)


def cooldown_seconds() -> float:
    return _parse_float(os.environ.get("LLM_ROUTER_QUALITY_BREAKER_COOLDOWN_S", ""), _COOLDOWN_S_DEFAULT)


def probe_size() -> int:
    return _parse_int(os.environ.get("LLM_ROUTER_QUALITY_BREAKER_PROBE_SIZE", ""), _PROBE_SIZE_DEFAULT)


def lookback_days() -> int:
    return _parse_int(os.environ.get("LLM_ROUTER_QUALITY_BREAKER_LOOKBACK_DAYS", ""), _LOOKBACK_DAYS_DEFAULT)


# ── state file: read/write, fail-open ────────────────────────────────────────

def _state_path() -> Path:
    override = os.environ.get("LLM_ROUTER_QUALITY_BREAKER_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.state_path(STATE_FILE)


def _read_state() -> dict:
    """FAIL-OPEN: a missing, corrupt or wrong-shaped file reads as ``{}`` —
    i.e. every class closed, no history — and records the failure. Never
    raises: a hook must never die on a state file it cannot parse."""
    p = _state_path()
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("quality_breaker.json: not a JSON object")
        classes = data.get("classes", {})
        if not isinstance(classes, dict):
            raise ValueError("quality_breaker.json: 'classes' is not an object")
        return data
    except Exception as exc:  # noqa: BLE001 — corrupt state must never block routing
        try:
            from llm_router import failopen
            failopen.record("QB-STATE-READ", exc)
        except Exception:  # noqa: BLE001
            pass
        return {}


def _write_state(state: dict) -> bool:
    """Atomic tmp + replace, same shape as ``health.py``'s snapshot write.
    FAIL-OPEN: never raises into a caller; a failed write just means the next
    evaluation starts from stale (not corrupt) state."""
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            from llm_router import failopen
            failopen.record("QB-STATE-WRITE", exc)
        except Exception:  # noqa: BLE001
            pass
        return False


# ── class identity ───────────────────────────────────────────────────────────

def class_key(lever: str, task_type: str | None, model: str | None = None) -> str:
    base = f"{lever}:{task_type or 'none'}"
    return f"{base}:{model}" if model else base


@dataclass
class Decision:
    """What a caller needs: whether to route, and a REASON either way (assert
    the reason, not just the boolean — CLAUDE.md K7)."""

    allowed: bool
    state: str
    reason: str
    key: str
    n: int  # classified units (used + redo + discarded) the decision is based on
    failure_rate: float | None
    unknown: int = 0


def _fmt_rate(rate: float | None) -> str:
    return f"{rate:.2f}" if rate is not None else "n/a"


# ── unit classification ──────────────────────────────────────────────────────

def _classify(units: list[dict]) -> tuple[int, int, int, float | None]:
    """(used, failed, unknown, failure_rate) over already-filtered units.
    ``failure_rate`` excludes unknown from BOTH sides (S8/S9): it is
    ``failed / (used + failed)``, ``None`` when that denominator is 0."""
    used = failed = unk = 0
    for u in units:
        outcome = u.get("outcome")
        if outcome == USED:
            used += 1
        elif outcome in _FAILURE_OUTCOMES:
            failed += 1
        elif outcome == UNKNOWN:
            unk += 1
        # NOT_ROUTED is filtered out by the caller; ignore defensively if not.
    classified = used + failed
    rate = (failed / classified) if classified else None
    return used, failed, unk, rate


def _ts(u: dict) -> float | None:
    raw = u.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, TypeError):
        return None


def _default_units_fn(**kwargs):
    from llm_router import northstar
    return northstar.units(**kwargs)


def _fetch_class_units(lever: str, task_type: str | None, model: str | None,
                        *, units_fn: UnitsFn | None = None) -> list[dict]:
    """This class's routed units (``outcome != "not_routed"``), oldest first."""
    fn = units_fn or _default_units_fn
    all_units = [u for u in fn(days=lookback_days()) if _counts_toward_breaker(u)]
    matches = [
        u for u in all_units
        if u.get("lever") == lever
        and u.get("task_type") == task_type
        and u.get("outcome") != NOT_ROUTED
        and (model is None or u.get("model") == model)
    ]
    matches.sort(key=lambda u: (_ts(u) is None, _ts(u) or 0.0))
    return matches


def _window(units_sorted: list[dict], n: int) -> list[dict]:
    """The most recent ``n`` (units_sorted is oldest-first)."""
    return units_sorted[-n:] if n > 0 else list(units_sorted)


def _select_key(lever: str, task_type: str | None, model: str | None,
                 mn: int, win: int, units_fn: UnitsFn | None) -> tuple[str, list[dict]]:
    """Per-model refinement: use the finer (lever, task_type, model) key only
    when THAT split alone already clears ``min_n`` on its own; otherwise fall
    back to the coarser (lever, task_type) class."""
    coarse_units = _fetch_class_units(lever, task_type, None, units_fn=units_fn)
    if model:
        fine_units = [u for u in coarse_units if u.get("model") == model]
        used, failed, _unk, _rate = _classify(_window(fine_units, win))
        if (used + failed) >= mn:
            return class_key(lever, task_type, model), fine_units
    return class_key(lever, task_type, None), coarse_units


# ── the state machine (pure — no I/O) ────────────────────────────────────────

def _evaluate_core(cls: dict, key: str, units_sorted: list[dict], now: float,
                    th: float, mn: int, win: int, cool: float, probe_n: int,
                    ) -> tuple[Decision, dict]:
    windowed = _window(units_sorted, win)
    used, failed, unk, rate = _classify(windowed)
    classified_n = used + failed
    cur_state = cls.get("state", CLOSED)
    opened_at = cls.get("opened_at")
    half_open_since = cls.get("half_open_since")
    new_cls = dict(cls)

    if cur_state == OPEN:
        elapsed = now - (opened_at if opened_at is not None else now)
        if opened_at is not None and elapsed >= cool:
            new_cls.update(state=HALF_OPEN, half_open_since=now)
            decision = Decision(
                allowed=True, state=HALF_OPEN, key=key, n=classified_n,
                failure_rate=rate, unknown=unk,
                reason=(f"quality_breaker: {key} half_open — cooldown elapsed, "
                        f"probing {probe_n} unit(s)"),
            )
        else:
            remaining = max(0.0, cool - elapsed)
            decision = Decision(
                allowed=False, state=OPEN, key=key, n=classified_n,
                failure_rate=rate, unknown=unk,
                reason=(f"quality_breaker: {key} OPEN (failure_rate={_fmt_rate(rate)} "
                        f"n={classified_n} unknown={unk}); cooldown {remaining:.0f}s remaining"),
            )
    elif cur_state == HALF_OPEN:
        since = half_open_since if half_open_since is not None else now
        # An absent ts is excluded explicitly, not coerced to 0.0 and compared
        # (lint_unknown_as_number.py: "absent reads as 0" is exactly the S9
        # shape this repo's CLAUDE.md warns about).
        after = [u for u in units_sorted if _ts(u) is not None and _ts(u) > since]
        probe_units = after[-probe_n:] if len(after) >= probe_n else []
        if not probe_units:
            decision = Decision(
                allowed=True, state=HALF_OPEN, key=key, n=classified_n,
                failure_rate=rate, unknown=unk,
                reason=f"quality_breaker: {key} half_open — probe pending ({len(after)}/{probe_n})",
            )
        else:
            p_used, p_failed, p_unk, p_rate = _classify(probe_units)
            p_classified = p_used + p_failed
            if p_classified == 0:
                # All-unknown probe: no evidence either way (S9) — keep probing.
                decision = Decision(
                    allowed=True, state=HALF_OPEN, key=key, n=0,
                    failure_rate=None, unknown=p_unk,
                    reason=(f"quality_breaker: {key} half_open — probe inconclusive "
                            f"(all {p_unk} unknown), continuing to probe"),
                )
            elif p_rate < th:
                new_cls.update(state=CLOSED, opened_at=None, half_open_since=None)
                decision = Decision(
                    allowed=True, state=CLOSED, key=key, n=p_classified,
                    failure_rate=p_rate, unknown=p_unk,
                    reason=(f"quality_breaker: {key} CLOSED — probe passed "
                            f"(failure_rate={_fmt_rate(p_rate)} n={p_classified})"),
                )
            else:
                new_cls.update(state=OPEN, opened_at=now, half_open_since=None)
                decision = Decision(
                    allowed=False, state=OPEN, key=key, n=p_classified,
                    failure_rate=p_rate, unknown=p_unk,
                    reason=(f"quality_breaker: {key} REOPENED — probe failed "
                            f"(failure_rate={_fmt_rate(p_rate)} n={p_classified} >= threshold {th})"),
                )
    else:  # CLOSED
        if classified_n >= mn and rate is not None and rate >= th:
            new_cls.update(state=OPEN, opened_at=now, half_open_since=None)
            decision = Decision(
                allowed=False, state=OPEN, key=key, n=classified_n,
                failure_rate=rate, unknown=unk,
                reason=(f"quality_breaker: {key} OPENED — failure_rate={_fmt_rate(rate)} "
                        f"n={classified_n} >= threshold {th} (min_n={mn})"),
            )
        else:
            decision = Decision(
                allowed=True, state=CLOSED, key=key, n=classified_n,
                failure_rate=rate, unknown=unk,
                reason=(f"quality_breaker: {key} closed — failure_rate={_fmt_rate(rate)} "
                        f"n={classified_n} unknown={unk} (min_n={mn}, threshold={th})"),
            )

    new_cls["failure_rate"] = decision.failure_rate
    new_cls["n"] = decision.n
    new_cls["unknown"] = unk
    new_cls["last_eval_at"] = now
    new_cls["state"] = decision.state
    return decision, new_cls


# ── draft lever: delegate to the existing I5 mechanism, unchanged ───────────

def _draft_decision() -> Decision:
    from llm_router.hooks import draft_usage
    reverted = draft_usage.drafting_reverted()
    streak = draft_usage.unused_streak()
    key = class_key("drafts", None)
    if reverted:
        return Decision(
            allowed=False, state=OPEN, key=key, n=streak, failure_rate=1.0, unknown=0,
            reason=(f"quality_breaker: {key} OPEN — draft auto-revert (I5): last "
                    f"{reverted} drafts unused in a row (delete draft_streak.json to resume)"),
        )
    return Decision(
        allowed=True, state=CLOSED, key=key, n=streak, failure_rate=None, unknown=0,
        reason=f"quality_breaker: {key} closed — draft auto-revert (I5): unused streak {streak}",
    )


# ── public entry point ───────────────────────────────────────────────────────

def should_route(lever: str, task_type: str | None, *, model: str | None = None,
                  now: float | None = None, units_fn: UnitsFn | None = None) -> Decision:
    """The single check every routing entry point makes before using a class.

    ``lever`` is one of northstar's lever names ("drafts", "direct",
    "mcp_llm", "agent_route", ...; MCP-only levers this module adds, like
    "mcp_llm_act", are equally valid class names). ``lever == "drafts"``
    delegates entirely to the existing I5 auto-revert; everything else runs
    the generic failure-rate breaker above.
    """
    if lever == "drafts":
        return _draft_decision()

    now = now if now is not None else time.time()
    th, mn, win = threshold(), min_n(), window_size()
    cool, probe_n = cooldown_seconds(), probe_size()

    key, units_sorted = _select_key(lever, task_type, model, mn, win, units_fn)

    state = _read_state()
    classes = state.get("classes")
    if not isinstance(classes, dict):
        classes = {}
    cls = dict(classes.get(key, {}))

    decision, new_cls = _evaluate_core(cls, key, units_sorted, now, th, mn, win, cool, probe_n)

    classes[key] = new_cls
    _write_state({"version": 1, "classes": classes})
    return decision


def evaluate_dry(lever: str, task_type: str | None, *, model: str | None = None,
                  now: float | None = None, units_fn: UnitsFn | None = None,
                  starting_state: dict | None = None) -> Decision:
    """Pure evaluation — no read or write of the real state file. Used by the
    read-only dry run (``dry_run``) so it can report "what WOULD happen" over
    historical data without perturbing production breaker state.

    Deliberately does NOT special-case ``lever == "drafts"`` the way
    ``should_route`` does: production gating delegates that lever to I5's own
    live streak (``draft_usage.drafting_reverted()``), which reflects
    whatever the real streak counter currently is — a fact about the running
    process, not about history. The dry run's question is "what would the
    generic failure-rate breaker say about this class over its
    northstar.units() history", for EVERY class alike, drafts included; that
    is what makes it comparable across classes and is why draft's real
    numbers (0 used / many discarded) can legitimately show here as the
    worst class even while I5's live streak has not (yet) tripped.
    """
    now = now if now is not None else time.time()
    th, mn, win = threshold(), min_n(), window_size()
    cool, probe_n = cooldown_seconds(), probe_size()

    key, units_sorted = _select_key(lever, task_type, model, mn, win, units_fn)
    cls = dict((starting_state or {}).get(key, {}))
    decision, _new_cls = _evaluate_core(cls, key, units_sorted, now, th, mn, win, cool, probe_n)
    return decision


# ── visibility: status / northstar / Stop-line ──────────────────────────────

def open_classes(*, units_fn: UnitsFn | None = None) -> list[dict]:
    """Every class currently ``open`` or ``half_open``, generic classes from
    the persisted snapshot plus the draft lever folded in from I5 — ONE list,
    so `llm-router status`/`northstar` never have to know there are two
    underlying mechanisms."""
    state = _read_state()
    classes = state.get("classes")
    out: list[dict] = []
    if isinstance(classes, dict):
        for key, cls in classes.items():
            if cls.get("state") in (OPEN, HALF_OPEN):
                out.append({
                    "key": key,
                    "state": cls.get("state"),
                    "failure_rate": cls.get("failure_rate"),
                    "n": cls.get("n", 0),
                    "unknown": cls.get("unknown", 0),
                })
    try:
        d = _draft_decision()
        if d.state != CLOSED:
            out.append({
                "key": d.key, "state": d.state, "failure_rate": d.failure_rate,
                "n": d.n, "unknown": d.unknown,
            })
    except Exception:  # noqa: BLE001 — visibility must never raise
        pass
    return sorted(out, key=lambda r: r["key"])


def stop_line_summary(*, units_fn: UnitsFn | None = None) -> str | None:
    """The Stop-line item: ``None`` when nothing is open (furniture is worse
    than silence — see status_premium.py's T-07), else e.g.
    ``"breaker: 2 classes off"``."""
    try:
        n = len(open_classes(units_fn=units_fn))
    except Exception:  # noqa: BLE001
        return None
    if n == 0:
        return None
    return f"breaker: {n} class{'es' if n != 1 else ''} off"


# ── read-only dry run over real history ──────────────────────────────────────

def dry_run(days: int | None = None, units_fn: UnitsFn | None = None) -> list[dict]:
    """Evaluate every (lever, task_type) class seen in the last ``days`` of
    real ``northstar.units()`` from a FRESH (empty) starting state — "if this
    breaker had been watching all along, would this class be open right now"
    — without touching the real persisted state file. Every class that
    produced at least one routed unit is reported, not only the ones that
    would open, so a closed class's headroom (n, failure_rate) is visible too.
    """
    fn = units_fn or _default_units_fn
    win_days = days if days is not None else lookback_days()
    all_units = [u for u in fn(days=win_days) if _counts_toward_breaker(u)]

    def _frozen_units_fn(**_kwargs):
        return all_units

    seen: set[tuple[str, str | None]] = set()
    for u in all_units:
        if u.get("outcome") == NOT_ROUTED:
            continue
        lever = u.get("lever")
        if not lever or lever == "none":
            continue
        seen.add((lever, u.get("task_type")))

    rows = []
    for lever, task_type in sorted(seen, key=lambda kv: (kv[0], kv[1] or "")):
        d = evaluate_dry(lever, task_type, units_fn=_frozen_units_fn)
        rows.append({
            "lever": lever, "task_type": task_type, "would_be": d.state,
            "failure_rate": d.failure_rate, "n": d.n, "unknown": d.unknown,
            "reason": d.reason,
        })
    return rows
