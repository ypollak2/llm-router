"""The decision ledger (PLAN v16 P1.7, R-CLS-6, R-CLS-9): one row per ``engine.decide``.

``engine.decide`` calls :func:`record`; the row goes to ``decisions.jsonl`` in the state
dir (0600): ``{decision_id, ts, door, session_id, task_id, trace_id, text_sha, layer, tier,
confidence, abstain, reason, policy_version, engine_version, policy_id, policy_stage,
grade_basis, inventory_sig, shadow}``. Ids, hashes, numbers and reason codes only: no prompt
text. A write failure never reaches the caller (a classifier never stalls routing).

* **P1.7-a.** Every row carries ``layer``, a ``reason`` and ``confidence`` or ``abstain``
  (:func:`row_complete` is the check; a ``confidence`` of ``None`` is legal only with
  ``abstain`` true).
* **P1.7-e.** Every row carries ``policy_id``, ``policy_stage`` and ``grade_basis``. With no
  dynamic policy the value is the string ``"none"``; it is never NULL.
* **Shadow flag (task 3).** :func:`shadow_sampled` picks 5-10% of decisions, stratified by tier: a
  pure function of the decision id and the tier, so a retry decides the same way. It decides ONE
  thing: the ``shadow: true`` flag on a ledger row, which marks the row as waiting for a reference
  label (:func:`record_label`, ``decision_labels.jsonl``; ground truth where the prompt is in
  GT-500, else a blind judge). The flag is OFF unless ``LLM_ROUTER_DECISION_SHADOW=on`` AND the
  P1.7-c gate file passes (:func:`shadow_gate_passed`). The gate governs this flag ONLY.
  It does NOT govern the pre-existing classifier shadow (``LLM_ROUTER_LOCAL_CLASSIFIER=shadow``,
  ``local_classifier`` / ``proxy/llm_shadow``), which the owner switched on on 2026-10-08, before
  P1.7 existed; that one is governed by its own variable. The harness's measured cost of it is
  evidence, not a gate. Nothing in this module calls a model.
* ``decisions.jsonl`` is size-capped (:data:`MAX_BYTES`, two generations, ``capped_log``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import math

from llm_router import capped_log, failopen, paths, prompt_key

LEDGER_FILENAME = "decisions.jsonl"
LABELS_FILENAME = "decision_labels.jsonl"
GATE_FILENAME = "p17c_gate.json"

LAYERS = ("prefix", "L1", "cache", "L3", "L2", "fallback")
POLICY_STAGES = ("legacy", "shadow", "filter", "full", "none")
GRADE_BASES = ("eligible", "probe", "base", "legacy", "user_pin", "none")
TIERS = ("haiku", "sonnet", "opus")

#: Shadow rate bounds (PLAN: 5-10%, the rate per OD-4). The default is the low end.
RATE_MIN, RATE_MAX, RATE_DEFAULT = 0.05, 0.10, 0.05
#: P1.7-c bars, shared by the harness and :func:`shadow_gate_passed`.
GATE_MAX_ADDED_P95_MS = 5.0
GATE_MIN_HISTORY = 1800
GATE_MIN_N_PER_ARM = 100
GATE_MIN_FREE_PCT = 20  # statistics rule 5: free memory < 20% is contended
GATE_MAX_LOAD1 = 4.0    # section 1.4 contention threshold, checked at the start AND the end of the run
#: decisions.jsonl cap: 1 MiB, two generations (``capped_log``).
MAX_BYTES = 1 << 20
#: Harness-only switch (the P1.7-c run must exercise the shadow flag before any gate exists). A
#: private module attribute, deliberately NOT an environment value a user can set.
_force_shadow = False

_OFF = ("off", "0", "false", "no", "")


def ledger_path() -> Path:
    return paths.state_path(LEDGER_FILENAME)


def labels_path() -> Path:
    return paths.state_path(LABELS_FILENAME)


def gate_path() -> Path:
    return paths.state_path(GATE_FILENAME)


def ledger_enabled() -> bool:
    """On by default; ``LLM_ROUTER_DECISION_LEDGER=off`` is the kill switch."""
    return os.environ.get("LLM_ROUTER_DECISION_LEDGER", "on").strip().lower() not in _OFF


# ── shadow sampling ──────────────────────────────────────────────────────────

def shadow_rate() -> float:
    """``LLM_ROUTER_DECISION_SHADOW_RATE`` clamped to [0.05, 0.10]; bad input is the default."""
    raw = os.environ.get("LLM_ROUTER_DECISION_SHADOW_RATE", "")
    try:
        v = float(raw)
    except ValueError:
        return RATE_DEFAULT
    if v != v:  # NaN
        return RATE_DEFAULT
    return min(RATE_MAX, max(RATE_MIN, v))


def shadow_gate_passed(path: Path | None = None) -> bool:
    """True only when ``p17c_gate.json`` records a passing P1.7-c run that meets every bar
    (re-checked here, so a hand-edited ``"passed": true`` with a small n does not open it)."""
    try:
        g = json.loads((path or gate_path()).read_text(encoding="utf-8"))
        return bool(g.get("passed")) and gate_verdict(g)["passed"]
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return False


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def gate_verdict(rows_or_gate) -> dict:
    """P1.7-c verdict from harness rows (``{"rows": [...], "meta": {...}}`` or a gate file).

    EVERY row at ``messages >= 1800`` must show ``added_p95_ms <= 5`` with ``n >= 100`` per arm.
    The machine must be uncontended at the start AND the end of the run: free memory >= 20% and
    load1 <= 4.0. Every number used must be finite: NaN, inf, a missing field or a non-number
    FAILS (``nan > 5`` is False, so a bare comparison would pass it). No qualifying row is "not
    informative" and FAILS (statistics rule 2).
    """
    rows = rows_or_gate.get("rows", []) if isinstance(rows_or_gate, dict) else []
    meta = rows_or_gate.get("meta", {}) if isinstance(rows_or_gate, dict) else {}
    big = [r for r in rows if isinstance(r, dict) and _num(r.get("messages"))
           and r["messages"] >= GATE_MIN_HISTORY]
    why = []
    if not big:
        why.append("no row at >= %d messages (nothing checked)" % GATE_MIN_HISTORY)
    for r in big:
        tag = "%s@%s" % (r.get("fixture"), r.get("messages"))
        try:
            ns = (r["off"]["n"], r["shadow"]["n"])
            added = r["added_p95_ms"]
            p95s = (r["off"]["p95"], r["shadow"]["p95"])
        except (KeyError, TypeError):
            why.append("%s: missing field" % tag)
            continue
        if not all(_num(x) for x in (*ns, added, *p95s)):
            why.append("%s: non-finite or non-numeric value" % tag)
            continue
        n = min(ns)
        if n < GATE_MIN_N_PER_ARM:
            why.append("%s: n=%d < %d per arm" % (tag, n, GATE_MIN_N_PER_ARM))
        if not added <= GATE_MAX_ADDED_P95_MS:
            why.append("%s: added p95 %.2f ms > %.1f" % (tag, added, GATE_MAX_ADDED_P95_MS))
    for k in ("machine_before", "machine_after"):
        m = meta.get(k) if isinstance(meta, dict) else None
        m = m if isinstance(m, dict) else {}
        free = m.get("free_pct")
        load = m.get("loadavg")
        load1 = load[0] if isinstance(load, (list, tuple)) and load else None
        if not _num(free) or free < GATE_MIN_FREE_PCT:
            why.append("%s: free memory %s (< %d%% or unknown)" % (k, free, GATE_MIN_FREE_PCT))
        if not _num(load1) or load1 > GATE_MAX_LOAD1:
            why.append("%s: load1 %s (> %.1f or unknown)" % (k, load1, GATE_MAX_LOAD1))
    return {"passed": not why, "why": why, "rows_checked": len(big)}


def shadow_enabled(gate: Path | None = None) -> bool:
    """Whether new ledger rows may be flagged ``shadow``: explicit opt-in AND a passing P1.7-c
    gate. Default off. Scope: the decision-ledger flag only (see the module docstring)."""
    if _force_shadow:
        return True
    mode = os.environ.get("LLM_ROUTER_DECISION_SHADOW", "off").strip().lower()
    if mode not in ("on", "1", "true", "yes"):
        return False
    return shadow_gate_passed(gate)


def shadow_sampled(decision_id: str, tier: str, *, rate: float | None = None) -> bool:
    """Deterministic, tier-stratified sample: each tier is sampled independently at ``rate``
    (a hash of ``tier|decision_id``), so a rare tier keeps the same rate as a common one."""
    r = shadow_rate() if rate is None else min(RATE_MAX, max(RATE_MIN, rate))
    h = hashlib.sha256(("%s|%s" % (tier, decision_id)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2.0 ** 64 < r


# ── rows ─────────────────────────────────────────────────────────────────────

def _enum(v, allowed: tuple, default: str = "none") -> str:
    return v if v in allowed else default


def build_row(d, *, door: str, text: str, session_id=None, task_id=None, trace_id=None,
              policy_id: str | None = None, policy_stage: str | None = None,
              grade_basis: str | None = None, inventory_sig: str | None = None,
              shadow: bool | None = None, now: float | None = None) -> dict:
    """The ledger row for a ``Decision``. ``policy_*`` / ``grade_basis`` default to the string
    ``"none"`` (P1.7-e); an unknown value is also written as ``"none"``, never dropped."""
    did = uuid.uuid4().hex
    tier = d.tier
    if shadow is None:
        shadow = shadow_enabled() and shadow_sampled(did, tier)
    return {
        "decision_id": did, "ts": round(time.time() if now is None else now, 3), "door": door,
        "session_id": session_id, "task_id": task_id, "trace_id": trace_id,
        "text_sha": prompt_key.key(text), "layer": d.layer, "tier": tier,
        "confidence": d.confidence, "abstain": bool(d.abstain), "reason": d.reason,
        "policy_version": d.policy_version, "engine_version": d.engine_version,
        "policy_id": policy_id if isinstance(policy_id, str) and policy_id else "none",
        "policy_stage": _enum(policy_stage, POLICY_STAGES),
        "grade_basis": _enum(grade_basis, GRADE_BASES),
        "inventory_sig": inventory_sig if isinstance(inventory_sig, str) and inventory_sig else None,
        "shadow": bool(shadow),
    }


def row_complete(row: dict) -> bool:
    """P1.7-a: layer in the known set, a reason, and confidence-or-abstain."""
    conf = row.get("confidence")
    has_conf = isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0.0 <= conf <= 1.0
    return (row.get("layer") in LAYERS and bool(row.get("reason"))
            and (has_conf or row.get("abstain") is True))


def row_policy_complete(row: dict) -> bool:
    """P1.7-e: ``policy_id``, ``policy_stage`` and ``grade_basis`` present and non-NULL."""
    return (isinstance(row.get("policy_id"), str) and bool(row["policy_id"])
            and row.get("policy_stage") in POLICY_STAGES and row.get("grade_basis") in GRADE_BASES)


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", opener=paths.private_opener) as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _append_ledger(row: dict) -> None:
    """One row, one atomic write, rotated at :data:`MAX_BYTES` (``capped_log``)."""
    capped_log.append(ledger_path(), (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"), MAX_BYTES)


def read_ledger(base: Path | None = None) -> list[dict]:
    """Both generations of ``decisions.jsonl``, oldest first."""
    return capped_log.read_dicts((base or paths.llm_router_home()) / LEDGER_FILENAME)


def _append_many(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", opener=paths.private_opener) as fh:
        fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def record(d, **kw) -> dict | None:
    """Append one row; return it, or ``None`` when disabled or the write failed. Never raises."""
    if not ledger_enabled():
        return None
    try:
        row = build_row(d, **kw)
        _append_ledger(row)
        return row
    except Exception as exc:  # noqa: BLE001 - a ledger never stalls routing
        failopen.record("LR-FO-DECISION-LEDGER-WRITE", exc)
        return None


def record_label(decision_id: str, ref_tier: str, source: str, *, now: float | None = None) -> bool:
    """A reference label for a shadow-sampled decision. ``source`` names where it came from
    (``gt500``, ``judge:<model>``, ``nightly:<model>``); the tier must be a known tier."""
    if ref_tier not in TIERS or not source or not decision_id:
        return False
    try:
        _append(labels_path(), {"decision_id": decision_id, "ref_tier": ref_tier, "source": source,
                                "ts": round(time.time() if now is None else now, 3)})
        return True
    except Exception as exc:  # noqa: BLE001
        failopen.record("LR-FO-DECISION-LABEL-WRITE", exc)
        return False


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except OSError:
        pass
    return out
