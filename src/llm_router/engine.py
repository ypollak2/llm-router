"""One classifier engine: ``decide(text, door=...) -> Decision`` (PLAN v16 P1.6).

The seven classifier call sites each decide differently today (door agreement
on the frozen corpus: hook vs gateway 68.0%). This module is the single
decision they will all call. It is the CORE only: nothing in ``src/`` calls it
yet (``feat/engine-wire-<site>`` does that, one PR per site).

Sync authority, per D-19 A: L1 heuristics (``classify.classify_signals`` under
``ENGINE_POLICY``) + L3 lexical model (``ml_lexical``) + a cache. No LLM and no
network on this path (D-13: the hook never waits). ``apply_l2`` is how a
schema-checked LLM verdict could be merged later; ``decide`` never calls it.

Not done here, on purpose (needs data or another item):
  * ``ENGINE_POLICY`` is GATEWAY_POLICY unrefitted; the refit on GT-500 tune
    needs that set (GE3).
  * ``TAU`` and the calibration tables are placeholders until fitted on tune.
  * the ``prefix`` layer is P2.7's; the ``complexity_knn`` vote is left out
    because it needs an embedding call (see ``decide`` docstring).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from llm_router import ml_lexical, prompt_key
from llm_router.classify import GATEWAY_POLICY, _score_categories, classify_signals
from llm_router.paths import state_path
from llm_router.secret_scrubber import SECRET_PATTERNS

log = logging.getLogger("llm_router.engine")

ENGINE_VERSION = "engine/1"
#: One policy for every door. Starts from GATEWAY_POLICY (the live proxy's);
#: the GT-500 tune refit is pending, so its fields equal the gateway's today.
ENGINE_POLICY = dataclasses.replace(GATEWAY_POLICY)
POLICY_ID = "ep1"
#: Abstain below this combined confidence. PLACEHOLDER: fitted on tune and
#: pre-registered before wiring. 0.5 equals L1's own ``_CONFIDENCE_THRESHOLD``.
TAU = 0.5

DOORS = ("proxy", "mcp", "hook", "gateway", "agent_route", "gemini_cli", "router")
Layer = Literal["prefix", "L1", "cache", "L3", "L2", "fallback"]

#: Mirrors ``proxy/claude_tiers.yaml`` route.default (a test pins the two).
TIERS = ("haiku", "sonnet", "opus")
_COMPLEXITY_TIER = {"simple": "haiku", "moderate": "sonnet", "complex": "opus", "deep_reasoning": "opus"}
_TIER_COMPLEXITY = {"haiku": "simple", "sonnet": "moderate", "opus": "complex"}
_TASK_TYPES = ("query", "research", "generate", "analyze", "code", "image", "video", "audio",
               "introspect", "coordinate", "coordination")


@dataclass(frozen=True)
class Decision:
    """Every R-CLS-2 field. ``ms`` is excluded from equality, so two decisions
    compare equal when they decide the same thing."""

    task_type: str
    dims: dict[str, int] | None
    tier: str
    complexity: str
    needs_tools: bool
    local_eligible: bool
    needs_repo_context: bool
    local_only: bool
    confidence: float | None
    calibrated: bool
    abstain: bool
    layer: str
    reason: str
    policy_version: str
    engine_version: str
    ms: float = field(default=0.0, compare=False)


class SecretLocalOnlyError(RuntimeError):
    """A prompt with a secret reached a door with no healthy local model."""


SECRET_REFUSAL = "prompt contains a secret; no local model available; not sent"


def secret_enforcement(decision: Decision, door: str, *, local_model_healthy: bool) -> str:
    """D-28 A. ``"none"``, ``"local_only"``, ``"refuse"`` or ``"log_only"``.

    The proxy only logs (a 400 would break a live Claude Code turn: fail-open
    NFR) until the FP-rate gate and the owner say otherwise. Other doors route
    local-only, and refuse when no local model is healthy.
    """
    if not decision.local_only:
        return "none"
    if door == "proxy":
        return "log_only"
    return "local_only" if local_model_healthy else "refuse"


def require_local_or_refuse(decision: Decision, door: str, *, local_model_healthy: bool) -> None:
    if secret_enforcement(decision, door, local_model_healthy=local_model_healthy) == "refuse":
        raise SecretLocalOnlyError(SECRET_REFUSAL)


# ── calibration ──────────────────────────────────────────────────────────────

N_BINS = 10


def _bin(p: float) -> int:
    return min(N_BINS - 1, max(0, int(p * N_BINS)))


def reliability_table(confidences, correct) -> list[dict]:
    """10 equal-width bins: ``{lo, hi, n, mean_conf, accuracy}`` (empty bins n=0)."""
    rows = [{"lo": k / N_BINS, "hi": (k + 1) / N_BINS, "n": 0, "c": 0.0, "a": 0} for k in range(N_BINS)]
    for p, ok in zip(confidences, correct):
        r = rows[_bin(p)]
        r["n"] += 1
        r["c"] += p
        r["a"] += 1 if ok else 0
    return [{"lo": r["lo"], "hi": r["hi"], "n": r["n"],
             "mean_conf": r["c"] / r["n"] if r["n"] else None,
             "accuracy": r["a"] / r["n"] if r["n"] else None} for r in rows]


def ece(confidences, correct) -> float:
    """Expected calibration error, 10 equal-width bins (PREREG-GT-amend3)."""
    n = len(list(confidences))
    if n == 0:
        raise ValueError("ece of an empty set")
    t = reliability_table(confidences, correct)
    return sum(r["n"] / n * abs(r["mean_conf"] - r["accuracy"]) for r in t if r["n"])


def fit_calibration(confidences, correct) -> list[float | None]:
    """Per-bin empirical accuracy (None for an empty bin: that bin stays raw)."""
    return [r["accuracy"] for r in reliability_table(confidences, correct)]


def _calibration() -> dict[str, list[float | None]]:
    """``{"L1": [...10], "L3": [...10]}`` from ``engine_calibration.json``, or {}."""
    p = _artifact("engine_calibration.json")
    try:
        raw = json.loads(p.read_text())
        return {k: v for k, v in raw.items() if k in ("L1", "L3") and isinstance(v, list) and len(v) == N_BINS}
    except (OSError, ValueError, AttributeError):
        return {}


def _artifact(name: str) -> Path:
    if "lexical" in name:
        override = os.environ.get("LLM_ROUTER_ENGINE_LEXICAL", "").strip()
    else:
        override = os.environ.get("LLM_ROUTER_ENGINE_CALIBRATION", "").strip()
    return Path(override) if override else state_path(name)


def _apply_calibration(layer: str, raw: float, table: dict) -> tuple[float, bool]:
    t = table.get(layer)
    if t is None:
        return raw, False
    v = t[_bin(raw)]
    return (raw, False) if v is None else (v, True)


# ── cache ────────────────────────────────────────────────────────────────────

_LRU_MAX = 4096
_lru: "OrderedDict[str, Decision]" = OrderedDict()
_lru_lock = threading.Lock()
_db: dict[str, sqlite3.Connection | None] = {}
_db_lock = threading.Lock()


def _cache_mode() -> str:
    """``LLM_ROUTER_DECIDE_CACHE``: ``sqlite`` (default), ``memory`` or ``off``."""
    m = os.environ.get("LLM_ROUTER_DECIDE_CACHE", "sqlite").strip().lower()
    return m if m in ("sqlite", "memory", "off") else "sqlite"


def _conn() -> sqlite3.Connection | None:
    path = str(state_path("decide_cache.sqlite"))
    with _db_lock:
        if path not in _db:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                c = sqlite3.connect(path, timeout=0.2, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("CREATE TABLE IF NOT EXISTS decisions (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)")
                c.commit()
                _db[path] = c
            except (sqlite3.Error, OSError) as exc:
                log.warning("engine: decide cache disabled: %s", exc)
                _db[path] = None
        return _db[path]


def clear_cache(*, persistent: bool = True) -> None:
    """Test seam and the operator's reset. Stability runs call this between passes."""
    with _lru_lock:
        _lru.clear()
    if persistent:
        c = _conn()
        if c is not None:
            with _db_lock:
                try:
                    c.execute("DELETE FROM decisions")
                    c.commit()
                except sqlite3.Error:
                    pass


