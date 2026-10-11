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
* **Shadow (task 3).** :func:`shadow_sampled` picks 5-10% of decisions, stratified by tier: a
  pure function of the decision id and the tier, so a retry decides the same way. It is OFF
  unless ``LLM_ROUTER_DECISION_SHADOW=on`` AND the P1.7-c gate file says the continuation
  harness passed (:func:`shadow_gate_passed`). A sampled row has ``shadow: true`` and waits for
  a reference label (:func:`record_label`, ``decision_labels.jsonl``): ground truth where the
  prompt is in GT-500, else a blind judge. Nothing in this module calls a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from llm_router import failopen, paths, prompt_key

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


def gate_verdict(rows_or_gate) -> dict:
    """P1.7-c verdict from harness rows (``{"rows": [...], "meta": {...}}`` or a gate file).

    Every row of a fixture at ``messages >= 1800`` must show ``added_p95_ms <= 5``, with
    ``n >= 100`` per arm and the machine uncontended before and after (free memory >= 20%).
    No qualifying row is "not informative" and FAILS (statistics rule 2).
    """
    rows = rows_or_gate.get("rows", [])
    meta = rows_or_gate.get("meta", {})
    big = [r for r in rows if isinstance(r.get("messages"), int) and r["messages"] >= GATE_MIN_HISTORY]
    why = []
    if not big:
        why.append("no row at >= %d messages (nothing checked)" % GATE_MIN_HISTORY)
    for r in big:
        n = min(r["off"]["n"], r["shadow"]["n"])
        if n < GATE_MIN_N_PER_ARM:
            why.append("%s@%d: n=%d < %d per arm" % (r["fixture"], r["messages"], n, GATE_MIN_N_PER_ARM))
        if r["added_p95_ms"] > GATE_MAX_ADDED_P95_MS:
            why.append("%s@%d: added p95 %.2f ms > %.1f" % (r["fixture"], r["messages"], r["added_p95_ms"],
                                                           GATE_MAX_ADDED_P95_MS))
    free = [meta.get(k, {}).get("free_pct") for k in ("machine_before", "machine_after")]
    if any(f is None or f < GATE_MIN_FREE_PCT for f in free):
        why.append("contended or unknown free memory %s (< %d%%)" % (free, GATE_MIN_FREE_PCT))
    return {"passed": not why, "why": why, "rows_checked": len(big)}


def shadow_enabled(gate: Path | None = None) -> bool:
    """The switch: explicit opt-in AND a passing P1.7-c gate. Default off."""
    mode = os.environ.get("LLM_ROUTER_DECISION_SHADOW", "off").strip().lower()
    if mode == "measure":  # the P1.7-c harness only: it must run the shadow to produce the gate
        return True
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
        _append(ledger_path(), row)
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
