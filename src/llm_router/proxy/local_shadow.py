"""Opt-in SHADOW mode for the proxy's local agent (local-usage plan P3).

With ``--shadow on`` every step is served by the normal upstream exactly as without
the proxy. In parallel, the local model answers the same step on a deep COPY of the
request and its tool-call choice is compared with Claude's. One ``local_shadow``
record per step goes to ``proxy_local_shadow.jsonl`` (a file of its own, so
``proxy_calls.jsonl`` and everything that reads it, NS / D1 / D2 included, never
sees it).

Guarantees, each pinned by a test in ``tests/test_proxy_local_shadow.py``:

* Latency isolation: the shadow job is a detached task that is started before
  Claude's reply is relayed but is never awaited by the relay path; the relay only
  sets a future after the last byte left. When Claude's reply is complete and the
  local job is still running, the job is cancelled (``dropped_claude_first``).
  Local also has its own wall budget (``budget_exceeded``).
* Bounded concurrency of 1: a second job while one runs is not queued, it is
  recorded as ``skipped_busy`` (one model resident, so the memory gate holds).
* Local never sees or writes the user's tree: no tool is executed, no file is read
  (no repo-knowledge attach, no post-apply check), the request is a deep copy.
* Reason codes only: a record holds ids, booleans, counts, seconds and a reason
  code from ``REASONS``. Never a prompt, a tool name, a tool argument or a reply.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

ENV_SHADOW = "LLM_ROUTER_PROXY_LOCAL_SHADOW"
SHADOW_NAME = "proxy_local_shadow.jsonl"
KIND = "local_shadow"
#: Wall budget of one local job (plan 5.1: p90 step latency <= 6 s on a fast model).
DEFAULT_BUDGET_S = 20.0
#: A request body this large (bytes of JSON) is over the 25k-token prompt cap whatever it holds:
#: 25k tokens x 3.6 chars/token is 90 KB, and 4x that leaves a wide margin for the fields
#: conversion drops. Decided from the length alone, so a multi-MB body costs the relay path
#: (and the loop) nothing: no copy, no conversion, no estimate.
RAW_BYTES_CAP = 400_000
#: Longest the job waits for Claude's reply after local finished first.
CLAUDE_WAIT_S = 900.0

# Reason codes (the ``fallback_reason`` of a record; None when local produced a comparable reply).
R_BUSY = "skipped_busy"
R_DROPPED = "dropped_claude_first"
R_BUDGET = "budget_exceeded"
R_BACKEND = "backend_error"
R_SCHEMA = "schema_invalid"
R_NOT_ELIGIBLE = "not_eligible"
R_CLAUDE_NOT_OK = "claude_not_ok"
R_NO_CLAUDE_REPLY = "no_claude_reply"
R_ERROR = "shadow_error"
REASONS = frozenset({R_BUSY, R_DROPPED, R_BUDGET, R_BACKEND, R_SCHEMA, R_NOT_ELIGIBLE, R_CLAUDE_NOT_OK,
                     R_NO_CLAUDE_REPLY, R_ERROR, "media_present", "prompt_over_cap", "backend_unhealthy",
                     "kill_switch", "breaker_open", "hedge_timeout", "local_context_overflow"})


def parse_on_off(raw: str | None) -> bool:
    value = str(raw or "").strip().lower()
    if value in ("1", "on", "true", "yes"):
        return True
    if value in ("", "0", "off", "false", "no"):
        return False
    raise ValueError(f"expected on/off, got {raw!r}")


def shadow_path() -> Path:
    from llm_router import paths

    return paths.state_path(SHADOW_NAME)


def write_record(rec: dict, path: Path | None = None) -> None:
    target = path or shadow_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError as exc:
        from llm_router import failopen

        failopen.record("LR-FO-PROXY-SHADOW-WRITE", exc)


def read_records(path: Path | None = None, days: float | None = None) -> list[dict]:
    target = path or shadow_path()
    if not target.exists():
        return []
    cutoff = time.time() - days * 86400 if days else None
    out: list[dict] = []
    try:
        with target.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") == KIND and (
                        cutoff is None or (isinstance(rec.get("ts"), (int, float)) and rec["ts"] >= cutoff)):
                    out.append(rec)
    except OSError:
        return []
    return out


# ── comparing two replies ────────────────────────────────────────────────────


def _norm(value):
    """Cheap argument equivalence: key order and surrounding whitespace do not matter."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, str):
        return value.strip()
    return value


