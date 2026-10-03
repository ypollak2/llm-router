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

Fails open: an unreadable or unwritable state file means "no limit known", never
"stop delegating" -- the wall itself is still handled by provider_reset.
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
    """Delegations allowed per window. ``LLM_ROUTER_CODEX_WINDOW_BUDGET``; 0 disables."""
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
    now = time.time() if now is None else now
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(path.with_suffix(path.suffix + ".lock")):
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


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def admit(*, complexity: str, deep_reasoning: bool, now: float | None = None) -> Admission:
    """Decide whether one more Codex delegation may be dispatched.

    This is an ADDITIONAL requirement on top of whatever gate the caller already
    applied (suitability, needs-tools-or-complex); it only ever narrows.
    """
    state = snapshot(now)
    tier = state.tier
    if tier == "exhausted":
        when = f"resumes {_clock(state.resets_at)}" if state.resets_at else "budget is 0"
        return Admission(
            False, tier,
            f"Codex window budget spent ({state.used}/{state.budget} in 5h); {when}",
            state,
        )
    if tier == "tight" and not (complexity == "complex" or deep_reasoning):
        return Admission(
            False, tier,
            f"Codex window tight ({state.used}/{state.budget} used, under half left); "
            f"only complex or deep-reasoning work is admitted, this was {complexity}",
            state,
        )
    return Admission(True, tier, "", state)


def status_line(now: float | None = None) -> str:
    """``codex: 3/15 used this window, resets 21:14`` (no reset clause when unused)."""
    s = snapshot(now)
    line = f"codex: {s.used}/{s.budget} used this window"
    if s.resets_at is not None:
        line += f", resets {_clock(s.resets_at)}"
    return line
