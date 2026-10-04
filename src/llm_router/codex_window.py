"""A rolling 5-hour budget for Codex sub-agent delegation.

Why this exists: a ChatGPT Plus account ran dry after about 17 `codex exec` agent
tasks inside one 5-hour window (message: "...or try again at 11:33 PM."), and
OpenAI publishes only a range (5-45 GPT-6 Astra messages per 5h). Codex passed
13/17 real tasks, the same as Claude, so the window is worth spending -- but on
the hardest work, and not so fast that the last tasks of the window hit the wall
and fail. provider_reset benches Codex AFTER the wall; this keeps delegation
from reaching it.

The counter is a list of dispatch timestamps, pruned to the window, so "rolling"
means what it says: a slot frees 5 hours after the delegation that used it.

Budget default 15, deliberately below the ~17 observed. Tiers:

* ``open``       more than half the budget remains: no extra requirement.
* ``tight``      under half remains: only complex or deep-reasoning work.
* ``exhausted``  nothing remains: no delegation until the oldest slot frees.

Admission and counting are ONE critical section (``reserve``): it takes the
exclusive lock, re-reads the window, decides, and writes the stamp before
releasing. A separate read-then-write let N concurrent hook processes all see
"14/15" and all dispatch (8 processes behind a barrier reached used=20 of 15).
A reservation that is abandoned before any Codex call is handed back with
``release``; once a call is made the stamp stays, so a run that dies on the
usage limit still counts.

``LLM_ROUTER_CODEX_WINDOW_BUDGET=0`` blocks ALL Codex delegation (a kill switch);
it does not remove the cap.

Fails open: an unreadable, unwritable or unlockable state file means "no limit
known", never "stop delegating" -- the wall itself is still handled by
provider_reset.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from llm_router import failopen, paths
from llm_router.file_lock import exclusive_lock

WINDOW_SECONDS = 5 * 3600
DEFAULT_BUDGET = 15
#: Below this share of the budget remaining, only the hardest work is admitted.
TIGHT_BELOW_FRACTION = 0.5


def budget() -> int:
    """Delegations allowed per window. ``LLM_ROUTER_CODEX_WINDOW_BUDGET``.

    0 blocks all Codex delegation (kill switch); it does not remove the cap.
    """
    raw = os.environ.get("LLM_ROUTER_CODEX_WINDOW_BUDGET", "").strip()
    if not raw:
        return DEFAULT_BUDGET
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_BUDGET


def _state_file() -> Path:
    return paths.state_path("codex_window.json")


def _in_window(path: Path, now: float) -> list[float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    stamps = data.get("delegations") if isinstance(data, dict) else None
    if not isinstance(stamps, list):
        return []
    # `<= now` also drops a timestamp from the future (clock moved back), which
    # would otherwise hold a slot for longer than the window.
    return sorted(
        float(t) for t in stamps
        if isinstance(t, (int, float)) and now - WINDOW_SECONDS < t <= now
    )


@dataclass(frozen=True)
class WindowState:
    used: int
    budget: int
    #: When the oldest counted delegation leaves the window (a slot frees, and
    #: when ``used >= budget`` delegation resumes). ``None`` when nothing is used.
    resets_at: float | None

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    @property
    def tier(self) -> str:
        if self.remaining <= 0:
            return "exhausted"
        if self.remaining < self.budget * TIGHT_BELOW_FRACTION:
            return "tight"
        return "open"


def snapshot(now: float | None = None) -> WindowState:
    """Current window usage. Fails open to an empty window."""
    now = time.time() if now is None else now
    try:
        stamps = _in_window(_state_file(), now)
    except Exception as exc:  # noqa: BLE001 -- unreadable state must not stop delegation
        failopen.record("CHZ-FO-CODEX-WINDOW-READ", exc)
        stamps = []
    return WindowState(
        used=len(stamps),
        budget=budget(),
        resets_at=stamps[0] + WINDOW_SECONDS if stamps else None,
    )


def record_delegation(now: float | None = None) -> None:
    """Count one Codex dispatch against the window. Never raises."""
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(path.with_suffix(path.suffix + ".lock")):
            now = time.time() if now is None else now  # inside the lock, see reserve()
            try:
                stamps = _in_window(path, now)
            except (OSError, ValueError):
                stamps = []  # corrupt file: start a fresh window rather than fail again
            stamps.append(now)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"delegations": stamps}), encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 -- counting must never break delegation
        failopen.record("CHZ-FO-CODEX-WINDOW-WRITE", exc)


@dataclass(frozen=True)
class Admission:
    allowed: bool
    tier: str
    #: Why a delegation was declined ("" when allowed). Written to the ledger.
    reason: str
    state: WindowState
    #: The stamp ``reserve`` wrote for this admission (``release`` takes it back).
    #: ``None`` for a decline, for the read-only ``admit``, and when the counter
    #: failed open (nothing was written, so there is nothing to release).
    token: float | None = None


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def _decide(state: WindowState, complexity: str, deep_reasoning: bool,
            enforce_tier: bool = True) -> Admission:
    tier = state.tier
    if tier == "exhausted":
        when = f"resumes {_clock(state.resets_at)}" if state.resets_at else "budget is 0"
        return Admission(
            False, tier,
            f"Codex window budget spent ({state.used}/{state.budget} in 5h); {when}",
            state,
        )
    if enforce_tier and tier == "tight" and not (complexity == "complex" or deep_reasoning):
        return Admission(
            False, tier,
            f"Codex window tight ({state.used}/{state.budget} used, under half left); "
            f"only complex or deep-reasoning work is admitted, this was {complexity}",
            state,
        )
    return Admission(True, tier, "", state)


def admit(*, complexity: str, deep_reasoning: bool, now: float | None = None) -> Admission:
    """Read-only decision: may one more Codex delegation be dispatched?

    Does NOT reserve a slot, so it is only a preview; dispatching code must call
    ``reserve`` (decide and count under one lock). This is an ADDITIONAL
    requirement on top of whatever gate the caller already applied (suitability,
    needs-tools-or-complex); it only ever narrows.
    """
    return _decide(snapshot(now), complexity, deep_reasoning)


def reserve(*, complexity: str, deep_reasoning: bool, enforce_tier: bool = True,
            now: float | None = None) -> Admission:
    """Decide AND count one Codex delegation as a single critical section.

    Under the exclusive lock: re-read the window, decide, and (when allowed)
    write the stamp. Concurrent callers are serialized, so at most
    ``budget - used`` of them are admitted. ``enforce_tier=False`` skips the
    "tight" complexity requirement (used for a fallback dispatch of a task that
    was already admitted); an exhausted window still refuses.

    Fails open: if the lock or the file cannot be used, the delegation is
    allowed, the condition is recorded, and no token is returned.
    """
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(path.with_suffix(path.suffix + ".lock")) as locked:
            if not locked:
                failopen.record("CHZ-FO-CODEX-WINDOW-LOCK",
                                detail="could not take the codex window lock; admitting unchecked")
                return Admission(True, "open", "", snapshot(now))
            # The clock is read INSIDE the lock. Read before it, a process that
            # waited would hold a `now` older than the stamp of the one that ran
            # while it waited, treat that stamp as "from the future", drop it, and
            # write the file back without it.
            now = time.time() if now is None else now
            try:
                stamps = _in_window(path, now)
            except (OSError, ValueError):
                stamps = []  # corrupt file: start a fresh window
            state = WindowState(
                used=len(stamps), budget=budget(),
                resets_at=stamps[0] + WINDOW_SECONDS if stamps else None,
            )
            adm = _decide(state, complexity, deep_reasoning, enforce_tier)
            if not adm.allowed:
                return adm
            stamps.append(now)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"delegations": stamps}), encoding="utf-8")
            tmp.replace(path)
            return Admission(True, adm.tier, "", state, token=now)
    except Exception as exc:  # noqa: BLE001 -- a broken counter must not stop delegation
        failopen.record("CHZ-FO-CODEX-WINDOW-RESERVE", exc)
        return Admission(True, "open", "", WindowState(0, budget(), None))


def release(token: float | None, now: float | None = None) -> None:
    """Hand back a reservation that was never used (declined before any Codex
    call). Removes exactly one stamp equal to ``token``. Never raises."""
    if token is None:
        return
    try:
        path = _state_file()
        with exclusive_lock(path.with_suffix(path.suffix + ".lock")):
            now = time.time() if now is None else now  # inside the lock, see reserve()
            try:
                stamps = _in_window(path, now)
            except (OSError, ValueError):
                return
            if token not in stamps:
                return  # already aged out or never written
            stamps.remove(token)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"delegations": stamps}), encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-WINDOW-RELEASE", exc)


def status_line(now: float | None = None) -> str:
    """``codex: 3/15 used this window, resets 21:14`` (no reset clause when unused)."""
    s = snapshot(now)
    line = f"codex: {s.used}/{s.budget} used this window"
    if s.resets_at is not None:
        line += f", resets {_clock(s.resets_at)}"
    return line
