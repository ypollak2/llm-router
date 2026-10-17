"""The proxy's local gate: serve a continuation locally ONLY on a qualified lane.

PLAN v16 P2.1 task 5 / P2.10 task 3 groundwork, decisions D-195 and D-196. Default OFF
(``--local-gate on`` / ``LLM_ROUTER_PROXY_LOCAL_GATE=on``). With the gate off nothing here
runs: no file is read, no ledger key is added, ``try_serve`` is byte-identical.

WHY. On 2026-10-17 the live proxy could not serve anything locally whatever P2.0
qualified: nothing under ``proxy/`` read ``local_eligibility.json``. ``try_serve`` picked
a model from the routing policy alone. This module is the missing read side: a step goes
to a local model only when the lane it belongs to is eligible in that file.

THE FILE. ``<state dir>/local_eligibility.json``, shape ``{model: {task_type: {tier: lane}}}``
with ``task_type`` in qa/tech_op/code and ``tier`` in simple/moderate (what P2.0's
``record_competence`` writes; it is not on main yet, so this reads tolerantly). A lane
serves iff ``lane["eligible"] is True`` and ``verdict == "eligible"`` and
``grade == "eligible"``. A missing, empty, unreadable or malformed file forwards
(``eligibility_unreadable``); it never fails open to local. The proxy never writes it.

STEP -> LANE. Only ``step_class == "continuation"`` is considered (turn_first, sub-agent
and harness steps stay on Claude). By the class of the tools the step answers:

  technical_op                       -> tech_op / simple
  web, with policy task_type query|research|analyze, complexity simple|moderate -> qa / <tier>
  web, with policy task_type code, complexity simple|moderate                   -> code / <tier>
  edit, exec, agent, other, none     -> forwarded (``lane_unmapped``)

The qa and code mappings are an assumption (nothing in P2.0 defines which proxy
continuation is a "qa" step); they are the narrowest reading that keeps every step that
may write or run code with Claude. The code lane is wired, but an edit-shaped reply is
never served (``proxy.server`` rejects it as ``edit_to_claude``).

The MODEL is the highest-``n`` eligible entry of the lane (``ollama/<name>``), not the
policy's pick and not ``--model``: a pin never makes an ineligible lane serve.

CHECKS, first failure forwards (reason code in ``row["local_gate"]["reason"]``):

  not_continuation / lane_unmapped   the step has no lane
  eligibility_unreadable             the file cannot be trusted
  ineligible_lane                    no eligible entry for the lane
  breaker_open                       quality_breaker class open, or the breaker raised.
                                     tech_op: techop_local/tech_op. qa, code: proxy_local/<lane>.
                                     half_open lets ONE probe through per step budget.
  promote_gated                      P0.15: the model is not configured or grandfathered
  capability_excluded                F14 capability filter: lacks tools or window
  no_local_model                     local_agent ``can_serve_locally`` says Claude
  backend_unhealthy                  BackendHealth open or the Ollama watchdog says hung
  pack_over_window                   the irreducible tokens do not fit the model's window
  gate_error                         the gate itself raised (the step is forwarded)
  served_local                       every check passed; the step goes to the local model

BREAKER FEED. A served step is followed for its next two tool calls. If either re-issues
a tool call the served reply made (same tool, same file path / pattern / command) the
served step was not enough: one ``redo`` unit. Two calls without that: one ``used``
unit. Units go to ``<state dir>/proxy_local_units.jsonl``, which the breaker reads as its
``units_fn`` for these classes. Whether a re-read really is a failure is an assumption of
D-195; the unit count, not this module, decides when the breaker opens.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from llm_router import failopen, paths

ENV_VAR = "LLM_ROUTER_PROXY_LOCAL_GATE"
ELIGIBILITY_NAME = "local_eligibility.json"
UNITS_NAME = "proxy_local_units.jsonl"

LANE_TECH_OP, LANE_QA, LANE_CODE = "tech_op", "qa", "code"
TIERS = ("simple", "moderate")

from llm_router.quality_breaker import LEVER_PROXY_LOCAL, LEVER_TECHOP_LOCAL as LEVER_TECHOP  # noqa: E402

R_NOT_CONTINUATION = "not_continuation"
R_LANE_UNMAPPED = "lane_unmapped"
R_UNREADABLE = "eligibility_unreadable"
R_INELIGIBLE = "ineligible_lane"
R_BREAKER = "breaker_open"
R_PROMOTE = "promote_gated"
R_CAPABILITY = "capability_excluded"
R_NO_MODEL = "no_local_model"
R_UNHEALTHY = "backend_unhealthy"
R_PACK = "pack_over_window"
R_ERROR = "gate_error"
R_SERVED = "served_local"
R_LOCAL_FAILED = "local_failed"
R_NEEDS_UNKNOWN = "needs_unknown"
R_LOCAL_REPEAT = "local_repeat"
REASONS = (R_NOT_CONTINUATION, R_LANE_UNMAPPED, R_UNREADABLE, R_INELIGIBLE, R_BREAKER, R_PROMOTE,
           R_CAPABILITY, R_NO_MODEL, R_UNHEALTHY, R_PACK, R_ERROR, R_SERVED, R_LOCAL_FAILED,
           R_NEEDS_UNKNOWN, R_LOCAL_REPEAT)

_QA_TASKS = frozenset({"query", "research", "analyze"})
_REREAD_WINDOW = 2          # tool calls after a served step that can count as a re-read
_MAX_PENDING = 256
_UNITS_KEEP = 5000


def parse_gate(raw: str | None) -> bool:
    value = str(raw or "").strip().lower()
    if value in ("1", "on", "true", "yes"):
        return True
    if value in ("", "0", "off", "false", "no"):
        return False
    raise ValueError(f"expected on/off, got {raw!r}")


@dataclass(frozen=True)
class LaneDecision:
    serve: bool
    model: str | None
    reason: str
    lane: str | None = None
    detail: str = ""

    def as_row(self) -> dict:
        row = {"lane": self.lane, "model": self.model, "reason": self.reason}
        if self.detail:
            row["detail"] = self.detail
        return row


# ── eligibility file ─────────────────────────────────────────────────────────

def eligibility_path() -> Path:
    return paths.state_path(ELIGIBILITY_NAME)


def load_eligibility(path: Path | None = None) -> dict | None:
    """The parsed file, or ``None`` when it is missing, empty, unreadable or not an object."""
    try:
        data = json.loads(Path(path or eligibility_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def _n_of(lane: dict) -> int:
    n = lane.get("n")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    counts = [lane.get("n_cand"), lane.get("n_ref")]
    if all(isinstance(c, int) and not isinstance(c, bool) for c in counts):
        return min(counts)
    return 0


def eligible_model(data: dict, lane: str, tier: str) -> str | None:
    """``ollama/<name>`` of the highest-n eligible entry for (lane, tier), else ``None``."""
    best: tuple[int, str] | None = None
    for name, entry in data.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        task = entry.get(lane)
        entry_lane = task.get(tier) if isinstance(task, dict) else None
        if not isinstance(entry_lane, dict):
            continue
        if not (entry_lane.get("eligible") is True and entry_lane.get("verdict") == "eligible"
                and entry_lane.get("grade") == "eligible"):
            continue
        rank = (_n_of(entry_lane), name)
        if best is None or rank[0] > best[0] or (rank[0] == best[0] and rank[1] < best[1]):
            best = rank
    if best is None:
        return None
    name = best[1]
    return name if name.startswith("ollama/") else f"ollama/{name}"


# ── step -> lane ─────────────────────────────────────────────────────────────

def lane_of(row: dict, choice: dict | None) -> tuple[str, str] | str:
    """``(lane, tier)``, or the reason code the step has no lane."""
    if row.get("step_class") != "continuation":
        return R_NOT_CONTINUATION
    prev = row.get("prev_tool_class")
    if prev == "technical_op":
        return LANE_TECH_OP, "simple"
    if prev != "web" or not choice:
        return R_LANE_UNMAPPED
    tier = choice.get("complexity")
    task = choice.get("task_type")
    if tier not in TIERS:
        return R_LANE_UNMAPPED
    if task in _QA_TASKS:
        return LANE_QA, tier
    if task == "code":
        return LANE_CODE, tier
    return R_LANE_UNMAPPED


def breaker_class(lane: str) -> tuple[str, str]:
    return (LEVER_TECHOP, "tech_op") if lane == LANE_TECH_OP else (LEVER_PROXY_LOCAL, lane)


# ── breaker feed ─────────────────────────────────────────────────────────────

def units_path() -> Path:
    return paths.state_path(UNITS_NAME)


def read_units(days: int | None = None, *, path: Path | None = None) -> list[dict]:
    """The units the gate wrote, oldest first; ``[]`` when there are none. ``days`` bounds age."""
    out: list[dict] = []
    floor = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    try:
        lines = Path(path or units_path()).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines[-_UNITS_KEEP:]:
        try:
            unit = json.loads(line)
            if not isinstance(unit, dict):
                continue
            if floor is not None and datetime.fromisoformat(unit["ts"]) < floor:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        out.append(unit)
    return out


def _append_unit(unit: dict, path: Path | None = None) -> None:
    target = Path(path or units_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(unit, sort_keys=True) + "\n")
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
        if len(lines) > 2 * _UNITS_KEEP:
            tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
            tmp.write_text("\n".join(lines[-_UNITS_KEEP:]) + "\n", encoding="utf-8")
            os.replace(tmp, target)
    except OSError as exc:
        failopen.record("PROXY-LOCAL-GATE-UNITS-TRIM", exc)


def _calls_in(content: Any) -> list[tuple[str, str]]:
    """``(tool name lower-cased, target)`` of each tool_use block in ``content``."""
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            target = next((str(inp[k]) for k in ("file_path", "path", "pattern", "command") if inp.get(k)), "")
            out.append((str(b.get("name") or "").lower(), target))
    return out


def _previous_calls(body: dict) -> list[tuple[str, str]]:
    """The calls the newest tool results answer (the assistant turn before the newest user turn)."""
    msgs = [m for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    return _calls_in(msgs[-2].get("content")) if len(msgs) >= 2 else []


@dataclass
class _Pending:
    lane: str
    model: str
    served: frozenset
    session_kind: str | None
    results_seen: bool = False
    calls: int = 0


class FailureFeed:
    """Follows served steps for their next two tool calls and writes breaker units."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._pending: OrderedDict[str, list[_Pending]] = OrderedDict()

    def note_served(self, session_id: str | None, lane: str, model: str, message: dict,
                    session_kind: str | None = None) -> None:
        calls = frozenset(_calls_in(message.get("content")))
        if not session_id or not calls:
            return
        # Consecutive served steps each keep their own window.
        pend = self._pending.setdefault(session_id, [])
        pend.append(_Pending(lane, model, calls, session_kind))
        del pend[:-(_REREAD_WINDOW + 1)]
        self._pending.move_to_end(session_id)
        while len(self._pending) > _MAX_PENDING:
            self._pending.popitem(last=False)

    def observe(self, body: dict, session_id: str | None) -> list[str]:
        """Called for every later request of the session. Returns the ``used`` / ``redo``
        outcomes this call settled (usually none)."""
        pending = self._pending.get(session_id) if session_id else None
        if not pending:
            return []
        calls = _previous_calls(body)
        if not calls:                      # a new human turn: the window is over, no verdict
            del self._pending[session_id]
            return []
        settled = []
        for pend in list(pending):
            if not pend.results_seen:
                pend.results_seen = True   # these are the results of the served step itself
                continue
            pend.calls += len(calls)
            if any(c in pend.served for c in calls):
                outcome = "redo"
            elif pend.calls >= _REREAD_WINDOW:
                outcome = "used"
            else:
                continue
            pending.remove(pend)
            lever, task = breaker_class(pend.lane)
            _append_unit({"ts": datetime.now(timezone.utc).isoformat(), "lever": lever, "task_type": task,
                          "model": pend.model, "outcome": outcome, "kind": "techop_local",
                          "session_kind": pend.session_kind}, self.path)
            settled.append(outcome)
        if not pending:
            del self._pending[session_id]
        return settled