def _cache_key(text: str, policy_version: str, ctx_digest: str) -> str:
    h = hashlib.sha256(prompt_key.normalize(text).encode()).hexdigest()
    return "%s|%s|%s" % (h, policy_version, (ctx_digest or "")[:8])


def _cache_get(key: str, mode: str) -> Decision | None:
    if mode == "off":
        return None
    with _lru_lock:
        d = _lru.get(key)
        if d is not None:
            _lru.move_to_end(key)
            return d
    if mode != "sqlite":
        return None
    c = _conn()
    if c is None:
        return None
    try:
        with _db_lock:
            row = c.execute("SELECT v FROM decisions WHERE k=?", (key,)).fetchone()
        d = Decision(**json.loads(row[0])) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    if d is not None:
        _lru_put(key, d)
    return d


def _lru_put(key: str, d: Decision) -> None:
    with _lru_lock:
        _lru[key] = d
        _lru.move_to_end(key)
        while len(_lru) > _LRU_MAX:
            _lru.popitem(last=False)


def _cache_put(key: str, d: Decision, mode: str) -> None:
    if mode == "off":
        return
    _lru_put(key, d)
    if mode != "sqlite":
        return
    c = _conn()
    if c is None:
        return
    try:
        with _db_lock:
            c.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?)",
                      (key, json.dumps(dataclasses.asdict(d), sort_keys=True), time.time()))
            c.commit()
    except sqlite3.Error:
        pass


