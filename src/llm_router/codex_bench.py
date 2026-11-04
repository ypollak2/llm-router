"""Two ways a Codex bench ends before its parsed reset time says so (R2 of the routing plan, 2026-11-04).

The bench comes from prose: ``agent-route.py`` reads "usage limit ... try again at 06:39" out of a failed ``codex exec``, and
when no time parses it benches for a flat hour. That text is the least reliable input in the chain and, until now, nothing ever
looked at the bench again; the one manual ``provider unban`` of 2026-10-11 brought back 58 delegations in 97 hours.

* **A fresh meter with room clears the bench.** Codex writes the real Plus meter into its rollouts (:mod:`codex_meter`). When a
  reading taken AFTER the bench was recorded, at most 30 minutes old, shows the 5 h window under ``FIVE_HOUR_BELOW`` percent and
  the weekly window under ``WEEKLY_BELOW`` percent, the wall the prose reported is gone. A reading taken before the bench says
  nothing (the 14 fresh-meter refusals of 2026-10-04..11-03 were all the 5 h window at 0% with the WEEKLY meter at 100%, and
  were right), so the reading must be newer than the bench and must show room in both windows.
* **A canary re-probes a long bench.** While a bench has more than ``LONG_BENCH_S`` left, one real delegation per
  ``CANARY_INTERVAL_S`` is let through it. The task itself is the probe: nothing synthetic is sent. A success lifts the bench;
  a usage limit benches it again as before.

Both lift the bench with ``provider_reset.clear_provider_reset(source=...)``, which writes the unban to ``provider_bench.jsonl`` with
its source, so KPI G4 counts the bench as wrong. ``LLM_ROUTER_CODEX_BENCH_PROBE=off`` switches both off.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Sequence

from llm_router import codex_meter, failopen, paths, provider_reset, state_files
from llm_router.file_lock import exclusive_lock

#: A bench is cleared when the fresh meter shows the 5 h window under this many percent ...
FIVE_HOUR_BELOW = codex_meter.FIVE_HOUR_SPENT_PCT
#: ... and the weekly window under this many.
WEEKLY_BELOW = 95.0
#: A bench with more than this left is "long": it gets a canary.
LONG_BENCH_S = 24 * 3600
#: At most one canary in this many seconds, across all processes.
CANARY_INTERVAL_S = 3 * 3600
CANARY_FILE = "codex_canary.json"


def enabled() -> bool:
    return os.environ.get("LLM_ROUTER_CODEX_BENCH_PROBE", "on").strip().lower() not in ("0", "off", "false", "no")


def meter_shows_headroom(meter: codex_meter.Meter | None) -> bool:
    """Both windows known and both under their line. One unknown window is not room."""
    return (meter is not None and meter.five_hour_used_pct is not None and meter.weekly_used_pct is not None
            and meter.five_hour_used_pct < FIVE_HOUR_BELOW and meter.weekly_used_pct < WEEKLY_BELOW)


def clear_if_meter_shows_headroom(providers: Sequence[str], now: float | None = None) -> list[str]:
    """Lift the bench of every provider in ``providers`` that a fresh meter reading, newer than the bench, contradicts.

    Cheap when nothing is benched: the rollouts are read only for a provider that has a live bench. Returns the names cleared.
    Never raises: a failure is counted and the bench stands."""
    if not enabled():
        return []
    try:
        now = time.time() if now is None else now
        entries = provider_reset.provider_reset_entries(now)
        benched = {p: entries[p] for p in providers if p in entries}
        if not benched:
            return []
        meter = codex_meter.read_meter(now)
        if not meter_shows_headroom(meter):
            return []
        cleared: list[str] = []
        for name, entry in benched.items():
            if meter.observed_at <= entry["set_at"]:
                continue  # the reading predates the bench: it cannot contradict it
            # ... and the same test again under the bench file's lock: another process may have recorded a NEWER bench for
            # this provider since the entries above were read, and a reading from before it must not lift it
            cleared += provider_reset.clear_provider_reset(name, now, source="meter", if_set_before=meter.observed_at)
        for name in cleared:
            codex_meter.log_event("bench_cleared", provider=name, source="meter", weekly_used_pct=meter.weekly_used_pct,
                                  five_hour_used_pct=meter.five_hour_used_pct,
                                  meter_age_s=round(meter.age_s(now), 1))
        return cleared
    except Exception as exc:  # noqa: BLE001 -- the bench stands; the loss is counted
        failopen.record("CHZ-FO-CODEX-BENCH-METER-CLEAR", exc)
        return []


# ------------------------------------------------------------------------------------------------ the canary


def _canary_path():
    return paths.state_path(CANARY_FILE)


def _last_canary() -> float:
    try:
        data = json.loads(_canary_path().read_text(encoding="utf-8"))
        last = data.get("last") if isinstance(data, dict) else None
        return float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else 0.0
    except FileNotFoundError:
        return 0.0
    except (OSError, ValueError) as exc:
        failopen.record("CHZ-FO-CODEX-CANARY-READ", exc)
        return 0.0


def canary_due(bench_until: float, now: float | None = None) -> bool:
    """Would a canary be allowed through a bench that ends at ``bench_until``? Read-only: nothing is claimed."""
    now = time.time() if now is None else now
    return enabled() and bench_until - now > LONG_BENCH_S and now - _last_canary() >= CANARY_INTERVAL_S


@dataclass(frozen=True)
class CanaryClaim:
    """The canary slot, held: when it was taken, and the stamp it replaced (so an unused slot can be handed back)."""

    claimed_at: float
    previous: float


def _write_stamp(path, value: float) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with state_files.open_private(tmp, "w") as fh:
        fh.write(json.dumps({"last": value}))
    tmp.replace(path)


def claim_canary(bench_until: float, now: float | None = None) -> CanaryClaim | None:
    """Take the canary slot: a claim for exactly one caller per ``CANARY_INTERVAL_S``, decided and written under one lock; None
    for everyone else.

    Refuses (None) when it cannot decide: an unlockable or unwritable slot must not turn into a canary per hook run."""
    now = time.time() if now is None else now
    try:
        path = _canary_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(path.with_suffix(path.suffix + ".lock"), timeout=0.5) as locked:  # a hook must not wait on it
            if not locked:
                return None
            if not canary_due(bench_until, now):
                return None
            previous = _last_canary()
            _write_stamp(path, now)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-CANARY-CLAIM", exc)
        return None
    codex_meter.log_event("canary_claimed", bench_remaining_s=round(bench_until - now))
    return CanaryClaim(now, previous)


def release_canary(claim: CanaryClaim | None) -> None:
    """The run that held ``claim`` ended without making a Codex call (declined by another gate, out of time, out of window):
    hand the slot back so the next task can be the probe. Only if the stamp is still the claim's own. Never raises."""
    if claim is None:
        return
    try:
        path = _canary_path()
        with exclusive_lock(path.with_suffix(path.suffix + ".lock"), timeout=0.5) as locked:
            if locked and _last_canary() == claim.claimed_at:
                _write_stamp(path, claim.previous)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-CANARY-RELEASE", exc)
        return
    codex_meter.log_event("canary_released")


def clear_after_canary(providers: Sequence[str], claim: CanaryClaim, now: float | None = None) -> list[str]:
    """The canary run succeeded: lift the bench of every provider in ``providers`` that was recorded before the run began
    (a bench another process recorded since is newer than this success and stands). Returns the names cleared."""
    try:
        cleared: list[str] = []
        for name in providers:
            cleared += provider_reset.clear_provider_reset(name, now, source="canary", if_set_before=claim.claimed_at)
        for name in cleared:
            codex_meter.log_event("bench_cleared", provider=name, source="canary")
        return cleared
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-CANARY-CLEAR", exc)
        return []
