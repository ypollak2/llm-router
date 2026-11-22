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
            the streak; a hedge timeout does neither, and neither does a step-budget
            timeout before the request was sent or after any response arrived. A
            step-budget timeout in which the request was sent and no response headers
            or stream lines came back counts as a failure (STALL-2).
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

import contextvars
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

# Fixed vocabulary for the ledger and the unhealthy log line. Matched from the error text, never copied
# from it: runner text can echo prompt content (EKIND-1).
KIND_RUNNER_OOM = "runner_oom"
KIND_RUNNER_COMPUTE = "runner_compute_error"
KIND_XML_PARSE = "xml_parse"
KIND_HTTP_500 = "http_500"
KIND_EMPTY_BODY = "empty_body"
KIND_OTHER = "other"
#: Kinds where the runner itself is broken (the Metal backend stays failed until the runner reloads).
RUNNER_KINDS = frozenset({KIND_RUNNER_OOM, KIND_RUNNER_COMPUTE})
#: Unhealthy period for a broken runner when nothing reloads it: probing every 60 s only produced
#: bursts of instant failures (2026-11-21 22:08-22:11Z, ~115 'Compute error' replies).
DEFAULT_RUNNER_COOLDOWN_S = 300.0

_OOM = re.compile(r"OutOfMemory|insufficient memory|command buffer \d+ failed", re.IGNORECASE)
_COMPUTE = re.compile(
    r"compute error|runner process (?:has )?(?:terminated|unexpectedly stopped|no longer running)"
    r"|model runner has unexpectedly stopped", re.IGNORECASE)
_XML = re.compile(r"xml syntax error", re.IGNORECASE)
_HTTP_500 = re.compile(r"\bHTTP 500\b")


def error_kind(err: str | None) -> str | None:
    """Map a backend error string to one fixed-vocabulary kind (None when there is no error)."""
    if not err:
        return None
    if _OOM.search(err):
        return KIND_RUNNER_OOM
    if _COMPUTE.search(err):
        return KIND_RUNNER_COMPUTE
    if _XML.search(err):
        return KIND_XML_PARSE
    if _HTTP_500.search(err):
        return KIND_HTTP_500
    if err in {"empty response", "zero-line stream", "empty reply"}:
        return KIND_EMPTY_BODY
    return KIND_OTHER


_TIMEOUT_REASONS = frozenset({"hedge_timeout", "budget_exceeded"})

_request_progress_box: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "_request_progress_box", default=None
)


PROGRESS_NOT_SENT = -1   # the backend has not yet sent the request (guard, prepare, queue)
PROGRESS_SENT = 0        # request sent; no response headers and no stream line yet


def begin_stream_progress() -> list[int]:
    """Install a fresh one-slot progress counter for the step about to call the backend and return it.

    Before Python 3.12 ``asyncio.wait_for`` runs the backend in a child task that copies the context;
    on 3.12+ it awaits in the current task. Either way the shared list is correct: the backend sees the
    same list object, so its changes reach the caller (a ContextVar value a child task set itself
    would not). Slot values: ``PROGRESS_NOT_SENT`` (-1) until the backend calls
    :func:`mark_request_sent`; 0 once sent; then +1 for the response headers and +1 per stream line
    (:func:`mark_response_progress`). After a ``budget_exceeded``, ``box[0] == 0`` means the request
    was sent and nothing came back."""
    box = [PROGRESS_NOT_SENT]
    _request_progress_box.set(box)
    return box


def mark_request_sent() -> None:
    """A backend calls this immediately before it opens its request; no-op outside a step."""
    box = _request_progress_box.get()
    if box is not None and box[0] < PROGRESS_SENT:
        box[0] = PROGRESS_SENT


def mark_response_progress() -> None:
    """A backend calls this when response headers arrive and for every stream line; no-op outside a step."""
    box = _request_progress_box.get()
    if box is not None:
        box[0] = max(box[0], PROGRESS_SENT) + 1


def classify(err: str | None, reason: str | None, elapsed_s: float, *, _box: list[int] | None = None) -> str:
    """One backend outcome: ``ok`` | ``fail`` | ``crash`` | ``neutral``.

    ``budget_exceeded``: ``_box`` (from :func:`begin_stream_progress`) says how far the backend got.
    ``_box[0] == 0`` (request sent, no response headers, no line) → FAIL (feeds the streak). Not
    sent (-1), headers or any line received (>0), or no box at all → NEUTRAL. ``hedge_timeout``
    stays NEUTRAL.
    """
    if reason == "hedge_timeout":
        return OUTCOME_NEUTRAL
    if reason == "budget_exceeded":
        return OUTCOME_FAIL if _box is not None and _box[0] == 0 else OUTCOME_NEUTRAL
    if not err:
        return OUTCOME_OK
    if CRASH_SIGNATURE.search(err):
        return OUTCOME_CRASH
    if err in {"empty response", "zero-line stream"} or elapsed_s < INVALID_MAX_S:
        return OUTCOME_FAIL
    return OUTCOME_OK  # a slow wrong answer: the model ran; the policy handles it


