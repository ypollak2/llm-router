"""Lane A shadow sampling rules (PR3; plan W1-4; D-285, D-292, D-293). Pure functions and constants: no file
is opened for writing here, no model is called, and nothing here runs unless lane A actually SERVED a step.

Lane A serves a continuation step on Haiku. Its quality evidence is a Frontier replay of the SAME step (the
client's unmodified bytes, sent to the model the client asked for), judged blind by the existing Sonnet judge
(``shadow_frontier.judge_pending``). ``shadow_frontier.FrontierShadow`` runs the replay in the background after
the client has its reply; this module says WHICH served steps are replayed and how many:

* **Deterministic key.** ``u = sha256(f"{session_id}|{msg_count}|{SALT}")[:8] / 2**64``; a step is sampled iff
  ``u < rate``. No ``random``, no clock: the same step always gets the same answer, so a retry or a second
  proxy cannot sample it twice or skip it the other time.
* **Fill phase, then steady (the OD-4 = A pattern, per task_group).** ``rate`` is 1.0 until the task group has
  ``FILL_UNTIL_DECIDED_PAIRS`` (60) decided pairs in the trailing ``WINDOW_DAYS`` (14) days of verdict rows,
  then ``STEADY_RATE`` (2%). 1-2% alone cannot reach 60 pairs at the expected volume (D-292), and the arm's
  time-based clock (100% for 14 days after the shadow first ran) is Haiku-turn shaped, so lane A has its own.
* **Own bucket inside the global cap.** At most ``BUCKET_CALLS_PER_DAY`` (12) lane A replays and
  ``BUCKET_INPUT_TOKENS_PER_DAY`` (400k) lane A input tokens per UTC day (D-293). These sit INSIDE
  ``shadow_frontier.MAX_CALLS_PER_DAY`` (20) / ``MAX_INPUT_TOKENS_PER_DAY`` (400k), which this change does not
  raise: every replay, lane A or arm, counts toward the global cap, so the D-31 arm gets only what lane A left.
  A skip because of the bucket reads ``lane_a_bucket``.

The numbers are the ones PREREG-LANEA (draft d1) lists as its ``LANEA-CONSTANTS``; the key names below match
those keys so the constants test can compare them one for one. The prereg, not this file, freezes them.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LANE_TAG = "A"
#: The salt of the deterministic key. A fixed literal; the prereg freeze fills in the final one.
SALT = "laneA-prereg-20261025-v1"
FILL_RATE = 1.0
STEADY_RATE = 0.02
FILL_UNTIL_DECIDED_PAIRS = 60
WINDOW_DAYS = 14
BUCKET_CALLS_PER_DAY = 12
BUCKET_INPUT_TOKENS_PER_DAY = 400_000
CANNOT_JUDGE_MAX_SHARE = 0.20
#: The unit lane A is judged in: ``lane_quality`` lane ``(door, served_class, task_group)``.
DOOR = "proxy"
SERVED_CLASS = "haiku"

PHASE_FILL = "fill"
PHASE_STEADY = "steady"
R_LANE_BUCKET = "lane_a_bucket"
R_NO_KEY = "no_key"

#: The prereg's ``LANEA-CONSTANTS`` keys this module owns, with the value in force.
CONSTANTS = {
    "SALT": SALT,
    "FILL_RATE": FILL_RATE,
    "FILL_UNTIL_DECIDED_PAIRS": FILL_UNTIL_DECIDED_PAIRS,
    "STEADY_RATE": STEADY_RATE,
    "LANE_A_CALLS_PER_DAY": BUCKET_CALLS_PER_DAY,
    "LANE_A_INPUT_TOKENS_PER_DAY": BUCKET_INPUT_TOKENS_PER_DAY,
    "MIN_PAIRS": 60,
    "CANNOT_JUDGE_MAX_SHARE": CANNOT_JUDGE_MAX_SHARE,
    "WINDOW_DAYS": WINDOW_DAYS,
}


def sample_key(session_id: Any, msg_count: Any, salt: str = SALT) -> float | None:
    """The step's position in [0, 1), or None when it has no usable (session, msg_count) key."""
    if not isinstance(session_id, str) or not session_id:
        return None
    if isinstance(msg_count, bool) or not isinstance(msg_count, int) or msg_count < 0:
        return None
    digest = hashlib.sha256(f"{session_id}|{msg_count}|{salt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def pair_order_key(pair_id: Any, salt: str = SALT) -> float:
    """A/B order of one pair for the blind judge: deterministic in the pair id, so a re-judge shows the same order."""
    digest = hashlib.sha256(f"{pair_id}|{salt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def phase_for(decided: int) -> tuple[float, str]:
    """``(rate, phase)`` for a task group that has ``decided`` decided pairs. Exactly the 60th flips it."""
    if decided >= FILL_UNTIL_DECIDED_PAIRS:
        return STEADY_RATE, PHASE_STEADY
    return FILL_RATE, PHASE_FILL


def is_lane_a(row: Any) -> bool:
    return isinstance(row, dict) and row.get("lane") == LANE_TAG


def has_tags(row: dict) -> bool:
    """A lane A pair / verdict row is usable as evidence only if it names its task group and rate phase."""
    return (isinstance(row.get("task_group"), str) and bool(row["task_group"])
            and row.get("rate_phase") in (PHASE_FILL, PHASE_STEADY))


def is_decided(row: dict) -> bool:
    """Both answers judged: two booleans and ``cannot_judge`` not true (prereg section 6)."""
    return (row.get("cannot_judge") is not True and isinstance(row.get("acceptable"), bool)
            and isinstance(row.get("frontier_acceptable"), bool))


def _ts(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def count_decided(rows: Iterable[dict], task_group: str, *, since: float, until: float) -> int:
    """Decided lane A pairs of ``task_group`` among verdict ``rows`` stamped inside ``[since, until]``."""
    n = 0
    for r in rows:
        ts = _ts(r.get("ts"))
        if (is_lane_a(r) and r.get("task_group") == task_group and has_tags(r) and is_decided(r)
                and ts is not None and since <= ts <= until):
            n += 1
    return n


def lane_day_spend(ledger_rows: Iterable[dict], day: str) -> tuple[int, int]:
    """``(calls, input tokens)`` the lane A bucket spent on UTC ``day``: the shadow ledger's lane A rows that made
    a Frontier call. Same accounting as ``shadow_frontier.day_spend`` (a failed replay is a call; its size counts
    at the estimate it was admitted with)."""
    calls = tokens = 0
    for r in ledger_rows:
        if not is_lane_a(r) or r.get("day") != day or r.get("outcome") not in ("replayed", "replay_failed"):
            continue
        calls += 1
        used, est = r.get("input_tokens"), r.get("est_input_tokens")
        tokens += used if isinstance(used, int) else (est if isinstance(est, int) else 0)
    return calls, tokens


def task_group_of(task_type: Any, prev_tool_class: Any) -> str:
    """The S1 task group of a step: ``offload_share``'s own rule, so the shadow and the KPI agree."""
    from llm_router import offload_share

    return offload_share._task_group(task_type, prev_tool_class)


def context_from_row(row: dict, body: dict) -> dict | None:
    """The lane context of a served step, or None when lane A did not serve it. Text-free: ids, a count, labels."""
    from llm_router.proxy import lane_a

    if row.get("lane_a_reason") != lane_a.R_SERVED:
        return None
    msgs = body.get("messages") if isinstance(body, dict) else None
    prev = row.get("prev_tool_class")
    return {"lane": LANE_TAG, "session_id": row.get("session_id"),
            "msg_count": len(msgs) if isinstance(msgs, list) else None,
            "task_group": task_group_of(row.get("tier_task_type"), prev),
            "prev_tool_class": prev, "conf": lane_a.clean_conf(row.get("lane_a_conf"))}


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def read_verdict_rows(path: Path) -> list[dict] | None:
    """Verdict rows (dicts), ``[]`` when the file is absent, ``None`` when it exists but cannot be read (the
    caller then keeps its current phase instead of falling back to the fill rate)."""
    import json

    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        return None
    out = []
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out
