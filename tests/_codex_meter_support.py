"""Support for the Codex meter tests: write rollout files the way Codex does, into a temp CODEX_HOME.

The line shape is the one read from a copy of a real ``codex exec`` rollout (codex-cli 0.161.0, 2026-11-04): an ``event_msg`` of type
``token_count`` whose payload carries ``rate_limits`` with ``limit_id`` ``codex``, ``primary`` (5 h, ``window_minutes`` 300) and
``secondary`` (weekly, 10080). A ``limit_id`` ``premium`` event has null windows.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

WEEK_S = 7 * 24 * 3600


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rate_limits(*, weekly: float | None, five: float | None, now: float, limit_id: str = "codex",
                weekly_resets: float | None = None, five_resets: float | None = None) -> dict:
    if limit_id != "codex":
        return {"limit_id": limit_id, "limit_name": None, "primary": None, "secondary": None, "plan_type": "plus"}
    return {
        "limit_id": "codex", "limit_name": None,
        "primary": None if five is None else {
            "used_percent": five, "window_minutes": 300, "resets_at": int(five_resets or now + 2 * 3600)},
        "secondary": None if weekly is None else {
            "used_percent": weekly, "window_minutes": 10080, "resets_at": int(weekly_resets or now + 3 * 24 * 3600)},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "plan_type": "plus", "rate_limit_reached_type": None,
    }


def write_rollout(codex_home: Path, readings: list[tuple[float, dict]], *, mtime: float | None = None,
                  file_clock: float | None = None, noise: int = 3) -> Path:
    """One rollout file holding ``readings`` as ``(event time, rate_limits)`` after some lines that carry no reading.

    The directory is the UTC date of the first event; the NAME carries ``file_clock`` (default: 3 hours off, as local time is
    off UTC), which a correct reader never uses."""
    first = readings[0][0]
    day = datetime.fromtimestamp(first, timezone.utc)
    folder = codex_home / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    clock = datetime.fromtimestamp(file_clock if file_clock is not None else first + 3 * 3600, timezone.utc)
    path = folder / f"rollout-{clock:%Y-%m-%dT%H-%M-%S}-{uuid.uuid4()}.jsonl"
    lines = [json.dumps({"timestamp": iso(first), "type": "session_meta", "payload": {"id": "x", "originator": "codex_exec"}})]
    lines += [json.dumps({"timestamp": iso(first), "type": "response_item", "payload": {"type": "message", "text": "noise"}})
              for _ in range(noise)]
    for at, limits in readings:
        lines.append(json.dumps({"timestamp": iso(at), "type": "event_msg",
                                 "payload": {"type": "token_count", "info": None, "rate_limits": limits}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = max(at for at, _ in readings) if mtime is None else mtime
    os.utime(path, (stamp, stamp))
    return path
