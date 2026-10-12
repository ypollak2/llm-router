"""Quality-sampled lanes for the S1 offload numerator (PLAN v16 section 1.1, P1.9).

A LANE is ``(door, served_class, task_group)``:

* door         ``proxy`` | ``subagent`` | ``mcp`` | ``direct`` | ``techop`` | ``hook``
* served_class ``local`` | ``haiku`` | ``very_cheap`` (another model at or below Haiku's input price)
* task_group   ``qa`` | ``code`` | ``tech_op``

A lane is *quality-sampled* when its LATEST batch (blind audit or shadow-pair batch) inside the trailing
``WINDOW_DAYS`` days passed:

* audit         ``n >= 30`` and ``acceptable / n >= 26 / 30``;
* shadow_pairs  ``n >= 60``, point gap ``<= 3`` points and Newcombe lower bound ``>= -10``.

The latest batch decides: a newer failing batch un-samples a lane that an older batch passed. Until the redo
detector is validated (P3.6-c) this applies to EVERY lane, because "not redone" is true almost always
(detector precision 2/5, recall 2/12, 1 escalation in 26,096 rows, PLAN section 1.1). A row in an unsampled
lane counts in the denominator only.

The batches live in ``lane_quality.jsonl`` in the state dir, one JSON object per line, written by whatever
runs the audit or the shadow pairs (this module only reads):

    {"ts": <epoch s or ISO>, "door": "proxy", "served_class": "haiku", "task_group": "qa",
     "kind": "audit", "n": 30, "acceptable": 27, "source": "<path or id of the batch>"}
    {"ts": ..., "door": ..., "served_class": ..., "task_group": ..., "kind": "shadow_pairs",
     "n": 64, "gap_pts": 2.1, "newcombe_lb": -6.4, "source": "..."}

No file, an unreadable file or a malformed row yields no sampled lane: the numerator is then 0 and the report
says so. Nothing here is guessed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

WINDOW_DAYS = 28
AUDIT_MIN_N = 30
AUDIT_MIN_ACCEPTABLE = 26          # of AUDIT_MIN_N, or the same rate at larger n
PAIRS_MIN_N = 60
PAIRS_MAX_GAP_PTS = 3.0
PAIRS_MIN_LB = -10.0
FILE = "lane_quality.jsonl"

Lane = tuple[str, str, str]


def _ts(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw == raw and abs(raw) < 1e11 else None
    if isinstance(raw, str):
        try:
            d = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _int(x: Any) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def _real(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None


def batch_passed(row: dict) -> bool | None:
    """Did this batch pass its bar? None when the row is not a readable batch."""
    kind, n = row.get("kind"), _int(row.get("n"))
    if n is None:
        return None
    if kind == "audit":
        ok = _int(row.get("acceptable"))
        if ok is None or not 0 <= ok <= n:
            return None
        # the same rate at larger n: ok / n >= 26 / 30, in integers
        return n >= AUDIT_MIN_N and ok * AUDIT_MIN_N >= AUDIT_MIN_ACCEPTABLE * n
    if kind == "shadow_pairs":
        gap, lb = _real(row.get("gap_pts")), _real(row.get("newcombe_lb"))
        if gap is None or lb is None:
            return None
        return n >= PAIRS_MIN_N and gap <= PAIRS_MAX_GAP_PTS and lb >= PAIRS_MIN_LB
    return None


def lane_of(row: dict) -> Lane | None:
    lane = tuple(row.get(k) for k in ("door", "served_class", "task_group"))
    return lane if all(isinstance(x, str) and x for x in lane) else None  # type: ignore[return-value]


def sampled_lanes(rows: Iterable[dict], now: float) -> dict[Lane, dict]:
    """``{lane: latest batch}`` for the lanes whose latest batch inside ``[now - 28 d, now]`` passed.
    Each value: ``{"kind", "n", "ts", "source"}``."""
    latest: dict[Lane, tuple[float, dict, bool]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts, lane, passed = _ts(row.get("ts")), lane_of(row), batch_passed(row)
        if ts is None or lane is None or passed is None or not now - WINDOW_DAYS * 86400.0 <= ts <= now:
            continue
        if lane not in latest or ts >= latest[lane][0]:
            latest[lane] = (ts, row, passed)
    return {lane: {"kind": r["kind"], "n": r["n"], "ts": t, "source": r.get("source")}
            for lane, (t, r, ok) in latest.items() if ok}


def read_rows(path: Path | None = None) -> list[dict]:
    """``lane_quality.jsonl`` rows; [] when the file is absent or unreadable."""
    if path is None:
        from llm_router import paths
        path = paths.state_path(FILE)
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