# ── decision ─────────────────────────────────────────────────────────────────


def _safer(tier: str) -> str:
    return TIERS[min(len(TIERS) - 1, TIERS.index(tier) + 1)] if tier in TIERS else tier


def _reason(s: str) -> str:
    return s if len(s) <= 120 else s[:119] + "…"


def _secret_names(text: str) -> list[str]:
    return [name for name, pat in SECRET_PATTERNS.items() if pat.search(text)]


def _lexical() -> ml_lexical.LexicalModel | None:
    return ml_lexical.load(_artifact("engine_lexical.json"))


def _policy_version(lex, cal) -> str:
    sig = json.dumps([dataclasses.asdict(ENGINE_POLICY), TAU, lex.version if lex else None, cal], sort_keys=True)
    return "%s-%s" % (POLICY_ID, hashlib.sha256(sig.encode()).hexdigest()[:8])


def _compute(text: str, lex, cal, policy_version: str) -> Decision:
    sig = classify_signals(text, ENGINE_POLICY)
    cx = sig.complexity.value
    tier = _COMPLEXITY_TIER.get(cx, "sonnet")
    dims = {k: int(v) for k, v in _score_categories(text).items()}
    req = sig.capabilities
    needs_tools = bool(req.needs_tools)
    needs_repo = bool(req.repo_search or req.read_files or req.git_operations)

    layer = "L1"
    if sig.confident:
        raw = sig.score / (sig.score + 2.0)
        reason = "L1 %s score=%d" % (sig.task_type.value, sig.score)
    else:
        raw = sig.score / (sig.score + 2.0)  # 0 for no signal, 1/3 for weak
        reason = "L1 low-signal score=%d default=%s" % (sig.score, ENGINE_POLICY.low_signal_default)
        if lex is not None:
            ltier, p = lex.predict(text)
            if ltier in TIERS:
                layer, tier, raw = "L3", ltier, p
                cx = _TIER_COMPLEXITY[tier] if _COMPLEXITY_TIER.get(cx) != tier else cx
                reason = "L3 lexical tier=%s p=%.2f (L1 low-signal)" % (tier, p)
    conf, calibrated = _apply_calibration(layer, raw, cal)
    abstain = conf < TAU
    if abstain:
        tier = _safer(tier)
        cx = _TIER_COMPLEXITY.get(tier, cx) if _COMPLEXITY_TIER.get(cx) != tier else cx
        reason += "; abstain, tier up"
    secrets = _secret_names(text)
    if secrets:
        reason += "; secret:" + ",".join(secrets)
    task = sig.task_type.value
    return Decision(
        task_type=task, dims=dims, tier=tier, complexity=cx, needs_tools=needs_tools,
        local_eligible=bool(secrets) or (not needs_tools and not abstain and tier != "opus" and task != "image"),
        needs_repo_context=needs_repo, local_only=bool(secrets), confidence=round(conf, 6),
        calibrated=calibrated, abstain=abstain, layer=layer, reason=_reason(reason),
        policy_version=policy_version, engine_version=ENGINE_VERSION)