# ── dependencies (injectable) ────────────────────────────────────────────────

def _default_breaker(feed: FailureFeed) -> Callable[[str, str, str | None], Any]:
    def call(lever: str, task_type: str, model: str | None):
        from llm_router import quality_breaker

        return quality_breaker.should_route(lever, task_type, model=model,
                                            units_fn=lambda **kw: read_units(kw.get("days"), path=feed.path))
    return call


def _default_promoted(model: str, lane: str) -> bool:
    from llm_router.model_discovery import order_for_chain

    front, _tail = order_for_chain([model], lane, critical=False)
    return model in front


def _default_capability_ok(model: str, needs: Any) -> bool:
    from llm_router import capability_filter

    return bool(capability_filter.filter_chain([model], needs))


def _default_window(model: str) -> int:
    from llm_router import local_models

    return local_models.num_ctx(model)


async def _default_can_serve(body: dict, steps: Any, choice: dict, model: str) -> tuple[bool, str]:
    from llm_router.local_agent import capability

    allow = type("Allow", (), {"allowed": True, "reason": "gate"})()
    cache = capability.BreakerCache(fn=lambda lever, task_type: allow)
    step = capability.Step(body=body, enabled_steps=steps, task_type=choice.get("task_type"), model=model)
    decision = await capability.can_serve_locally(step, cache)
    return decision.local, f"{decision.reason}"


