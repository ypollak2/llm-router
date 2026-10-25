"""Lane A (D-285, D-292, D-293): the Haiku tier for classifier-confident CONTINUATION steps.

A continuation is a step that answers the results of the assistant's last tool calls. Lane A is
a carve-out from ``pinned_models``: turn-first, sub-agent and harness steps stay on the pinned
model. It is OFF by default and serves only when ALL of these hold: the YAML key
``lane_a.enabled: true``, the env switch ``LLM_ROUTER_PROXY_LANE_A=on`` (read per request) and
``haiku_rewrite: true``. Confidence is not the safety case (the classifier is unvalidated); the
breaker and the Frontier shadow pairs of later PRs are.

Whatever the switches say, every continuation ledger row carries two fields, so real traffic can
measure the eligible share without serving anything:

``lane_a_reason``  one of ``REASONS`` (a closed set; an unknown value is written as null)
``lane_a_conf``    the engine's confidence on the newest human prompt, or null. Never invented.

``eligible`` means every gate passed and a switch is off: the step stays on the pinned model.
``off`` means the lane was not evaluated at all (no tier policy, advise authority, a step served
elsewhere), so a lane switched off reads ``eligible``, not ``off``. The eligible share ``s_A`` is
``eligible / (continuation rows whose reason is not off and not not_pinned)``. The gates are side-effect
free: rules classifier + ``engine.compute(cache=False)`` in a worker thread, no shadow/ledger/cache write,
no model call. The verdict is on the newest human prompt, so the steps of one turn share it. A bad
``lane_a:`` YAML block raises and stops the tier policy loading. This module holds no model name: the
served model is the policy's ``haiku`` tier.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from llm_router.proxy import tool_classes

ENV_SWITCH = "LLM_ROUTER_PROXY_LANE_A"
DEFAULT_TAU = 0.67  # PLACEHOLDER: fixed by the pre-registration, not by this file
DEFAULT_PREV_TOOL_CLASSES = (tool_classes.TECHNICAL_OP,)
#: Classes that can change the user's files or launch work: never servable by lane A.
NEVER_CLASSES = frozenset({tool_classes.EDIT, tool_classes.EXEC, tool_classes.AGENT})

R_SERVED = "served"
R_ELIGIBLE = "eligible"
R_RETRIED = "retried_original"  # served on Haiku, Haiku refused the body (4xx), the pinned model answered
R_OFF = "off"
R_NOT_PINNED = "not_pinned"
R_NOT_CONTINUATION = "not_continuation"
R_PINNED_TURN = "pinned_turn"
R_NOT_MAIN_THREAD = "not_main_thread"
R_SESSION_KIND = "session_kind"
R_PINNED_BY_USER = "pinned_by_user"
R_CORRECTION = "correction_signal"
R_NOT_ALLOWLISTED = "not_allowlisted"
R_NO_HAIKU_TIER = "no_haiku_tier"
R_CONF_UNAVAILABLE = "conf_unavailable"
R_LOW_CONF = "low_conf"
R_ENGINE_DISAGREE = "engine_disagree"
R_HAIKU_BODY = "haiku_body_blocked"
R_BREAKER_OPEN = "breaker_open_reserved"  # the lane A breaker is open (or a half-open probe is in flight)
R_SWITCH_COST = "switch_cost_high"  # PR2: moving the cached prefix to Haiku would cost >= max_switch_usd
R_CONSEC_CAP = "consecutive_cap"  # PR2: this turn already ran max_consecutive lane A steps in a row
R_KNN_VETO = "knn_veto"  # D-297: complexity_knn (enabled) votes "needs frontier"
R_ERROR = "error"
#: ``TierDecision.detail`` of a lane A serve that is the breaker's half-open probe.
DETAIL_PROBE = "lane_a_probe"

REASONS = (R_SERVED, R_RETRIED, R_ELIGIBLE, R_OFF, R_NOT_PINNED, R_NOT_CONTINUATION, R_PINNED_TURN,
           R_NOT_MAIN_THREAD, R_SESSION_KIND, R_PINNED_BY_USER, R_CORRECTION, R_NOT_ALLOWLISTED,
           R_NO_HAIKU_TIER, R_CONF_UNAVAILABLE, R_LOW_CONF, R_ENGINE_DISAGREE, R_HAIKU_BODY,
           R_BREAKER_OPEN, R_SWITCH_COST, R_CONSEC_CAP, R_KNN_VETO, R_ERROR)


#: PR2 defaults. The brief fixes none of the first four; they are conservative choices, each
#: configurable under ``lane_a:`` in the YAML. ``redo_*`` mirror ``haiku_guard`` (MAX_REDO, MIN_N,
#: WINDOW_DAYS) and prereg section 10.
DEFAULT_MAX_SWITCH_USD = 0.05
DEFAULT_MAX_CONSECUTIVE = 6


@dataclass(frozen=True)
class BreakerConfig:
    """The lane A breaker (``proxy/lane_a_breaker.py``). A hard failure is a Haiku-served step that
    failed (4xx/5xx/429, timeout, empty or truncated reply, tool-call parse error)."""

    trip_failures: int = 5  # hard failures inside window_s open the breaker
    window_s: float = 600.0
    cooldown_s: float = 1800.0  # open -> half-open after a failure trip
    quality_cooldown_s: float = 86400.0  # open -> half-open after a redo-rate trip (quality_breaker's 24 h)
    probe_timeout_s: float = 120.0  # a half-open probe with no outcome after this lets the next one in
    redo_min_n: int = 30  # haiku_guard.MIN_N
    redo_max: float = 0.15  # haiku_guard.MAX_REDO
    redo_window_days: float = 7.0  # haiku_guard.WINDOW_DAYS


_BREAKER_KEYS = {"trip_failures": int, "window_s": float, "cooldown_s": float, "quality_cooldown_s": float,
                 "probe_timeout_s": float, "redo_min_n": int, "redo_max": float, "redo_window_days": float}


def _parse_breaker(raw: object) -> BreakerConfig:
    if raw is None:
        return BreakerConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"lane_a.breaker must be a mapping, got {raw!r}")
    unknown = set(raw) - set(_BREAKER_KEYS)
    if unknown:
        raise ValueError(f"lane_a.breaker: unknown key(s) {sorted(map(str, unknown))}")
    vals = {}
    for k, v in raw.items():
        kind = _BREAKER_KEYS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (kind is int and v != int(v)) or v <= 0:
            raise ValueError(f"lane_a.breaker.{k} must be a positive {kind.__name__}, got {v!r}")
        vals[k] = kind(v)
    if "redo_max" in vals and vals["redo_max"] > 1.0:
        raise ValueError("lane_a.breaker.redo_max must be in (0, 1]")
    return BreakerConfig(**vals)


@dataclass(frozen=True)
class LaneAConfig:
    """The ``lane_a:`` block of the tier policy YAML. Default: off."""

    enabled: bool = False
    tau: float = DEFAULT_TAU
    prev_tool_classes: tuple[str, ...] = DEFAULT_PREV_TOOL_CLASSES
    max_switch_usd: float = DEFAULT_MAX_SWITCH_USD  # cache rule: estimated prefix re-write cost ceiling
    max_consecutive: int = DEFAULT_MAX_CONSECUTIVE  # loop guard: lane A steps in a row inside one turn
    breaker: BreakerConfig = BreakerConfig()


def parse_config(raw: object) -> LaneAConfig:
    """Strict: an unknown key, a non-bool ``enabled``, a ``tau`` outside (0, 1] or a class that is
    unknown, empty or in ``NEVER_CLASSES`` is a ValueError (the policy then fails to load)."""
    if raw is None:
        return LaneAConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"lane_a must be a mapping, got {raw!r}")
    unknown = set(raw) - {"enabled", "tau", "prev_tool_classes", "max_switch_usd", "max_consecutive", "breaker"}
    if unknown:
        raise ValueError(f"lane_a: unknown key(s) {sorted(map(str, unknown))}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"lane_a.enabled must be true or false, got {enabled!r}")
    tau = raw.get("tau", DEFAULT_TAU)
    if isinstance(tau, bool) or not isinstance(tau, (int, float)) or not 0.0 < float(tau) <= 1.0:
        raise ValueError(f"lane_a.tau must be a number in (0, 1], got {tau!r}")
    classes = raw.get("prev_tool_classes", list(DEFAULT_PREV_TOOL_CLASSES))
    if not isinstance(classes, (list, tuple)) or not classes:
        raise ValueError("lane_a.prev_tool_classes must be a non-empty list")
    for c in classes:
        if c not in tool_classes.CLASSES:
            raise ValueError(f"lane_a.prev_tool_classes: unknown class {c!r}")
        if c in NEVER_CLASSES:
            raise ValueError(f"lane_a.prev_tool_classes: {c!r} can change files or launch work and is never allowed")
    sw = raw.get("max_switch_usd", DEFAULT_MAX_SWITCH_USD)
    if isinstance(sw, bool) or not isinstance(sw, (int, float)) or not sw > 0:
        raise ValueError(f"lane_a.max_switch_usd must be a positive number, got {sw!r}")
    cap = raw.get("max_consecutive", DEFAULT_MAX_CONSECUTIVE)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError(f"lane_a.max_consecutive must be an integer >= 1, got {cap!r}")
    return LaneAConfig(enabled, float(tau), tuple(dict.fromkeys(classes)), float(sw), cap,
                       _parse_breaker(raw.get("breaker")))


def env_on() -> bool:
    """``LLM_ROUTER_PROXY_LANE_A``: only ``on`` / ``1`` / ``true`` / ``yes`` turn serving on; unset or
    anything else is off. Read on every request, so ``off`` needs no restart."""
    return os.environ.get("LLM_ROUTER_PROXY_LANE_A", "").strip().lower() in ("on", "1", "true", "yes")


@dataclass
class LaneAOutcome:
    """What the lane decided for one step: the closed reason, the confidence (or None) and, when it
    serves, the Haiku class (``task_type``, ``complexity``) the decision was made on."""

    reason: str
    conf: float | None = None
    serve: bool = False
    task_type: str | None = None
    complexity: str | None = None
    chain_head: list = field(default_factory=list)
    probe: bool = False  # served as the breaker's half-open probe
    key: str | None = None  # conversation key and turn signature (a hash), for the turn memory
    sig: str = ""


def clean_reason(value: object) -> str | None:
    """The closed set: a value outside ``REASONS`` is null, never a free string."""
    return value if isinstance(value, str) and value in REASONS else None


def clean_conf(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if 0.0 <= f <= 1.0 else None


def ensure_fields(row: dict) -> None:
    """Called by the proxy's one ledger write: a continuation row always carries both fields.
    A row the policy never evaluated gets ``off`` and a null confidence."""
    if row.get("step_class") != "continuation":
        return
    row["lane_a_reason"] = clean_reason(row.get("lane_a_reason", R_OFF))
    row["lane_a_conf"] = clean_conf(row.get("lane_a_conf"))


class TurnMemory:
    """Per-conversation memory of lane A serves, in process only (a restart forgets it, which can only
    make the cache rule stricter). It is written only when every switch is on, so a lane that is off
    holds nothing. One entry per conversation: the human turn it is about (a hash of the newest human
    prompt, never the text), how many lane A steps ran in a row in that turn, and whether the
    last step of the turn stayed on Haiku (the prefix is then cached there)."""

    MAX = 4096

    def __init__(self) -> None:
        self._d: dict[str, tuple[str, int]] = {}

    def get(self, key: str, sig: str) -> int:
        """Lane A steps in a row so far in this turn (0 for a new turn or an unknown conversation)."""
        cur = self._d.get(key)
        return cur[1] if cur is not None and cur[0] == sig else 0

    def note(self, key: str, sig: str, *, served: bool, capped: bool = False) -> None:
        if capped:
            return  # the cap holds for the rest of the turn: the entry stays
        if not served:
            self._d.pop(key, None)  # a step that stayed pinned ends the run (and the Haiku cache claim)
            return
        n = self.get(key, sig) + 1
        self._d.pop(key, None)
        self._d[key] = (sig, n)
        while len(self._d) > self.MAX:
            self._d.pop(next(iter(self._d)))

    def forget(self, key: str) -> None:
        self._d.pop(key, None)
