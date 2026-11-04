"""Count the model responses of a ``codex exec`` run from the rollout files Codex writes.

``codex exec`` writes one rollout per thread under ``$CODEX_HOME/sessions/YYYY/MM/DD/``, named
``rollout-<local time>-<thread id>.jsonl``. A delegated run is not one model call: Codex answers, runs a tool,
answers again, and may spawn sub-agent threads of its own (their rollouts carry
``session_meta.source.subagent.thread_spawn.parent_thread_id``). Counting the delegation as one call hides all
of that, so the status line counts what the rollout shows:

* a MODEL RESPONSE is one ``token_usage_record`` line, identified by its ``response_id`` (the Responses API id of
  one sampling request; the analogue of a Claude ``message.id``). A forked or resumed rollout repeats the lines of
  its parent, so responses are counted as a SET of ids across the run's thread and every descendant thread;
* a rollout from a Codex CLI that writes no ``token_usage_record`` is counted by its ``token_count`` events that
  carry usage (one per response), summed;
* a run whose rollout cannot be found, or holds neither record, has NO countable responses: the caller counts the
  run as one response and flags it. It is never counted as zero (the run did happen) and never guessed larger.

The thread id comes from the run itself (``codex_agent.CodexResult.thread_id``, recorded by agent-route in
``north_star_units.jsonl``). Rows written before that field existed are matched by time instead
(:func:`thread_for_run`): the one top-level ``codex exec`` rollout that started when the run did, or none.

STDLIB ONLY and read-only: it never writes under ``$CODEX_HOME``. Meant for the detached refresher, not for a hook
or the per-tick renderer: it lists and reads rollout files.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

_THREAD_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_NAME = re.compile(
    r"rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl")
#: A run's own rollout starts this long before the delegation row's ``ts - duration_sec`` at the earliest, and
#: this long after it at the latest (the hook measures the duration around the whole subprocess).
START_BEFORE_S = 8.0
START_AFTER_S = 3.0
#: A child thread starts after its parent and before the run ended; this slack covers the clocks.
CHILD_SLACK_S = 5.0


def codex_home() -> Path:
    """``$CODEX_HOME`` or ``~/.codex`` (Codex's own rule)."""
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".codex"


@dataclass(frozen=True)
class RunCount:
    """The responses of one run. ``responses`` is None when none could be counted (see the module docstring)."""
    responses: int | None
    threads: int = 0
    how: str = "none"          # "rollout" | "token_count" | "none"


def _file_start(path: Path) -> tuple[float, str] | None:
    """``(local start time as epoch seconds, thread id)`` from a rollout's file name, or None."""
    m = _NAME.fullmatch(path.name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    try:
        return time.mktime((y, mo, d, h, mi, s, 0, 0, -1)), m.group(7).lower()
    except (OverflowError, ValueError):
        return None


class RolloutIndex:
    """The rollout files whose names put them in ``[lo, hi]`` (epoch seconds), listed once.

    Listing is by the day directories the window touches, never the whole tree. ``meta`` reads only the first
    line of a file and is cached."""

    def __init__(self, lo: float, hi: float, home: Path | None = None) -> None:
        self.root = (home or codex_home()) / "sessions"
        self.files: dict[str, tuple[float, Path]] = {}      # thread id -> (start, path)
        self._meta: dict[str, dict | None] = {}
        day = lo - 86400.0                                   # a run that crossed local midnight, or a timezone slip
        while day <= hi + 86400.0:
            t = time.localtime(day)
            folder = self.root / f"{t.tm_year:04d}" / f"{t.tm_mon:02d}" / f"{t.tm_mday:02d}"
            try:
                names = os.listdir(folder)
            except OSError:
                names = []
            for name in names:
                start = _file_start(Path(name))
                if start is not None and lo - 86400.0 <= start[0] <= hi + 86400.0:
                    self.files[start[1]] = (start[0], folder / name)
            day += 86400.0

    def meta(self, thread_id: str) -> dict | None:
        """The ``session_meta`` payload (first line) of a thread's rollout, or None."""
        if thread_id not in self._meta:
            entry = self.files.get(thread_id)
            payload = None
            if entry is not None:
                try:
                    with open(entry[1], "rb") as fh:
                        first = json.loads(fh.readline())
                    if isinstance(first, dict) and isinstance(first.get("payload"), dict):
                        payload = first["payload"]
                except (OSError, ValueError):
                    payload = None
            self._meta[thread_id] = payload
        return self._meta[thread_id]

    def parent_of(self, thread_id: str) -> str | None:
        meta = self.meta(thread_id) or {}
        source = meta.get("source")
        spawn = source.get("subagent", {}).get("thread_spawn") if isinstance(source, dict) \
            and isinstance(source.get("subagent"), dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
        return parent.lower() if isinstance(parent, str) and _THREAD_ID.fullmatch(parent) else None

    def tree(self, thread_id: str, end: float) -> list[Path]:
        """The rollout of ``thread_id`` and of every descendant thread that started by ``end``."""
        thread_id = thread_id.lower()
        if thread_id not in self.files:
            return []
        start = self.files[thread_id][0]
        members = [thread_id]
        known = {thread_id}
        grew = True
        while grew:                                          # a child of a child: repeat until no thread joins
            grew = False
            for tid, (t0, _path) in self.files.items():
                if tid in known or not start - CHILD_SLACK_S <= t0 <= end + CHILD_SLACK_S:
                    continue
                if self.parent_of(tid) in known:
                    known.add(tid)
                    members.append(tid)
                    grew = True
        return [self.files[t][1] for t in members]


def _responses_of(path: Path, ids: set[str]) -> int:
    """Add the ``response_id`` of every ``token_usage_record`` line to ``ids``; return the ``token_count``
    events that carry usage (the fallback count when the file has no ``token_usage_record``)."""
    usage_events = 0
    try:
        with open(path, "rb") as fh:
            for line in fh:
                if b'"token_usage_record"' in line:
                    try:
                        payload = json.loads(line).get("payload")
                    except ValueError:
                        continue
                    rid = payload.get("response_id") if isinstance(payload, dict) else None
                    if isinstance(rid, str) and rid:
                        ids.add(rid)
                elif b'"token_count"' in line and b'"last_token_usage"' in line:   # info:null carries no usage
                    usage_events += 1
    except OSError:
        return 0
    return usage_events


def count_run(index: RolloutIndex, thread_id: str, end: float) -> RunCount:
    """The model responses of the run that opened ``thread_id`` and ended at ``end`` (epoch seconds)."""
    if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
        return RunCount(None)
    files = index.tree(thread_id, end)
    if not files:
        return RunCount(None)
    ids: set[str] = set()
    token_counts = 0
    for path in files:
        token_counts += _responses_of(path, ids)
    if ids:
        return RunCount(len(ids), threads=len(files), how="rollout")
    if token_counts:
        return RunCount(token_counts, threads=len(files), how="token_count")
    return RunCount(None, threads=len(files))


def thread_for_run(index: RolloutIndex, end: float, duration_s: float) -> str | None:
    """The thread of a run recorded without one: the single top-level ``codex exec`` rollout that started when
    the run did (``end - duration_s``). Two candidates, or none, is None: a wrong thread would put another
    run's responses in this session."""
    start = end - duration_s
    found: list[str] = []
    for tid, (t0, _path) in index.files.items():
        if not start - START_BEFORE_S <= t0 <= start + START_AFTER_S:
            continue
        meta = index.meta(tid) or {}
        if meta.get("originator") == "codex_exec" and meta.get("source") == "exec":
            found.append(tid)
    return found[0] if len(found) == 1 else None
