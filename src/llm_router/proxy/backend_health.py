"""Backend-health circuit breaker for the proxy's local serving path.

WHY. On 2026-09-30 (``~/.rsi/research/llm-router-cursor-parity/p3-compaction-ab.md``)
the dedicated Ollama server's Metal backend ran out of GPU memory under swap
pressure (``ggml_metal_synchronize: command buffer 0 failed with status 5`` /
``kIOGPUCommandBufferCallbackErrorOutOfMemory``). From then until a restart,
every step got an empty reply in ~0.1 s: 29/29 empties in that A/B came from
those windows, against 0/24 while the backend was healthy. The same fault is in
the 2026-09-28 server log 126 times. The proxy kept sending every step there,
and every one then fell back to Claude with the attempt's latency added.

WHAT. One breaker per serving model, per proxy process (the proxy is one
long-lived process, so in-memory state is the whole truth):

``closed``  steps go to the backend. Each backend outcome is classified
            (:func:`classify`): a crash signature opens the breaker at once;
            ``fail_n`` consecutive failures (an empty reply, or any invalid
            reply in under ``INVALID_MAX_S``) open it; a working reply resets
            the streak; a timeout (hedge or step budget) does neither.
``open``    no step is sent. The proxy forwards to Claude and the ledger row
            says ``reason = "backend_unhealthy"``. A one-line warning goes to
            stderr when it opens.
            After ``cooldown_s`` the next step runs the backend's ``probe``:
            a one-token request with the serving ``num_ctx`` (a different
            ``num_ctx`` would make Ollama reload the model). Probe passes ->
            ``closed`` and that step is tried; probe fails -> a new cooldown
            starts. One probe at a time; a step that arrives while a probe is
            running is skipped. A backend with no ``probe`` gets one trial step.

``fail_n = 0`` disables the breaker.

Defaults: ``fail_n = 3``. The healthy windows produced 0 empties in 24 attempts,
the crashed windows 29 in 29, so three in a row separates them with room to
spare. ``cooldown_s = 60``. The crashed server never recovered by itself in
those runs, and a probe of a crashed server returns in ~0.1 s, so a short
cooldown costs almost nothing and lets a restarted server back in within a
minute.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from typing import Callable

from llm_router.proxy.ledger import scrub_detail

REASON_BACKEND_UNHEALTHY = "backend_unhealthy"

DEFAULT_FAIL_N = 3
DEFAULT_COOLDOWN_S = 60.0
DEFAULT_PROBE_TIMEOUT_S = 10.0
# A reply this fast that is still invalid did not come from a working decode:
# healthy served steps took 3.7-13.2 s (n=9) in the same A/B.
INVALID_MAX_S = 1.0

OUTCOME_OK = "ok"
OUTCOME_FAIL = "fail"
OUTCOME_CRASH = "crash"
OUTCOME_NEUTRAL = "neutral"

# Text an Ollama / llama.cpp runner emits when the backend itself has failed,
# as it reaches the client in an HTTP error body or a streamed ``error`` line.
CRASH_SIGNATURE = re.compile(
    r"compute error"
    r"|command buffer \d+ failed"
    r"|OutOfMemory"
    r"|runner process (?:has )?(?:terminated|unexpectedly stopped|no longer running)"
    r"|model runner has unexpectedly stopped",
    re.IGNORECASE,
)

_TIMEOUT_REASONS = frozenset({"hedge_timeout", "budget_exceeded"})


def classify(err: str | None, reason: str | None, elapsed_s: float) -> str:
    """One backend outcome: ``ok`` | ``fail`` | ``crash`` | ``neutral``."""
    if reason in _TIMEOUT_REASONS:
        return OUTCOME_NEUTRAL
    if not err:
        return OUTCOME_OK
    if CRASH_SIGNATURE.search(err):
        return OUTCOME_CRASH
    if err == "empty response" or elapsed_s < INVALID_MAX_S:
        return OUTCOME_FAIL
    return OUTCOME_OK  # a slow wrong answer: the model ran; the policy handles it


@dataclass
class _State:
    streak: int = 0
    opened_at: float | None = None
    probing: bool = False
    trigger: str | None = None


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class BackendHealth:
    def __init__(self, fail_n: int = DEFAULT_FAIL_N, cooldown_s: float = DEFAULT_COOLDOWN_S, *,
                 probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
                 clock: Callable[[], float] | None = None,
                 warn: Callable[[str], None] | None = None) -> None:
        if fail_n < 0 or cooldown_s < 0:
            raise ValueError("backend fail_n and cooldown_s must be >= 0")
        self.fail_n = fail_n
        self.cooldown_s = cooldown_s
        self.probe_timeout_s = probe_timeout_s
        self.clock = clock or time.monotonic
        self.warn = warn or _stderr
        self._states: dict[str, _State] = {}

    def _get(self, key: str) -> _State:
        return self._states.setdefault(key, _State())

    def _open(self, key: str, st: _State, trigger: str, detail: str | None) -> dict:
        was_open = st.opened_at is not None
        st.opened_at, st.trigger, st.streak = self.clock(), trigger, 0
        if not was_open:
            self.warn(f"llm-router proxy: local backend {key} marked unhealthy ({trigger}: "
                      f"{(detail or '')[:120]}); steps go to Claude for {self.cooldown_s:g}s, then a probe")
        return {"state": "tripped", "trigger": trigger}

    def record(self, key: str, err: str | None, reason: str | None, elapsed_s: float) -> dict | None:
        """Record one backend outcome. Returns the trip record when this outcome
        opened the breaker, else ``None``."""
        if self.fail_n == 0:
            return None
        st = self._get(key)
        outcome = classify(err, reason, elapsed_s)
        if outcome == OUTCOME_CRASH:
            return self._open(key, st, "crash_signature", err)
        if outcome == OUTCOME_FAIL:
            st.streak += 1
            if st.streak >= self.fail_n:
                return self._open(key, st, "consecutive_invalid", err)
        elif outcome == OUTCOME_OK:
            st.streak = 0
        return None

    def is_open(self, key: str) -> bool:
        """True while steps for ``key`` would be skipped without a probe (open
        and still cooling down, or a probe in flight). Read-only: after the
        cooldown this is False so the next step reaches ``admit`` and probes."""
        st = self._states.get(key)
        if st is None or st.opened_at is None:
            return False
        return st.probing or (self.clock() - st.opened_at) < self.cooldown_s

    async def admit(self, key: str, backend) -> tuple[bool, dict | None]:
        """``(send_the_step, ledger_info)``. ``(True, None)`` while closed."""
        if self.fail_n == 0:
            return True, None
        st = self._get(key)
        if st.opened_at is None:
            return True, None
        waited = self.clock() - st.opened_at
        if waited < self.cooldown_s or st.probing:
            return False, {"state": "open", "trigger": st.trigger,
                           "retry_in_s": round(max(0.0, self.cooldown_s - waited), 1)}
        probe = getattr(backend, "probe", None)
        if probe is None:
            # Nothing cheap to ask: this step is the trial. Its outcome, via
            # record(), closes or re-opens the breaker.
            st.opened_at, st.streak = None, max(0, self.fail_n - 1)
            return True, {"state": "trial"}
        st.probing = True
        t0 = self.clock()
        try:
            ok, detail = await probe(self.probe_timeout_s)
        except Exception as exc:  # noqa: BLE001 - a probe that raises is a failed probe
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        finally:
            st.probing = False
        info = {"probe_ok": ok, "probe_detail": scrub_detail(detail or ""),
                "probe_s": round(self.clock() - t0, 3)}
        if ok:
            st.opened_at, st.streak, st.trigger = None, 0, None
            self.warn(f"llm-router proxy: local backend {key} passed its health probe; serving resumes")
            return True, dict(info, state="recovered")
        st.opened_at = self.clock()
        return False, dict(info, state="open", trigger=st.trigger)
