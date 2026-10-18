"""Judge spend ledger and caps (P3.1-4). Rows hold no prompt or answer text.

``judge_ledger.jsonl``: one row per judge decision that reached a selected judge:
``ts, door, answering_model, judge_model, judge_kind, tokens, cost_usd, outcome``.
``tokens`` and ``cost_usd`` are None when unknown; ``cost_usd`` is 0 only for a
measured local call, never as a stand-in for "not measured".

The Haiku (subscription) judge is the only judge that spends quota. It is stopped,
with the outcome recorded, when any of these holds:
  * ``LLM_ROUTER_JUDGE_HAIKU_FALLBACK`` is not on (privacy: a local answer's prompt
    would leave the machine; default off, fails closed)    -> ``privacy_off``
  * the subscription quota reading is stale, unknown, session >= 80% or
    weekly >= 85% (``shadow_frontier.quota_skip``)          -> ``quota_skip``
  * today's Haiku calls reached ``LLM_ROUTER_JUDGE_HAIKU_MAX_CALLS`` (20)  -> ``cap_calls``
  * today's Haiku tokens would pass ``LLM_ROUTER_JUDGE_HAIKU_DAILY_VOLUME`` (400000, in tokens)
                                                            -> ``cap_tokens``
A day is a LOCAL day (midnight in the machine's timezone), the same window as D-218's
"today" (``ledger_view.resolve("today")``).

F97 atomic admission: the caps are checked and a reservation row is written under one lock
(``judge_ledger.lock``), so two simultaneous drains cannot both pass at 19/20. The reservation
lives in ``judge_reservations.jsonl`` (no text; ``ts, rid, tokens, outcome``), not in the spend
ledger, so the ledger keeps one row per decision. It counts as one call and its estimated
tokens until :func:`release_reservation` runs after the call. A call whose token use is
unknown leaves an ``estimated`` row: the pre-call estimate counts toward the token cap, not 0.
A reservation older than ``RESERVATION_TTL_S`` (a crashed drain) stops counting.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
import uuid

from llm_router import failopen, paths

LEDGER_FILENAME = "judge_ledger.jsonl"
MAX_ROWS = 5000
_TRIM_BYTES = 1_500_000

OUT_GRADED = "graded"
OUT_FAILED = "failed"
OUT_PRIVACY = "privacy_off"
OUT_QUOTA = "quota_skip"
OUT_CAP_CALLS = "cap_calls"
OUT_CAP_TOKENS = "cap_tokens"
OUT_LOCK = "lock_busy"
#: Reservation-file outcomes (not ledger outcomes).
RES_RESERVED = "reserved"
RES_ESTIMATED = "estimated"
RESERVED_FILENAME = "judge_reservations.jsonl"
LOCK_FILENAME = "judge_ledger.lock"
RESERVATION_TTL_S = 900.0
_LOCK_WAIT_S = 5.0
_LOCK_STALE_S = 30.0
_ESTIMATE_KEEP_S = 2 * 86_400.0
#: Outcomes in which a model call was actually made.
CALLED = frozenset({OUT_GRADED, OUT_FAILED})

DEFAULT_MAX_CALLS = 20
DEFAULT_MAX_TOKENS = 400_000


def ledger_path():
    return paths.state_path(LEDGER_FILENAME)


def _non_negative_int(raw: str, default: int, name: str) -> int:
    raw = raw.strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError as exc:
        failopen.record("CHZ-FO-JUDGE-LEDGER-CONFIG", exc, detail=f"{name} unparseable")
        return default


def haiku_caps() -> tuple[int, int]:
    """(calls per day, tokens per day)."""
    return (
        _non_negative_int(os.environ.get("LLM_ROUTER_JUDGE_HAIKU_MAX_CALLS", ""), DEFAULT_MAX_CALLS, "max calls"),
        _non_negative_int(os.environ.get("LLM_ROUTER_JUDGE_HAIKU_DAILY_VOLUME", ""), DEFAULT_MAX_TOKENS, "daily volume"),
    )


def _day(ts: float) -> float:
    """Start of the local day holding ``ts``: D-218's "today" bound, from the one shared helper."""
    from llm_router import ledger_view

    return ledger_view.resolve("today", now=ts).since_ts


