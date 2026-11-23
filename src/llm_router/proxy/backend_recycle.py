"""Opt-in runner recycle, shared across all local backends in one proxy process.

LLM_ROUTER_BACKEND_RECYCLE_CMD is an argv string, default unset/empty (off).
Only lifecycle metadata reaches the request ledger; command output is discarded.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import time
from contextlib import asynccontextmanager

RATE_LIMIT_S = 600.0
INFLIGHT_WAIT_S = 1.0
COMMAND_TIMEOUT_S = 60.0


def recycle_command() -> str:
    return os.environ.get("LLM_ROUTER_BACKEND_RECYCLE_CMD", "").strip()


class BackendRecycle:
    def __init__(self, command: str, *, clock=None, action=None) -> None:
        self.command = command
        self.action = action   # async callable -> result dict; used when no command is set (runner_recover)
        self.clock = clock or time.monotonic
        self.last_started = None
        self.task = None
        self.closed = False
        self.inflight = 0
        self.ready = asyncio.Event()
        self.ready.set()
        self.idle = asyncio.Event()
        self.idle.set()

    @asynccontextmanager
    async def request(self):
        while True:
            await self.ready.wait()
            if self.ready.is_set():
                break
        self.inflight += 1
        self.idle.clear()
        try:
            yield
        finally:
            self.inflight -= 1
            if self.inflight == 0:
                self.idle.set()

    def start(self, emit) -> dict | None:
        """Schedule at most one owned task; the triggering request never waits."""
        if not (self.command or self.action):
            return None
        if (self.closed or (self.task is not None and not self.task.done())
                or (self.last_started is not None
                    and self.clock() - self.last_started < RATE_LIMIT_S)):
            return {"recycle": "skipped_ratelimit", "exit_code": None, "duration_s": 0.0}
        self.task = asyncio.create_task(self._background(emit))
        return {"recycle": "started", "exit_code": None, "duration_s": 0.0}

    async def _background(self, emit):
        try:
            result = await self.recycle()
        except asyncio.CancelledError:
            self._emit(emit, {"recycle": "failed", "exit_code": None, "duration_s": 0.0})
            raise
        except Exception:  # noqa: BLE001 - a recycle must never break serving
            result = {"recycle": "failed", "exit_code": None, "duration_s": 0.0}
        if result is not None:
            self._emit(emit, result)

    @staticmethod
    def _emit(emit, result):
        try:
            emit(result)
        except Exception:  # noqa: BLE001 - observability cannot strand the owned task
            pass

    async def aclose(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def recycle(self) -> dict | None:
        if not (self.command or self.action):
            return None
        if not self.ready.is_set() or (self.last_started is not None
                and self.clock() - self.last_started < RATE_LIMIT_S):
            return {"recycle": "skipped_ratelimit", "exit_code": None, "duration_s": 0.0}
        # No await between admission closing and the idle check: a new local
        # call cannot race the final check and start while the command runs.
        self.ready.clear()
        try:
            try:
                await asyncio.wait_for(self.idle.wait(), timeout=INFLIGHT_WAIT_S)
            except asyncio.TimeoutError:
                return {"recycle": "skipped_inflight", "exit_code": None, "duration_s": 0.0}
            self.last_started = self.clock()
            if not self.command:
                return await self.action()
            return await self._run()
        finally:
            self.ready.set()

    async def _run(self) -> dict:
        t0 = self.clock()
        code = None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *shlex.split(self.command), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            code = await asyncio.wait_for(process.wait(), timeout=COMMAND_TIMEOUT_S)
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            # Reap on timeout and cancellation before reopening local admission.
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        return {"recycle": "ok" if code == 0 else "failed", "exit_code": code,
                "duration_s": round(max(0.0, self.clock() - t0), 3)}

    def wrap(self, backend):
        return _TrackedBackend(backend, self)


class _TrackedBackend:
    def __init__(self, backend, owner) -> None:
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_owner", owner)

    def __setattr__(self, name, value):
        setattr(self._backend, name, value)

    def __getattr__(self, name):
        value = getattr(self._backend, name)
        if name not in {"complete", "probe", "warm_up"}:
            return value

        async def tracked(*args, **kwargs):
            async with self._owner.request():
                return await value(*args, **kwargs)
        return tracked


def recycle_after_failed_probe(recycler, health_info, emit):
    """Retry a dead runner after a failed cooldown probe, using the shared limit."""
    if recycler is not None and health_info and health_info.get("probe_ok") is False:
        health_info.update(recycler.start(emit) or {})