def calls_of_message(message: dict | None) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for block in (message or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            inp = block.get("input")
            out.append((str(block.get("name")), inp if isinstance(inp, dict) else {}))
    return out


def calls_of_reply(buf: bytes, ctype: str) -> list[tuple[str, dict]] | None:
    """Tool calls in Anthropic's reply (JSON body or SSE stream); ``None`` if unreadable."""
    try:
        if "event-stream" not in ctype:
            data = json.loads(buf or b"{}")
            return calls_of_message(data) if isinstance(data, dict) else None
        blocks: dict[int, dict] = {}
        for raw in buf.decode("utf-8", "replace").splitlines():
            if not raw.startswith("data:"):
                continue
            try:
                ev = json.loads(raw[5:].strip())
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            idx = ev.get("index")
            if ev.get("type") == "content_block_start":
                cb = ev.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    blocks[idx] = {"name": cb.get("name"), "json": "", "input": cb.get("input") or {}}
            elif ev.get("type") == "content_block_delta" and idx in blocks:
                d = ev.get("delta") or {}
                if d.get("type") == "input_json_delta":
                    blocks[idx]["json"] += d.get("partial_json") or ""
        out = []
        for idx in sorted(blocks):
            b = blocks[idx]
            try:
                inp = json.loads(b["json"]) if b["json"] else b["input"]
            except ValueError:
                inp = {}
            out.append((str(b["name"]), inp if isinstance(inp, dict) else {}))
        return out
    except Exception:  # noqa: BLE001 - an unreadable reply is a recorded reason, never an error
        return None


def compare(claude: list[tuple[str, dict]], local: list[tuple[str, dict]]) -> tuple[bool, bool | None]:
    """``(same tool names in the same order, args equivalent | None when names differ)``.
    Two text-only replies (no tool call) agree."""
    if [n for n, _ in claude] != [n for n, _ in local]:
        return False, None
    return True, all(_norm(a) == _norm(b) for (_, a), (_, b) in zip(claude, local))


def step_id(session_id: str | None, ts: float, msg_id: str | None) -> str:
    """Opaque id that joins a record to its ledger row: msg_id when Claude's reply had one."""
    if msg_id:
        return str(msg_id)
    return hashlib.sha1(f"{session_id}|{ts}".encode()).hexdigest()[:16]


# ── the runner ───────────────────────────────────────────────────────────────

#: ``local_call(body_copy) -> (message | None, error | None, reason_code | None)``.
LocalCall = Callable[[dict], Awaitable[tuple[dict | None, str | None, str | None]]]


class ShadowRunner:
    """Detached local jobs, at most one at a time."""

    def __init__(self, local_call: LocalCall, *, budget_s: float = DEFAULT_BUDGET_S,
                 path: Path | None = None, claude_wait_s: float = CLAUDE_WAIT_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._local_call = local_call
        self.budget_s = budget_s
        self.path = path
        self.claude_wait_s = claude_wait_s
        self._clock = clock
        self._busy = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(self, body: dict, *, session_id: str | None, ts: float,
               claude_reply: "asyncio.Future[dict]", eligible_reason: str | None = None) -> None:
        """Start the job and return at once. ``claude_reply`` resolves, when Claude's reply is
        complete, to ``{"ok": bool, "calls": list | None, "msg_id": str | None}``."""
        base = {"kind": KIND, "ts": ts, "session_id": session_id}
        if eligible_reason is not None:
            self._finish(base, claude_reply, reason=eligible_reason)
            return
        if self._busy:
            self._finish(base, claude_reply, reason=R_BUSY)
            return
        self._busy = True
        task = asyncio.get_running_loop().create_task(self._job(body, base, claude_reply))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _finish(self, base: dict, claude_reply, *, reason: str) -> None:
        """A record with no local attempt; written once Claude has answered (never blocks it)."""
        async def _later() -> None:
            await self._write(base, claude_reply, local=None, latency=None, schema_valid=None, reason=reason)

        task = asyncio.get_running_loop().create_task(_later())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, base: dict, claude_reply, *, local: list | None, latency: float | None,
                     schema_valid: bool | None, reason: str | None) -> None:
        claude: dict = {}
        try:
            claude = await asyncio.wait_for(asyncio.shield(claude_reply), self.claude_wait_s)
        except Exception:  # noqa: BLE001 - timeout or cancellation: recorded below
            claude = {}
        rec = dict(base, step_id=step_id(base.get("session_id"), base["ts"], claude.get("msg_id")),
                   agree=None, args_equal=None, local_latency_s=latency, schema_valid=schema_valid,
                   fallback_reason=reason)
        if reason is None:
            if not claude:
                rec["fallback_reason"] = R_NO_CLAUDE_REPLY
            elif not claude.get("ok") or claude.get("calls") is None:
                rec["fallback_reason"] = R_CLAUDE_NOT_OK
            else:
                rec["agree"], rec["args_equal"] = compare(claude["calls"], local or [])
        write_record(rec, self.path)

    async def _job(self, body: dict, base: dict, claude_reply) -> None:
        t0 = self._clock()
        work = None
        local = None
        schema_valid: bool | None = None
        reason: str | None = None
        try:
            body_copy = await asyncio.to_thread(copy.deepcopy, body)   # off the loop: bodies reach MBs
            work = asyncio.ensure_future(self._local_call(body_copy))
            done, _ = await asyncio.wait({work, claude_reply}, timeout=self.budget_s,
                                         return_when=asyncio.FIRST_COMPLETED)
            if work not in done:
                work.cancel()
                try:
                    await work
                except asyncio.CancelledError:
                    # Our own cancel of ``work`` is swallowed; a cancel of THIS job is not (``work``
                    # is cancelled in both cases, so ``work.cancelled()`` cannot tell them apart).
                    me = asyncio.current_task()
                    if me is not None and getattr(me, "cancelling", lambda: 1)() > 0:
                        raise
                except Exception:  # noqa: BLE001 - the local call failed while stopping; reason is known
                    pass
                reason = R_DROPPED if claude_reply in done else R_BUDGET
            else:
                message, err, code = work.result()
                if err or message is None:
                    reason = code or R_BACKEND
                    schema_valid = False if reason == R_SCHEMA else None  # None: no reply to judge
                else:
                    schema_valid = True
                    local = calls_of_message(message)
        except Exception:  # noqa: BLE001 - the shadow job must never surface an error
            reason = R_ERROR
        finally:
            if work is not None and not work.done():
                work.cancel()              # an outer cancel must not leave the local call running
            self._busy = False  # the model slot is free; waiting for Claude holds nothing
        latency = round(self._clock() - t0, 3)
        await self._write(base, claude_reply, local=local, latency=latency, schema_valid=schema_valid,
                          reason=reason)

    async def drain(self) -> None:
        """Wait for every outstanding job (tests, shutdown)."""
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