def read_rows() -> list[dict]:
    try:
        text = ledger_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        failopen.record("CHZ-FO-JUDGE-LEDGER-READ", exc)
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def usage_today(now: float | None = None) -> dict:
    """Today's judge spend: Haiku calls and tokens, and local calls."""
    day = _day(time.time() if now is None else now)
    calls = tokens = local = 0
    for r in read_rows():
        ts = r.get("ts")
        if not isinstance(ts, (int, float)) or _day(ts) != day or r.get("outcome") not in CALLED:
            continue
        if r.get("judge_kind") == "subscription":
            calls += 1
            t = r.get("tokens")
            tokens += t if isinstance(t, int) and not isinstance(t, bool) else 0
        else:
            local += 1
    return {"haiku_calls": calls, "haiku_tokens": tokens, "local_calls": local}


def reserved_path():
    return paths.state_path(RESERVED_FILENAME)


def _read_reserved() -> list[dict]:
    try:
        text = reserved_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        failopen.record("CHZ-FO-JUDGE-LEDGER-READ", exc)
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _reserved_today(now: float) -> tuple[int, int]:
    """(calls, tokens) held by live reservations and unknown-token estimates today."""
    day = _day(now)
    calls = tokens = 0
    for r in _read_reserved():
        ts, t = r.get("ts"), r.get("tokens")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or _day(ts) != day:
            continue
        if not isinstance(t, int) or isinstance(t, bool):
            continue
        if r.get("outcome") == RES_RESERVED and now - ts <= RESERVATION_TTL_S:
            calls += 1
            tokens += t
        elif r.get("outcome") == RES_ESTIMATED:
            tokens += t
    return calls, tokens


