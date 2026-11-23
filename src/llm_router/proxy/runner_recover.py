"""Built-in recovery of the dedicated local runner after a Metal out-of-memory wedge (D-601).

WHY. A wedged runner (``kIOGPUCommandBufferCallbackErrorOutOfMemory``, then ``decode() failed:
Compute error`` on every request) served 0 local calls for hours four times since 2026-11-21 (the
longest 22:24Z-02:54Z) until a human killed its llama-server child and reloaded the model. The
breaker in ``backend_health`` already detects the wedge from the reply (crash_signature,
fast_empty, empty_burst); this module is the repair that ``BackendRecycle`` runs.

WHAT. :class:`RunnerRecover` is awaited by ``BackendRecycle`` (which owns the 1-per-10-min limit,
the in-flight drain and the ledger event). Steps, each fail-closed:
1. read the pid in ``LLM_ROUTER_RUNNER_PIDFILE``; missing or not a pid -> ``skipped``;
2. ``lsof -nP -iTCP:<port> -sTCP:LISTEN``: the listener must be that same pid, else ``skipped``.
   The pid is never taken from the port, only checked against it;
3. SIGTERM only that pid's ``llama-server`` / ``ollama runner`` children (``pgrep -P``, then
   ``ps -o command=`` must name one of them); no child -> ``skipped``;
4. empty ``POST /api/generate`` {model, prompt "", keep_alive -1, num_ctx} reloads the model.
At most ``MAX_PER_HOUR`` attempts per rolling hour; the next one returns ``skipped`` with
``reason=hourly_cap`` and ``alarm=True`` and nothing else is done.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import subprocess
import time
from urllib.parse import urlparse

MAX_PER_HOUR = 3
HOUR_S = 3600.0
RELOAD_TIMEOUT_S = 120.0
CMD_TIMEOUT_S = 10.0


def _is_runner_cmd(command: str) -> bool:
    """True only when the executable (argv0 basename) is llama-server, or ``ollama runner ...``."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    return exe == "llama-server" or (exe == "ollama" and len(argv) > 1 and argv[1] == "runner")


def enabled() -> bool:
    """Kill flag LLM_ROUTER_RUNNER_AUTORECOVER (default on; inert without a pidfile path)."""
    return os.environ.get("LLM_ROUTER_RUNNER_AUTORECOVER", "1").strip().lower() not in {"0", "false", "off", "no"}


def pidfile_path() -> str:
    return os.environ.get("LLM_ROUTER_RUNNER_PIDFILE", "").strip()


async def _run(argv: list[str]) -> str:
    """Run one read-only helper (lsof, pgrep, ps); stdout only, '' on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=CMD_TIMEOUT_S)
        return out.decode("utf-8", "replace")
    except (OSError, ValueError, asyncio.TimeoutError):
        return ""


async def _reload(url: str, body: dict) -> bool:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=RELOAD_TIMEOUT_S) as c:
            r = await c.post(url, json=body)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 - a failed reload is a failed row, not a crash
        return False


class RunnerRecover:
    def __init__(self, pidfile: str, base_url: str, model: str, num_ctx: int | None, *,
                 run=None, kill=None, reload=None, clock=None) -> None:
        self.pidfile = pidfile
        self._base = base_url   # str, or a callable resolved at recovery time
        self.model = model.removeprefix("ollama/")
        self.num_ctx = num_ctx
        self._run = run or _run
        self._kill = kill or os.kill
        self._reload = reload or _reload
        self.clock = clock or time.monotonic
        self.starts: list[float] = []

    async def _snapshot(self, cp: str) -> tuple[str, str] | None:
        out = (await self._run(["ps", "-o", "ppid=,command=", "-p", cp])).strip()
        parts = out.split(None, 1)
        return (parts[0], parts[1]) if len(parts) == 2 else None

    @staticmethod
    def _row(status: str, reason: str, t0: float, clock, **extra) -> dict:
        return {"recycle": status, "runner_recover": status, "reason": reason, "exit_code": None,
                "duration_s": round(max(0.0, clock() - t0), 3), **extra}

    async def __call__(self) -> dict:
        t0 = self.clock()
        base = (self._base() if callable(self._base) else self._base).rstrip("/")
        port = urlparse(base).port or 11434
        self.starts = [t for t in self.starts if t0 - t < HOUR_S]
        if len(self.starts) >= MAX_PER_HOUR:
            return self._row("skipped", "hourly_cap", t0, self.clock, alarm=True)
        try:
            pid = int(open(self.pidfile).read().split()[0])
        except (OSError, ValueError, IndexError):
            return self._row("skipped", "pidfile_missing_or_invalid", t0, self.clock)
        if pid <= 1:
            return self._row("skipped", "pidfile_missing_or_invalid", t0, self.clock)
        listeners = {int(m) for m in re.findall(
            r"^p(\d+)$", await self._run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"]), re.M)}
        if listeners != {pid}:
            return self._row("skipped", "listener_not_pidfile_pid", t0, self.clock)
        children: dict[int, tuple[str, str]] = {}
        for cp in re.findall(r"\d+", await self._run(["pgrep", "-P", str(pid)])):
            snap = await self._snapshot(cp)
            if snap and snap[0] == str(pid) and _is_runner_cmd(snap[1]):
                children[int(cp)] = snap
        if not children:
            return self._row("skipped", "no_runner_child", t0, self.clock)
        if not self.model or not self.num_ctx:   # S5: decided before any signal is sent
            return self._row("skipped", "no_model", t0, self.clock)
        self.starts.append(t0)   # counts only attempts that reach the kill
        try:
            for cp, snap in children.items():
                if await self._snapshot(str(cp)) != snap:   # S2: ppid or command changed since the scan
                    return self._row("skipped", "child_changed", t0, self.clock)
                self._kill(cp, signal.SIGTERM)
        except OSError:
            return self._row("failed", "kill_failed", t0, self.clock)
        body = {"model": self.model, "prompt": "", "keep_alive": -1}
        if self.num_ctx:
            body["options"] = {"num_ctx": self.num_ctx}
        ok = await self._reload(f"{base}/api/generate", body)
        return self._row("ok" if ok else "failed", "reloaded" if ok else "reload_failed", t0, self.clock)