def _default_watchdog() -> str | None:
    from llm_router import ollama_watchdog

    return ollama_watchdog.gate()


@dataclass
class GateDeps:
    load: Callable[[], dict | None] = load_eligibility
    breaker: Callable[[str, str, str | None], Any] | None = None
    promoted: Callable[[str, str], bool] = _default_promoted
    capability_ok: Callable[[str, Any], bool] = _default_capability_ok
    can_serve: Callable[..., Awaitable[tuple[bool, str]]] = _default_can_serve
    backend_open: Callable[[str], bool] = lambda model: False
    watchdog: Callable[[], str | None] = _default_watchdog
    window: Callable[[str], int] = _default_window
    clock: Callable[[], float] = time.monotonic


class LaneGate:
    """One per proxy app. ``decide`` is the whole gate; ``observe`` / ``note_served`` feed the breaker."""

    def __init__(self, steps: Any, step_budget_s: float, deps: GateDeps | None = None,
                 feed: FailureFeed | None = None) -> None:
        self.steps = steps
        self.step_budget_s = step_budget_s
        self.feed = feed or FailureFeed()
        self.deps = deps or GateDeps()
        if self.deps.breaker is None:
            self.deps.breaker = _default_breaker(self.feed)
        self._probe_at: dict[tuple[str, str], float] = {}

    def observe(self, body: dict, session_id: str | None) -> None:
        try:
            self.feed.observe(body, session_id)
        except Exception as exc:  # noqa: BLE001 - the feed never costs a request
            failopen.record("PROXY-LOCAL-GATE-OBSERVE", exc)

    def note_served(self, session_id: str | None, decision: LaneDecision, message: dict,
                    session_kind: str | None = None) -> None:
        try:
            if decision.lane and decision.model:
                self.feed.note_served(session_id, decision.lane, decision.model, message, session_kind)
        except Exception as exc:  # noqa: BLE001
            failopen.record("PROXY-LOCAL-GATE-NOTE-SERVED", exc)

    async def decide(self, body: dict, row: dict, choice: dict | None, needs: Any = None) -> LaneDecision:
        try:
            return await self._decide(body, row, choice, needs)
        except Exception as exc:  # noqa: BLE001 - a gate that cannot decide forwards
            return LaneDecision(False, None, R_ERROR, detail=type(exc).__name__)

    async def _decide(self, body: dict, row: dict, choice: dict | None, needs: Any) -> LaneDecision:
        d = self.deps
        mapped = lane_of(row, choice)
        if isinstance(mapped, str):
            return LaneDecision(False, None, mapped)
        lane, tier = mapped
        data = await asyncio.to_thread(d.load)
        if data is None:
            return LaneDecision(False, None, R_UNREADABLE, lane)
        model = eligible_model(data, lane, tier)
        if model is None:
            return LaneDecision(False, None, R_INELIGIBLE, lane)
        lever, task = breaker_class(lane)
        try:
            verdict = await asyncio.to_thread(d.breaker, lever, task, model)
            allowed = bool(getattr(verdict, "allowed", False))
            state = getattr(verdict, "state", None)
        except Exception as exc:  # noqa: BLE001 - an unreadable breaker forwards
            return LaneDecision(False, model, R_BREAKER, lane, type(exc).__name__)
        if not allowed:
            return LaneDecision(False, model, R_BREAKER, lane)
        if state == "half_open":
            now = d.clock()
            last = self._probe_at.get((lever, task))
            if last is not None and now - last < self.step_budget_s:
                return LaneDecision(False, model, R_BREAKER, lane, "half_open probe in flight")
            self._probe_at[(lever, task)] = now
        if not d.promoted(model, lane):
            return LaneDecision(False, model, R_PROMOTE, lane)
        if needs is None or getattr(needs, "tokens", None) is None:
            # Capability routing is off (or the caller could not size the request): what the
            # request needs (tools, vision, window) is unknown, and the filter that would
            # check it is a no-op when disabled, so it cannot be recomputed here. An unknown
            # need forwards; it never serves (F48). needs_from_anthropic_body swallows its
            # own errors and returns Needs() with tokens=None; its normal path always sets an
            # int (_estimate), so tokens None means the body could not be read.
            return LaneDecision(False, model, R_NEEDS_UNKNOWN, lane)
        if not d.capability_ok(model, needs):
            return LaneDecision(False, model, R_CAPABILITY, lane)
        ok, why = await d.can_serve(body, self.steps, choice or {}, model)
        if not ok:
            return LaneDecision(False, model, R_NO_MODEL, lane, why)
        if d.backend_open(model):
            return LaneDecision(False, model, R_UNHEALTHY, lane)
        hung = await asyncio.to_thread(d.watchdog)
        if hung:
            return LaneDecision(False, model, R_UNHEALTHY, lane, "watchdog")
        from llm_router.capability_filter import WINDOW_MARGIN

        if needs.tokens > WINDOW_MARGIN * d.window(model):
            return LaneDecision(False, model, R_PACK, lane)
        return LaneDecision(True, model, R_SERVED, lane)
