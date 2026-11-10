"""P18: background compaction worker for the proxy's local path (PLAN-SUMMARY-SPEED-256K-20261110, row A8).

Flag LLM_ROUTER_COMPACT_BG, default off: while off nothing here runs (``server.py`` only touches this module
behind ``enabled()``). On, one worker per proxy process pre-summarises the OLDEST closable chunks of a local
conversation while the conversation is in a gap, and puts every valid summary in the P10 chunk store under the
key P9 uses (same chunk, prompt version, prompt template, model, num_predict and ``mr_summary.key_salt()``).
The next live turn that goes over the cap then finds those chunks in the store: the P9 path
(``mr_summary.shorten``) runs unchanged and pays 0-1 maps instead of up to N.

Thresholds, as fractions of ``limit`` (the local prompt cap, which P9 triggers on; the cap is derived from the
window): SOFT 0.50 starts pre-summarisation, HARD 0.80 is only counted here (the live turn summarises
synchronously through P9; the worker never does that), TARGET 0.50 is what the projected size (the prompt
with every stored leading chunk replaced by its summary ceiling) is brought down to. After 3 consecutive
refills (the projected size back over SOFT within REFILL_RESET_TURNS live turns of the last completed cycle)
the worker stops for that conversation.

Gaps. Default: user idle only, no new request on the conversation for more than IDLE_S (60 s). Behind
LLM_ROUTER_COMPACT_BG_TOOL_GAPS=1 also the tool-execution gap (the last served reply ended in a tool_use and
the next request has not arrived). One map per gap. Probe 3b (does aborting a map free the single slot
quickly) has NOT been run: that is why tool gaps are a separate, off flag.

Safety. One worker per process, at most one map in flight. The map is issued by the proxy's own
``map_call`` (live model, live num_ctx, P12 ``background`` priority), and the worker polls ``should_yield`` and
its own live-request flag every POLL_S while the map runs, cancelling ONLY the task it started. The ledger
(``compact_worker.jsonl`` in the state dir, rotated at LEDGER_MAX_BYTES, plus ``counts()``) holds counts and
seconds, never text, and one row per worker event (queued map outcome, thrash stop), none per live turn.
"started" is counted when the map task is created, so a map aborted while still waiting in the queue counts
as started. Memory: at most MAX_CONVS conversations are tracked, each idle one dropped after CONV_TTL_S, and a
conversation's request body is released as soon as the worker has evaluated it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from llm_router import failopen, paths
from llm_router.proxy import chunk_store, mr_summary, summary_gate
from llm_router.token_estimate import est_tokens

SOFT = 0.50
HARD = 0.80
TARGET = 0.50
IDLE_S = 60.0
POLL_S = 0.1
TICK_S = 5.0
THRASH_REFILLS = 3
REFILL_RESET_TURNS = 20
LEDGER_NAME = "compact_worker.jsonl"
LEDGER_MAX_BYTES = 1_000_000   # one rotation to <name>.1 above this; the ledger never grows without bound
MAX_CONVS = 64                 # conversations tracked at once (LRU by last activity; live ones are never evicted)
CONV_TTL_S = 6 * 3600.0        # an idle conversation older than this is forgotten

#: ``MapCall(prompt, num_predict) -> reply text or None``; raises on failure. Supplied by the proxy: the live backend, priority "background".
MapCall = Callable[[str, int], Awaitable[str | None]]


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_COMPACT_BG", "").strip().lower() in ("1", "true", "yes", "on")


def tool_gaps_enabled() -> bool:
    return os.environ.get("LLM_ROUTER_COMPACT_BG_TOOL_GAPS", "").strip().lower() in ("1", "true", "yes", "on")


def _has_tool_use(message: dict | None) -> bool:
    content = (message or {}).get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


@dataclass
class _Conv:
    body: dict | None = None
    active: int = 0                 # live requests in flight on this conversation
    last_activity: float = 0.0      # last request start or finish
    tool_gap: bool = False          # last served reply ended in a tool_use, no request since
    mapped_in_gap: bool = False     # the one map of this gap is spent
    settled: bool = False           # last evaluation found nothing to do; reset by any request
    turns: int = 0
    cycle_done: bool = False
    cycle_done_turn: int = 0
    refills: int = 0
    stopped: bool = False


@dataclass
class _Plan:
    est: int
    projected: int
    saved: int
    next_chunk: tuple[list[dict], str, int] | None    # (messages, key, est of the chunk) or None
    gate_skipped: bool = False


class Worker:
    def __init__(self, map_call: MapCall, *, est: Callable[[dict], int], limit_fn: Callable[[], int], model: str,
                 yield_check: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.monotonic,
                 sink: Callable[[dict], None] | None = None, store_path=None, tool_gaps: bool | None = None,
                 idle_s: float = IDLE_S, poll_s: float = POLL_S):
        self.map_call, self.est, self.limit_fn, self.model = map_call, est, limit_fn, model
        self.yield_check, self.clock = yield_check, clock
        self.sink = sink if sink is not None else _file_sink
        self.store_path = store_path
        self._tool_gaps = tool_gaps       # None: read the env flag per tick
        self.idle_s, self.poll_s = idle_s, poll_s
        self.convs: dict[str, _Conv] = {}
        self._busy = False                # one map in flight, ever
        self._inflight: asyncio.Future | None = None
        self._preempt = False
        self.c = dict(queued=0, started=0, aborted=0, completed=0, stored=0, invalid=0, failed=0,
                      thrash_stops=0, hard_crossings=0, live_turns=0, live_maps_paid=0)

    # ------------------------------------------------------------ hooks the proxy calls
    def note_request(self, key: str) -> None:
        """A live local request starts on ``key``: the gap is over, and any map WE started is preempted."""
        st = self.convs.setdefault(key, _Conv())
        st.active += 1
        st.turns += 1
        st.last_activity = self.clock()
        st.tool_gap = False
        st.mapped_in_gap = False
        st.settled = False
        self._evict()
        if self._inflight is not None and not self._inflight.done():
            self._preempt = True

    def note_done(self, key: str, body: dict | None, served: dict | None, maps_paid: int = 0) -> None:
        """The live turn finished. ``served`` None: not served locally, so there is nothing to compact for it."""
        st = self.convs.get(key)
        if st is None:
            return
        st.active = max(0, st.active - 1)
        st.last_activity = self.clock()
        self.c["live_turns"] += 1
        self.c["live_maps_paid"] += maps_paid
        if served is None:
            if st.active == 0:
                self.convs.pop(key, None)
            return
        st.body = body
        st.tool_gap = _has_tool_use(served)
        st.settled = False
        self._evict()

    def _evict(self) -> None:
        """Bound memory: drop idle conversations past the TTL, then the least recently active beyond MAX_CONVS."""
        now = self.clock()
        for k in [k for k, s in self.convs.items() if not s.active and now - s.last_activity > CONV_TTL_S]:
            del self.convs[k]
        idle_keys = sorted((s.last_activity, k) for k, s in self.convs.items() if not s.active)
        while len(self.convs) > MAX_CONVS and idle_keys:
            del self.convs[idle_keys.pop(0)[1]]

    # ------------------------------------------------------------ scheduling
    def _gap_ok(self, st: _Conv) -> bool:
        if st.active or st.body is None or st.stopped or st.mapped_in_gap or st.settled:
            return False
        if self._tool_gaps if self._tool_gaps is not None else tool_gaps_enabled():
            if st.tool_gap:
                return True
        return self.clock() - st.last_activity > self.idle_s

    async def tick(self) -> dict | None:
        """Evaluate the idlest eligible conversation and run at most one map. Returns the event row or None."""
        if self._busy:
            return None
        ready = [(st.last_activity, k) for k, st in self.convs.items() if self._gap_ok(st)]
        if not ready:
            return None
        key = min(ready)[1]
        st = self.convs[key]
        self._busy = True
        try:
            return await self._step(key, st)
        except Exception as exc:  # noqa: BLE001 - an optimisation, never a crash of the proxy
            failopen.record("LR-FO-PROXY-COMPACT-BG", exc)
            return None
        finally:
            st.body = None   # evaluated: the next note_done brings a fresh body; never keep a 256K-token request alive
            self._busy = False

    async def _plan(self, body: dict) -> _Plan:
        store = chunk_store.ChunkStore(self.store_path) if self.store_path else chunk_store.ChunkStore()
        return await asyncio.to_thread(self._plan_sync, body, store)

    def _plan_sync(self, body: dict, store: chunk_store.ChunkStore) -> _Plan:
        messages = body.get("messages") or []
        total = self.est(body)
        if len(messages) < 3:
            return _Plan(total, total, 0, None)
        hist = messages[1:]
        units = mr_summary.units_of(hist)
        uests = [est_tokens(hist[a:b]) for a, b in units]
        closable = [c for c in mr_summary.partition(uests, mr_summary.chunk_target())[:-1]
                    if c[1] < len(units) - mr_summary.W_CLOSE]
        np_, salt = mr_summary.map_num_predict(), mr_summary.key_salt()
        saved, leading, nxt, gate_skipped = 0, True, None, False
        for c in closable:
            chunk = messages[units[c[0]][0] + 1:units[c[1]][1] + 1]
            try:
                key = chunk_store.chunk_key(chunk, prompt_version=mr_summary.PROMPT_VERSION,
                                            prompt_template=mr_summary.MAP_PROMPT, model=self.model,
                                            num_predict=np_, salt=salt)
            except Exception as exc:  # noqa: BLE001
                failopen.record("LR-FO-PROXY-COMPACT-BG", exc, detail="key")
                break
            if key in store.entries:
                if leading:
                    saved += c[2] - mr_summary.SUMMARY_CEIL
                continue
            leading = False
            if nxt is None:
                if summary_gate.enabled() and summary_gate.over_budget(summary_gate.extract(mr_summary.slice_text(chunk))):
                    gate_skipped = True   # P9 would keep this chunk verbatim; a map here would be wasted
                    break
                nxt = (chunk, key, c[2])
        return _Plan(total, total - saved, saved, nxt, gate_skipped)

    async def _step(self, key: str, st: _Conv) -> dict | None:
        limit = self.limit_fn()
        plan = await self._plan(st.body)
        soft, hard, target = int(limit * SOFT), int(limit * HARD), int(limit * TARGET)
        if plan.est >= hard:
            self.c["hard_crossings"] += 1   # the live turn pays through P9; counted, never handled here
        if plan.projected < soft:
            st.settled = True
            return None
        if st.cycle_done:                   # the projected size is back over SOFT after a finished cycle: a refill
            st.cycle_done = False
            st.refills = st.refills + 1 if st.turns - st.cycle_done_turn < REFILL_RESET_TURNS else 1
            if st.refills >= THRASH_REFILLS:
                st.stopped = True
                self.c["thrash_stops"] += 1
                return self._emit("thrash_stop", refills=st.refills)
        if plan.projected <= target or plan.next_chunk is None:
            st.settled = True
            if plan.projected <= target:
                st.cycle_done, st.cycle_done_turn = True, st.turns
            return None
        chunk, skey, chunk_est = plan.next_chunk
        st.mapped_in_gap = True             # one queued map per gap, spent whatever its outcome
        self.c["queued"] += 1
        if self._preempt_pending():
            self._preempt = False
            self.c["aborted"] += 1
            return self._emit("aborted", before_start=True)
        t0 = self.clock()
        outcome = await self._run_map(chunk)
        row = self._finish(outcome, skey, chunk, t0)
        if row["outcome"] == "stored" and plan.projected - (chunk_est - mr_summary.SUMMARY_CEIL) <= target:
            st.cycle_done, st.cycle_done_turn = True, st.turns
        return row

    # ------------------------------------------------------------ one map
    def _preempt_pending(self) -> bool:
        return self._preempt or any(s.active for s in self.convs.values()) or bool(self.yield_check())

    async def _run_map(self, chunk: list[dict]) -> tuple[str, str | None]:
        """('completed', reply) | ('aborted', None) | ('failed', None). Polls the yield signal the whole time."""
        self._preempt = False
        prompt, np_ = mr_summary.map_prompt(chunk), mr_summary.map_num_predict()
        self.c["started"] += 1
        task = asyncio.ensure_future(self.map_call(prompt, np_))
        self._inflight = task
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.poll_s)
                if done:
                    break
                if self._preempt_pending():
                    task.cancel()          # only the task this worker started
                    await asyncio.wait({task})
                    return "aborted", None
            if task.cancelled():
                return "aborted", None
            if task.exception() is not None:
                failopen.record("LR-FO-PROXY-COMPACT-BG", task.exception(), detail="map")
                return "failed", None
            return "completed", task.result()
        finally:
            if not task.done():
                task.cancel()
            self._inflight = None
            self._preempt = False

    def _finish(self, outcome: tuple[str, str | None], skey: str, chunk: list[dict], t0: float) -> dict:
        kind, reply = outcome
        secs = round(self.clock() - t0, 3)
        if kind == "aborted":
            self.c["aborted"] += 1
            return self._emit("aborted", map_s=secs)
        if kind == "failed" or reply is None:
            self.c["failed"] += 1
            return self._emit("failed", map_s=secs)
        self.c["completed"] += 1
        reply = reply.strip()
        if not mr_summary.valid_summary(reply):
            self.c["invalid"] += 1         # not stored: P9 maps it live (or falls back to extractive)
            return self._emit("invalid", map_s=secs)
        text = reply if est_tokens(reply) <= mr_summary.BODY_EST else mr_summary._head_by_est(reply, mr_summary.BODY_EST)
        if not text.startswith(mr_summary.HEADINGS[0]):
            self.c["invalid"] += 1
            return self._emit("invalid", map_s=secs)
        store = chunk_store.ChunkStore(self.store_path) if self.store_path else chunk_store.ChunkStore()
        store.put(skey, text)
        store.flush()
        self.c["stored"] += 1
        return self._emit("stored", map_s=secs)

    # ------------------------------------------------------------ ledger
    def _emit(self, outcome: str, **fields) -> dict:
        row = {"ts": round(time.time(), 3), "outcome": outcome, **fields, "counts": dict(self.c)}
        try:
            self.sink(row)
        except Exception as exc:  # noqa: BLE001
            failopen.record("LR-FO-PROXY-COMPACT-BG", exc, detail="ledger")
        return row

    def counts(self) -> dict:
        return dict(self.c)

    async def run(self) -> None:
        """The proxy's background loop: tick every TICK_S until cancelled."""
        while True:
            await asyncio.sleep(TICK_S)
            await self.tick()


def _file_sink(row: dict) -> None:
    path = paths.state_path(LEDGER_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size > LEDGER_MAX_BYTES:
            os.replace(path, path.with_name(LEDGER_NAME + ".1"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


_WORKER: Worker | None = None


def install(worker: Worker) -> Worker:
    """The one worker of this process (the proxy builds it once)."""
    global _WORKER
    _WORKER = worker
    return worker


def get() -> Worker | None:
    return _WORKER


def clear() -> None:
    """Forget the installed worker (proxy shutdown, or a restart that does not build one)."""
    global _WORKER
    _WORKER = None


__all__ = ["Worker", "enabled", "tool_gaps_enabled", "install", "get", "clear"]
