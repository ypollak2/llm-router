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
A day is a UTC day. A call whose token use is unknown counts as a call, not as tokens.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone

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


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


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


def admit_haiku(now: float, est_tokens: int = 0) -> str | None:
    """None when a Haiku judge call may go out now, else the outcome that stops it."""
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
    max_calls, max_tokens = haiku_caps()
    used = usage_today(now)
    if used["haiku_calls"] >= max_calls:
        return OUT_CAP_CALLS
    if used["haiku_tokens"] + max(est_tokens, 0) > max_tokens:
        return OUT_CAP_TOKENS
    return None


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
    max_calls, max_tokens = haiku_caps()
    flag = "on" if judge_select.haiku_fallback_enabled() else "off"
    return (f"judge spend today (UTC): haiku calls {u['haiku_calls']}/{max_calls}, "
            f"haiku tokens {u['haiku_tokens']}/{max_tokens}, local judge calls {u['local_calls']}; "
            f"haiku fallback {flag}")


@contextlib.contextmanager
def no_proxy_env() -> Iterator[None]:
    """A judge call never loops through the local proxy: its base URL is removed for the call."""
    saved = {k: os.environ.pop(k) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE") if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