def _fallback(exc: BaseException, policy_version: str) -> Decision:
    return Decision(
        task_type="query", dims=None, tier=_safer("haiku"), complexity="moderate", needs_tools=False,
        local_eligible=False, needs_repo_context=False, local_only=False, confidence=None,
        calibrated=False, abstain=True, layer="fallback",
        reason=_reason("engine error %s; abstain, safer tier" % type(exc).__name__),
        policy_version=policy_version, engine_version=ENGINE_VERSION)


def decide(text: str, *, ctx_digest: str = "", door: str, session_id: str | None = None,
           task_id: str | None = None) -> Decision:
    """The one decision. Sync, no LLM, no network, never raises on content.

    ``door`` names the call site and must be one of ``DOORS``; it is NOT part
    of the cache key or the result: every door gets the same Decision for the
    same text (P1.6-a). ``session_id`` / ``task_id`` are accepted for the
    decision ledger (P1.7) and do not affect the decision.

    L3 here is the lexical model only. The ``complexity_knn`` vote the plan
    also names embeds the prompt through Ollama, which is a model call on the
    sync path, so it is not used (open question for the owner).
    """
    if door not in DOORS:
        raise ValueError("unknown door %r; expected one of %s" % (door, ", ".join(DOORS)))
    t0 = time.perf_counter()
    mode = _cache_mode()
    lex, cal, pv = None, {}, POLICY_ID
    try:
        lex, cal = _lexical(), _calibration()
        pv = _policy_version(lex, cal)
        key = _cache_key(text or "", pv, ctx_digest)
        hit = _cache_get(key, mode)
        if hit is not None:
            return dataclasses.replace(hit, layer="cache", dims=dict(hit.dims) if hit.dims else hit.dims,
                                       ms=(time.perf_counter() - t0) * 1000)
        d = _compute(text or "", lex, cal, pv)
        _cache_put(key, d, mode)
        return dataclasses.replace(d, ms=(time.perf_counter() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001 — a classifier never stalls routing
        log.warning("engine: decide failed, abstaining: %s", exc)
        return dataclasses.replace(_fallback(exc, pv), ms=(time.perf_counter() - t0) * 1000)


# ── L2: schema-checked LLM verdicts (not on the sync path) ───────────────────


def apply_l2(base: Decision, raw) -> Decision:
    """Merge an LLM verdict into ``base`` only if it is schema-valid.

    Valid: a dict (or JSON string) with ``task_type`` in the known set,
    ``tier`` in ``TIERS`` and ``confidence`` a finite number in [0, 1]. Anything
    else abstains: ``base`` one tier safer, ``abstain=True``, and a warning is
    logged (P1.6-f). ``decide`` never calls this; an L2 layer gets authority
    only through a D-19 B/C candidate that passes task 8.
    """
    try:
        v = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        ok = (isinstance(v, dict) and v.get("task_type") in _TASK_TYPES and v.get("tier") in TIERS
              and isinstance(v.get("confidence"), (int, float)) and not isinstance(v.get("confidence"), bool)
              and math.isfinite(v["confidence"]) and 0.0 <= v["confidence"] <= 1.0)
    except (ValueError, TypeError):
        ok = False
    if not ok:
        log.warning("engine: L2 output failed schema, abstaining (layer=%s)", base.layer)
        return dataclasses.replace(base, tier=_safer(base.tier), abstain=True,
                                   complexity=_TIER_COMPLEXITY.get(_safer(base.tier), base.complexity),
                                   reason=_reason("L2 output schema-invalid; abstain, tier up"))
    conf = float(v["confidence"])
    abstain = conf < TAU
    tier = _safer(v["tier"]) if abstain else v["tier"]
    return dataclasses.replace(base, task_type=v["task_type"], tier=tier,
                               complexity=_TIER_COMPLEXITY[tier], confidence=conf, calibrated=False,
                               abstain=abstain, layer="L2", reason=_reason("L2 %s %s" % (v["task_type"], tier)))