@contextlib.contextmanager
def _ledger_lock() -> Iterator[None]:
    """Exclusive lock across threads and processes (``O_EXCL`` file; a lock older than 30 s is stale)."""
    path = paths.state_path(LOCK_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > _LOCK_STALE_S:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                failopen.record("CHZ-FO-JUDGE-LEDGER-LOCK", exc)
            if time.monotonic() > deadline:
                raise TimeoutError("judge ledger lock busy")
            time.sleep(0.005)
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError as exc:
            failopen.record("CHZ-FO-JUDGE-LEDGER-LOCK", exc)


def _gate(now: float) -> str | None:
    """The stop for the checks that hold no spend state: privacy flag, then quota reading."""
    from llm_router import judge_select

    if not judge_select.haiku_fallback_enabled():
        return OUT_PRIVACY
    try:
        from llm_router import shadow_frontier

        if shadow_frontier.quota_skip(now) is not None:
            return OUT_QUOTA
    except Exception as exc:  # noqa: BLE001 — unknown quota means no call
        failopen.record("CHZ-FO-JUDGE-QUOTA", exc)
        return OUT_QUOTA
    return None


def _caps_stop(now: float, est_tokens: int) -> str | None:
    max_calls, max_tokens = haiku_caps()
    used = usage_today(now)
    held_calls, held_tokens = _reserved_today(now)
    if used["haiku_calls"] + held_calls >= max_calls:
        return OUT_CAP_CALLS
    if used["haiku_tokens"] + held_tokens + max(est_tokens, 0) > max_tokens:
        return OUT_CAP_TOKENS
    return None


def admit_haiku(now: float, est_tokens: int = 0) -> str | None:
    """None when a Haiku judge call would be admitted now, else the outcome that stops it.

    A read-only check; a caller about to make the call uses :func:`reserve_haiku`, which is atomic.
    """
    return _gate(now) or _caps_stop(now, est_tokens)


def reserve_haiku(now: float, est_tokens: int = 0) -> tuple[str | None, str | None]:
    """(stop, reservation id): admit a Haiku call and reserve its spend atomically.

    Caps are checked and the reservation row is appended under one lock, so concurrent
    callers cannot both take the last slot. Exactly one of the pair is not None. The caller
    must pass the id to :func:`release_reservation` once the call is done. A lock that cannot
    be taken fails closed (``lock_busy``).
    """
    stop = _gate(now)
    if stop is not None:
        return stop, None
    est = max(est_tokens, 0)
    rid = uuid.uuid4().hex
    try:
        from llm_router.judge_hygiene import append_private

        with _ledger_lock():
            stop = _caps_stop(now, est)
            if stop is not None:
                return stop, None
            append_private(reserved_path(), [json.dumps(
                {"ts": now, "rid": rid, "tokens": est, "outcome": RES_RESERVED})])
    except (OSError, TimeoutError) as exc:
        failopen.record("CHZ-FO-JUDGE-LEDGER-LOCK", exc)
        return OUT_LOCK, None
    return None, rid


def release_reservation(rid: str, final_tokens: int | None) -> None:
    """Close a reservation after its call. Never raises.

    Known ``final_tokens``: the ledger row carries them, so the reservation is dropped. Unknown
    (None): the reservation becomes an ``estimated`` row and its pre-call estimate keeps
    counting toward the token cap. Estimates older than two days are dropped.
    """
    try:
        from llm_router.judge_hygiene import rewrite_private

        with _ledger_lock():
            now = time.time()
            keep = []
            for r in _read_reserved():
                if r.get("rid") == rid:
                    if final_tokens is not None:
                        continue
                    r = {**r, "outcome": RES_ESTIMATED}
                ts = r.get("ts")
                if isinstance(ts, (int, float)) and now - ts > _ESTIMATE_KEEP_S:
                    continue  # a reservation or estimate this old no longer counts
                keep.append(json.dumps(r))
            rewrite_private(reserved_path(), keep)
    except (OSError, TimeoutError) as exc:
        failopen.record("CHZ-FO-JUDGE-LEDGER-LOCK", exc)


def record(
    *, door: str | None, answering_model: str, judge_model: str | None, judge_kind: str | None,
    tokens: int | None, cost_usd: float | None, outcome: str, now: float | None = None,
) -> None:
    """Append one text-free row; trims to the newest ``MAX_ROWS`` once the file is large."""
    from llm_router.judge_sampler import normalize_door

    row = {"ts": time.time() if now is None else now, "door": normalize_door(door),
           "answering_model": answering_model, "judge_model": judge_model, "judge_kind": judge_kind,
           "tokens": tokens, "cost_usd": cost_usd, "outcome": outcome}
    try:
        from llm_router.judge_hygiene import append_private, rewrite_private

        path = ledger_path()
        append_private(path, [json.dumps(row)])
        if path.stat().st_size > _TRIM_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > MAX_ROWS:
                rewrite_private(path, lines[-MAX_ROWS:])
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-JUDGE-LEDGER-WRITE", exc)


def status_text(now: float | None = None) -> str:
    """Today's judge spend against the caps, for ``judge status`` and ``kpi --judge``."""
    from llm_router import judge_select

    u = usage_today(now)
    estimated = _reserved_today(time.time() if now is None else now)[1]
    max_calls, max_tokens = haiku_caps()
    flag = "on" if judge_select.haiku_fallback_enabled() else "off"
    return (f"judge spend today (local day): haiku calls {u['haiku_calls']}/{max_calls}, "
            f"haiku tokens {u['haiku_tokens'] + estimated}/{max_tokens}, local judge calls {u['local_calls']}; "
            f"haiku fallback {flag}")


@contextlib.contextmanager
def no_proxy_env() -> Iterator[None]:
    """A judge call never loops through the local proxy: its base URL is removed for the call.

    DRAIN PROCESS ONLY. This mutates the process-global ``os.environ`` across an ``await``, so it
    is safe only in a process that runs nothing but judge coroutines (the detached session-start
    drain and ``llm-router judge drain``); inside the MCP server it would strip the variable from
    concurrent calls. ``providers.call_llm`` takes no base-url override (``extra_params`` refuses
    one on purpose), so the call cannot be given an explicit env instead. A test keeps
    ``drain_queue`` out of the server modules (``test_f97_judge_followups``).
    """
    saved = {k: os.environ.pop(k) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE") if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