@dataclass
class _State:
    streak: int = 0
    opened_at: float | None = None
    probing: bool = False
    trigger: str | None = None
    kind: str | None = None
    cooldown_s: float | None = None


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class BackendHealth:
    def __init__(self, fail_n: int = DEFAULT_FAIL_N, cooldown_s: float = DEFAULT_COOLDOWN_S, *,
                 probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
                 clock: Callable[[], float] | None = None,
                 warn: Callable[[str], None] | None = None,
                 fast_empty: bool = False,
                 runner_cooldown_s: float | None = None) -> None:
        if fail_n < 0 or cooldown_s < 0:
            raise ValueError("backend fail_n and cooldown_s must be >= 0")
        self.fast_empty = fast_empty
        self.fail_n = fail_n
        self.cooldown_s = cooldown_s
        # Longer unhealthy period for runner_oom / runner_compute_error; None keeps the plain cooldown.
        self.runner_cooldown_s = runner_cooldown_s
        self.probe_timeout_s = probe_timeout_s
        self.clock = clock or time.monotonic
        self.warn = warn or _stderr
        self._states: dict[str, _State] = {}

    def _get(self, key: str) -> _State:
        return self._states.setdefault(key, _State())

    def _cooldown(self, st: _State) -> float:
        return self.cooldown_s if st.cooldown_s is None else st.cooldown_s

    def error_kind_for(self, key: str) -> str | None:
        """Kind of the error that last opened the breaker for ``key`` (fixed vocabulary), else None."""
        st = self._states.get(key)
        return None if st is None else st.kind

    def _open(self, key: str, st: _State, trigger: str, detail: str | None) -> dict:
        was_open = st.opened_at is not None
        st.opened_at, st.trigger, st.streak = self.clock(), trigger, 0
        st.kind = error_kind(detail) or KIND_OTHER
        longer = st.kind in RUNNER_KINDS and self.runner_cooldown_s is not None
        st.cooldown_s = max(self.runner_cooldown_s, self.cooldown_s) if longer else None
        if not was_open:
            # The kind, not the runner text: that can echo prompt content.
            self.warn(f"llm-router proxy: local backend {key} marked unhealthy ({trigger}, kind={st.kind}); "
                      f"steps go to Claude for {self._cooldown(st):g}s, then a probe")
        return {"state": "tripped", "trigger": trigger}

    def record(self, key: str, err: str | None, reason: str | None, elapsed_s: float,
               _box: list[int] | None = None) -> dict | None:
        """Record one backend outcome. Returns the trip record when this outcome
        opened the breaker, else ``None``."""
        if self.fail_n == 0:
            return None
        st = self._get(key)
        outcome = classify(err, reason, elapsed_s, _box=_box)
        if (self.fast_empty and err == "zero-line stream" and elapsed_s < INVALID_MAX_S
                and reason not in _TIMEOUT_REASONS):
            return self._open(key, st, "fast_empty", err)
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
        return st.probing or (self.clock() - st.opened_at) < self._cooldown(st)

    def open_for(self, key: str) -> float | None:
        """Seconds the breaker has been open, cooldown or not (``is_open`` is False once a probe is due); None
        while closed."""
        st = self._states.get(key)
        return None if st is None or st.opened_at is None else max(0.0, self.clock() - st.opened_at)

    async def admit(self, key: str, backend) -> tuple[bool, dict | None]:
        """``(send_the_step, ledger_info)``. ``(True, None)`` while closed."""
        if self.fail_n == 0:
            return True, None
        st = self._get(key)
        if st.opened_at is None:
            return True, None
        waited = self.clock() - st.opened_at
        if waited < self._cooldown(st) or st.probing:
            return False, {"state": "open", "trigger": st.trigger, "error_kind": st.kind,
                           "retry_in_s": round(max(0.0, self._cooldown(st) - waited), 1)}
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
            st.opened_at, st.streak, st.trigger, st.kind, st.cooldown_s = None, 0, None, None, None
            self.warn(f"llm-router proxy: local backend {key} passed its health probe; serving resumes")
            return True, dict(info, state="recovered")
        st.opened_at = self.clock()
        return False, dict(info, state="open", trigger=st.trigger, error_kind=st.kind)
