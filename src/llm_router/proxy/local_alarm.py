"""Local-serving alarm (``LLM_ROUTER_LOCAL_ALARM``, default off).

Local serving can stop without anything failing: every step then goes to Claude and the proxy looks healthy.
This module says so. It is evaluated lazily from the request path, at most once per ``EVAL_EVERY_S``: no
background task, no thread. With the flag off nothing here runs and no file is written.

Alarm codes:

``kill_switch``     the kill-switch file has existed for more than ``KILL_AFTER_S``
``backend_breaker`` the backend health breaker has been open for more than ``BACKEND_AFTER_S``
                    (measured from the first evaluation that saw it open)
``quality_breaker`` the quality breaker is open
``no_local``        in the last ``WINDOW_S`` there were >= ``NO_LOCAL_MIN`` eligible candidate steps and none was
                    served local

Files, all under the router home, mode 0600:
``local_alarm.json``  ``{ts, active: [{code, since}], silenced}``, written atomically
``proxy_alarms.jsonl`` one line per transition: ``{ts, code, event}`` (event ``raise`` | ``clear``)
``local_alarm_maintenance.json``  optional ``{"maintenance_until": <epoch s>}``: alarms are still computed, but
                                  ``silenced`` is true until then. Written by the owner, only read here.

Nothing here may fail or block a request: callers use :meth:`LocalAlarm.maybe_evaluate`, which swallows every
exception and counts it in ``errors``.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Callable

ALARM_NAME = "local_alarm.json"
LOG_NAME = "proxy_alarms.jsonl"
MAINTENANCE_NAME = "local_alarm_maintenance.json"

KILL_SWITCH = "kill_switch"
BACKEND_BREAKER = "backend_breaker"
QUALITY_BREAKER = "quality_breaker"
NO_LOCAL = "no_local"

EVAL_EVERY_S = 300.0
KILL_AFTER_S = 30 * 60.0
BACKEND_AFTER_S = 15 * 60.0
WINDOW_S = 60 * 60.0
NO_LOCAL_MIN = 20
_MAX_STEPS = 20000  # memory bound on the step window


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_LOCAL_ALARM", "").strip().lower() in ("1", "true", "yes", "on")


def _home() -> Path:
    from llm_router import paths

    return paths.llm_router_home()


def _write_0600(path: Path, text: str, *, append: bool) -> None:
    if append:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, text.encode())
        finally:
            os.close(fd)
        return
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


class LocalAlarm:
    def __init__(self, home: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.home = home
        self.clock = clock
        self.steps: deque[tuple[float, bool]] = deque()  # (ts, served) of eligible candidate steps
        self.since: dict[str, float] = {}  # active code -> when it started
        self._backend_seen_open: float | None = None
        self._next_eval = 0.0
        self.errors = 0

    def _dir(self) -> Path:
        return self.home if self.home is not None else _home()

    def note_step(self, candidate: bool, served: bool) -> None:
        """One step that reached the local decision. Only eligible candidates count toward ``no_local``."""
        if not candidate:
            return
        self.steps.append((self.clock(), bool(served)))
        while len(self.steps) > _MAX_STEPS:
            self.steps.popleft()

    def _conditions(self, now: float, kill_path: Path | None, backend_open: bool, quality_open: bool) -> dict[str, float]:
        """Active codes -> since."""
        active: dict[str, float] = {}
        if kill_path is not None:
            try:
                born = kill_path.stat().st_mtime
                if now - born > KILL_AFTER_S:
                    active[KILL_SWITCH] = born
            except OSError:
                pass
        if backend_open:
            if self._backend_seen_open is None:
                self._backend_seen_open = now
            if now - self._backend_seen_open > BACKEND_AFTER_S:
                active[BACKEND_BREAKER] = self._backend_seen_open
        else:
            self._backend_seen_open = None
        if quality_open:
            active[QUALITY_BREAKER] = self.since.get(QUALITY_BREAKER, now)
        while self.steps and now - self.steps[0][0] > WINDOW_S:
            self.steps.popleft()
        if len(self.steps) >= NO_LOCAL_MIN and not any(s for _, s in self.steps):
            active[NO_LOCAL] = self.since.get(NO_LOCAL, now)
        return active

    def _silenced(self, now: float) -> bool:
        try:
            data = json.loads((self._dir() / MAINTENANCE_NAME).read_text())
            return float(data["maintenance_until"]) > now
        except Exception:  # noqa: BLE001 - absent or unreadable means not in maintenance
            return False

    def evaluate(self, *, kill_path: Path | None, backend_open: bool, quality_open: bool) -> list[str]:
        """Compute, write ``local_alarm.json``, log transitions. Returns the active codes."""
        now = self.clock()
        active = self._conditions(now, kill_path, backend_open, quality_open)
        home = self._dir()
        home.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"ts": now, "code": c, "event": "raise"}) for c in active if c not in self.since]
        lines += [json.dumps({"ts": now, "code": c, "event": "clear"}) for c in self.since if c not in active]
        if lines:
            _write_0600(home / LOG_NAME, "\n".join(lines) + "\n", append=True)
        self.since = active
        state = {"ts": now, "active": [{"code": c, "since": s} for c, s in active.items()],
                 "silenced": self._silenced(now)}
        _write_0600(home / ALARM_NAME, json.dumps(state), append=False)
        return list(active)

    def due(self) -> bool:
        return self.clock() >= self._next_eval

    def maybe_evaluate(self, kill_path: Path | None, backend_open: bool, quality_open: bool) -> bool:
        """At most once per ``EVAL_EVERY_S``; never raises. True when an evaluation ran."""
        try:
            now = self.clock()
            if now < self._next_eval:
                return False
            self._next_eval = now + EVAL_EVERY_S
            self.evaluate(kill_path=kill_path, backend_open=backend_open, quality_open=quality_open)
            return True
        except Exception:  # noqa: BLE001 - an alarm never fails a request
            self.errors += 1
            return False
