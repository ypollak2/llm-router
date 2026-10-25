"""Lane A breaker and failure feed (PR2; plan W1-4, D-285, D-292, D-293).

Lane A serves continuation steps on Haiku. Its safety case is this breaker plus the Frontier shadow
pairs of PR3, not the classifier's confidence. State machine (lever ``proxy_haiku``, task ``lane_a``):

``closed``     lane A may serve. Opens on EITHER trigger:
               * ``failures``: ``trip_failures`` (5) hard failures of Haiku-served lane A steps inside
                 ``window_s`` (10 min). Hard failure = HTTP 4xx/5xx/429, a timeout or stream error, an
                 empty or truncated reply, a tool call whose arguments do not parse.
               * ``redo_rate``: of at least ``redo_min_n`` (30) settled served steps in the last
                 ``redo_window_days`` (7), more than ``redo_max`` (15%) were redone (a tool call
                 re-issued within two calls: ``local_gate.FailureFeed``). Prereg section 10.
``open``       lane A does not serve (reason ``breaker_open_reserved``), steps stay on the pinned
               model. After ``cooldown_s`` (30 min; ``quality_cooldown_s``, 24 h, after a redo-rate trip)
               the next ``admit`` moves it to half-open.
``half_open``  exactly one step is admitted (the probe, ``TierDecision.detail == "lane_a_probe"``).
               Others stay pinned until the probe settles, or until ``probe_timeout_s`` passes with no
               outcome (the next step is then admitted as the new probe). A probe that succeeds closes
               the breaker (the failure window and the redo units are cleared: the evidence that opened it
               cannot reopen it); a probe that fails reopens it and restarts the cooldown.

The decision path is in memory only: ``admit`` and ``record_*`` never touch the disk. The state
(``lane_a_breaker.json``, 0600 through ``state_files``) is loaded once on the first ``admit`` in a
worker thread and written by a coalesced background flush task in a worker thread, so a restart keeps an
open breaker and nothing blocks the event loop. A load or write error is recorded with ``failopen`` and
never raises. An unreadable state file reads closed; ``admit`` itself returns "not allowed" on any
internal error, so a breaker that cannot decide does not serve. The breaker is built only when
``lane_a.enabled`` is true in the YAML; a lane that is off creates no object, no file and no task.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Callable

from llm_router import failopen, paths, state_files
from llm_router.proxy import lane_a
from llm_router.proxy.local_gate import FailureFeed

STATE_FILE = "lane_a_breaker.json"
KEY = "proxy_haiku:lane_a"
CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
_MAX_UNITS = 500

# Failure kinds written to the breaker state (closed set; text-free).
F_TIMEOUT = "timeout"
F_STREAM = "stream_error"
F_EMPTY = "empty"
F_TRUNCATED = "truncated"
F_TOOL_PARSE = "tool_parse_error"
F_UNPARSEABLE = "unparseable"


def state_path() -> Path:
    return paths.state_path(STATE_FILE)


class LaneABreaker:
    """In-memory breaker with an asynchronous, coalesced flush. ``clock`` is wall time (persisted)."""

    def __init__(self, cfg: lane_a.BreakerConfig | None = None, path: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg or lane_a.BreakerConfig()
        self.path = path
        self.clock = clock
        self.state = CLOSED
        self.opened_at: float | None = None
        self.cooldown_s = self.cfg.cooldown_s
        self.reason: str | None = None
        self.probe_at: float | None = None
        self.failures: list[float] = []
        self.units: list[tuple[float, bool]] = []  # (ts, redo)
        self.events: list[dict] = []  # text-free: {ts, event, reason}, newest last, bounded
        self._loaded = False
        self._load_lock: asyncio.Lock | None = None
        self._flush_task: asyncio.Task | None = None
        self._dirty = False

    # ── persistence (worker threads only) ───────────────────────────────────

    def _file(self) -> Path:
        return self.path or state_path()

    def _read(self) -> dict | None:
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            failopen.record("LR-FO-LANE-A-BREAKER-READ", exc)
            return None
        return raw if isinstance(raw, dict) and raw.get("key") == KEY else None

    def _apply(self, raw: dict) -> None:
        st = raw.get("state")
        if st in (OPEN, HALF_OPEN):
            self.state = st
        num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
        self.opened_at = num(raw.get("opened_at"))
        cd = num(raw.get("cooldown_s"))
        self.cooldown_s = cd if cd is not None and cd > 0 else self.cfg.cooldown_s
        self.reason = raw.get("reason") if isinstance(raw.get("reason"), str) else None
        self.probe_at = None  # a probe in flight before a restart is gone
        self.failures = [t for t in (num(x) for x in raw.get("failures") or []) if t is not None]
        self.units = [(t, bool(r)) for t, r in ((num(u[0]), u[1]) for u in raw.get("units") or []
                                                 if isinstance(u, (list, tuple)) and len(u) == 2)
                      if t is not None][-_MAX_UNITS:]
        if self.state in (OPEN, HALF_OPEN) and self.opened_at is None:
            self.opened_at = self.clock()

    def _snapshot(self) -> dict:
        return {"version": 1, "key": KEY, "state": self.state, "opened_at": self.opened_at,
                "cooldown_s": self.cooldown_s, "reason": self.reason, "failures": list(self.failures),
                "units": [[t, r] for t, r in self.units[-_MAX_UNITS:]], "events": self.events[-50:]}

    def _write(self, snap: dict) -> None:
        p = self._file()
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        try:
            with state_files.open_private(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snap, sort_keys=True))
            os.replace(tmp, p)
        except OSError as exc:
            failopen.record("LR-FO-LANE-A-BREAKER-WRITE", exc)
            try:
                tmp.unlink()
            except OSError:
                pass

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        async with self._load_lock:
            if self._loaded:
                return
            raw = await asyncio.to_thread(self._read)
            if raw is not None and not self._loaded:
                self._apply(raw)
            self._loaded = True

    def _mark_dirty(self) -> None:
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): ``flush`` writes it
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while self._dirty:
            self._dirty = False
            await asyncio.to_thread(self._write, self._snapshot())

    async def flush(self) -> None:
        """Write now and wait (shutdown, tests)."""
        if self._flush_task is not None and not self._flush_task.done():
            await self._flush_task
        self._dirty = False
        await asyncio.to_thread(self._write, self._snapshot())

    # ── transitions (in memory) ─────────────────────────────────────────────

    def _event(self, name: str, reason: str | None = None) -> None:
        self.events.append({"ts": round(self.clock(), 3), "event": name, "reason": reason})
        del self.events[:-50]

    def _open(self, reason: str, cooldown_s: float) -> None:
        self.state, self.opened_at, self.cooldown_s, self.reason = OPEN, self.clock(), cooldown_s, reason
        self.probe_at = None
        self._event("open", reason)
        self._mark_dirty()

    def _close(self) -> None:
        self.state, self.opened_at, self.reason, self.probe_at = CLOSED, None, None, None
        self.failures.clear()
        self.units.clear()
        self._event("closed", "probe_passed")
        self._mark_dirty()

    async def admit(self) -> tuple[bool, bool]:
        """``(allowed, is_probe)``. Never raises; an internal error is not allowed."""
        try:
            await self._ensure_loaded()
            now = self.clock()
            if self.state == OPEN:
                if self.opened_at is not None and now - self.opened_at >= self.cooldown_s:
                    self.state, self.probe_at = HALF_OPEN, None
                    self._event("half_open", "cooldown_elapsed")
                    self._mark_dirty()
                else:
                    return False, False
            if self.state == HALF_OPEN:
                if self.probe_at is not None and now - self.probe_at < self.cfg.probe_timeout_s:
                    return False, False
                self.probe_at = now
                return True, True
            return True, False
        except Exception as exc:  # noqa: BLE001 - a breaker that cannot decide does not serve
            failopen.record("LR-FO-LANE-A-BREAKER-ADMIT", exc)
            return False, False

    def record_success(self, probe: bool = False) -> None:
        if probe and self.state == HALF_OPEN:
            self._close()

    def record_failure(self, kind: str, probe: bool = False) -> None:
        """A hard failure of a Haiku-served lane A step."""
        now = self.clock()
        if probe and self.state == HALF_OPEN:
            self.failures.append(now)
            self._open(f"probe_failed:{kind}", self.cooldown_s)
            return
        if self.state != CLOSED:
            return  # a late outcome from before the trip: already open
        self.failures = [t for t in self.failures if now - t <= self.cfg.window_s] + [now]
        if len(self.failures) >= self.cfg.trip_failures:
            self._open(f"failures:{len(self.failures)} in {int(self.cfg.window_s)}s (last {kind})",
                       self.cfg.cooldown_s)
        else:
            self._mark_dirty()

    def record_unit(self, redo: bool) -> None:
        """A settled served step: redone (a tool call re-issued within two calls) or used."""
        now = self.clock()
        horizon = self.cfg.redo_window_days * 86400.0
        self.units = [(t, r) for t, r in self.units if now - t <= horizon][-(_MAX_UNITS - 1):] + [(now, redo)]
        if self.state == CLOSED and len(self.units) >= self.cfg.redo_min_n:
            k = sum(1 for _, r in self.units if r)
            if k / len(self.units) > self.cfg.redo_max:
                self._open(f"redo_rate:{k}/{len(self.units)}", self.cfg.quality_cooldown_s)
                return
        self._mark_dirty()


# ── reading a reply ─────────────────────────────────────────────────────────


def _sse_events(buf: bytes):
    for line in buf.decode("utf-8", "replace").splitlines():
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(ev, dict):
                yield ev


def read_reply(status: int, buf: bytes, ctype: str) -> tuple[dict | None, str | None]:
    """``(message, failure)``: the assistant message the reply carried (content blocks only, tool
    inputs parsed) and the hard-failure kind, or None. Text is never returned in a failure kind."""
    if status >= 400:
        return None, f"http_{status}"
    content: list[dict] = []
    stop: str | None = None
    if "event-stream" in (ctype or ""):
        blocks: dict[int, dict] = {}
        partial: dict[int, list[str]] = {}
        done = False
        for ev in _sse_events(buf):
            t = ev.get("type")
            if t == "error":
                return None, F_STREAM
            if t == "content_block_start" and isinstance(ev.get("content_block"), dict):
                blocks[ev.get("index", len(blocks))] = dict(ev["content_block"])
            elif t == "content_block_delta" and isinstance(ev.get("delta"), dict):
                i, d = ev.get("index"), ev["delta"]
                if d.get("type") == "text_delta" and i in blocks:
                    blocks[i]["text"] = (blocks[i].get("text") or "") + str(d.get("text") or "")
                elif d.get("type") == "input_json_delta":
                    partial.setdefault(i, []).append(str(d.get("partial_json") or ""))
            elif t == "message_delta":
                stop = (ev.get("delta") or {}).get("stop_reason", stop)
            elif t == "message_stop":
                done = True
        if not done:  # Anthropic always ends a stream with message_stop
            return None, F_TRUNCATED
        for i in sorted(k for k in blocks if isinstance(k, int)):
            b = blocks[i]
            if b.get("type") == "tool_use":
                txt = "".join(partial.get(i, [])) or (json.dumps(b["input"]) if b.get("input") else "")
                try:
                    b["input"] = json.loads(txt) if txt else {}
                except ValueError:
                    return None, F_TOOL_PARSE
            content.append(b)
    else:
        try:
            data = json.loads(buf or b"")
        except ValueError:
            return None, F_UNPARSEABLE
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            return None, F_UNPARSEABLE
        content = [b for b in data["content"] if isinstance(b, dict)]
        stop = data.get("stop_reason")
    tools = [b for b in content if b.get("type") == "tool_use"]
    if any(not isinstance(b.get("input"), dict) for b in tools):
        return None, F_TOOL_PARSE
    if stop == "tool_use" and not tools:
        return None, F_TOOL_PARSE
    if stop == "max_tokens":
        return None, F_TRUNCATED
    if not tools and not any((b.get("text") or "").strip() for b in content if b.get("type") == "text"):
        return None, F_EMPTY
    return {"role": "assistant", "content": content}, None


# ── the feed ─────────────────────────────────────────────────────────────────


class LaneAFeed(FailureFeed):
    """``local_gate.FailureFeed`` whose settled outcomes go to the breaker in memory (the parent
    appends a unit to a file, which would block the loop)."""

    def __init__(self, breaker: LaneABreaker) -> None:
        super().__init__(None)
        self.breaker = breaker

    def _emit(self, pend, outcome: str) -> None:
        self.breaker.record_unit(outcome == "redo")


class LaneARuntime:
    """What the proxy holds when ``lane_a.enabled`` is true: the breaker and the feed."""

    def __init__(self, cfg: lane_a.BreakerConfig, path: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.breaker = LaneABreaker(cfg, path, clock)
        self.feed = LaneAFeed(self.breaker)

    def observe(self, body: dict, session_id: str | None) -> None:
        try:
            self.feed.observe(body, session_id)
        except Exception as exc:  # noqa: BLE001 - the feed never costs a request
            failopen.record("LR-FO-LANE-A-FEED-OBSERVE", exc)

    def failure(self, kind: str, probe: bool) -> None:
        try:
            self.breaker.record_failure(kind, probe)
        except Exception as exc:  # noqa: BLE001
            failopen.record("LR-FO-LANE-A-FEED-FAILURE", exc)

    def reply(self, status: int, buf: bytes, ctype: str, *, session_id: str | None, model: str | None,
              session_kind: str | None, probe: bool) -> str | None:
        """Settle one Haiku-served reply. Returns the failure kind, or None."""
        try:
            message, kind = read_reply(status, buf, ctype)
            if kind is not None:
                self.breaker.record_failure(kind, probe)
                return kind
            self.breaker.record_success(probe)
            if message is not None and model:
                self.feed.note_served(session_id, "lane_a", model, message, session_kind)
            return None
        except Exception as exc:  # noqa: BLE001
            failopen.record("LR-FO-LANE-A-FEED-REPLY", exc)
            return None


class ServedStep:
    """One lane A step sent to Haiku: the callbacks ``server.forward`` makes about its outcome."""

    def __init__(self, runtime: LaneARuntime, *, probe: bool, session_id: str | None, model: str | None,
                 session_kind: str | None) -> None:
        self.rt, self.probe, self.session_id, self.model, self.session_kind = (
            runtime, probe, session_id, model, session_kind)

    def failure(self, kind: str) -> None:
        self.rt.failure(kind, self.probe)

    def reply(self, status: int, buf: bytes, ctype: str) -> str | None:
        return self.rt.reply(status, buf, ctype, session_id=self.session_id, model=self.model,
                             session_kind=self.session_kind, probe=self.probe)


__all__ = ["LaneABreaker", "LaneAFeed", "LaneARuntime", "ServedStep", "read_reply", "state_path"]
