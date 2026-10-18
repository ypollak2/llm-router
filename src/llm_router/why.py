"""P3.5-f: why did the router decide that? The last N decisions as records (pure, read-only).

Sources (all read, nothing written; ids and enums only, never prompt or answer text):

* ``proxy_calls.jsonl`` rows: ``served_model``, ``tier_reason``, ``tier_task_type`` /
  ``tier_complexity``, ``cls_*``, ``tier_quota_state``, ``tier_proposed``.
* ``usage.db`` ``routing_decisions`` rows (hook and MCP/router doors): ``final_model``,
  ``reason_code``, ``task_type`` / ``complexity``, ``layer``, ``confidence``, ``skipped_json``.
* ``decisions.jsonl`` (``decision_ledger``): joined by ``trace_id`` to fill ``layer`` /
  ``confidence`` / ``door`` that a row did not carry.

JOIN PROBE (tests/test_p35_why.py::test_join_probe): ``engine.decide`` writes ``decisions.jsonl``
with ``trace_id`` = null, so today a decision record cannot be joined to a hook/MCP
``routing_decisions`` row (or a proxy row). Those rows carry ``layer``/``confidence`` in their own
columns, so the join is a fill-in, not a requirement; what a row does not carry prints
``n/a (not recorded for <door>)``. The join below starts working the day ``engine.decide`` stamps a
trace id; no write was added here (``decide`` is the hot path, not an off-path writer).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from llm_router import decision_ledger, paths
from llm_router.proxy import ledger as proxy_ledger

ELEMENTS = ("class", "confidence", "layer", "quota_state", "alternatives")
_SKIP_KEYS = ("model", "reason", "until")  # whitelist: never anything else from skipped_json


def na(door: str) -> str:
    return f"n/a (not recorded for {door})"


def _s(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ts(raw) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _cls(task, cx) -> str | None:
    task, cx = _s(task), _s(cx)
    return f"{task}/{cx}" if task and cx else task or cx


def _alternatives(raw) -> list[dict] | None:
    """``skipped_json`` -> whitelisted entries; None = unknown (NULL or unparseable), [] = none skipped."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return [{k: e[k] for k in _SKIP_KEYS if k in e} for e in data if isinstance(e, dict)]


def _record(door, model, tier_reason, ts, trace_id, el, source, abstain=None) -> dict:
    rec = {"door": door, "model": _s(model) or na(door), "tier_reason": _s(tier_reason) or na(door),
           "ts": ts, "trace_id": _s(trace_id), "source": source, "abstain": abstain}
    for k in ELEMENTS:
        rec[k] = el.get(k)  # None = not recorded; render() / finish() print the n/a
    return rec


def _proxy_record(r: dict) -> dict:
    tier, proposed = _s(r.get("tier")), _s(r.get("tier_proposed"))
    alts = None
    if proposed and tier:
        alts = [{"model": proposed, "reason": f"proposed tier, served {tier} ({_s(r.get('tier_reason')) or 'n/a'})"}] \
            if proposed != tier else []
    return _record("proxy", r.get("served_model") or r.get("requested_model"), r.get("tier_reason"),
                   _ts(r.get("ts")), r.get("trace_id"),
                   {"class": _cls(r.get("tier_task_type"), r.get("tier_complexity")),
                    "confidence": _num(r.get("cls_confidence")), "layer": _s(r.get("cls_layer")),
                    "quota_state": _s(r.get("tier_quota_state")), "alternatives": alts}, "proxy_calls.jsonl")


def _db_records(db: Path, limit: int, notes: list[str]) -> list[dict]:
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            from llm_router.provider_classes import real_decision_sql

            cols = {r[1] for r in con.execute("PRAGMA table_info(routing_decisions)")}
            if not cols:
                return []
            rows = con.execute("SELECT * FROM routing_decisions WHERE success = 1 AND "
                               + real_decision_sql(con) + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        notes.append(f"usage.db unreadable ({type(exc).__name__})")
        return []
    out = []
    for r in rows:
        d = dict(r)  # columns a database predates are simply absent -> .get() -> not recorded
        door = "hook" if d.get("classifier_type") == "hook" else "mcp"
        ab = d.get("abstain")
        out.append(_record(door, d.get("final_model"), d.get("reason_code"), _ts(d.get("timestamp")),
                           d.get("trace_id"),
                           {"class": _cls(d.get("task_type"), d.get("complexity")),
                            "confidence": _num(d.get("confidence")), "layer": _s(d.get("layer")),
                            "alternatives": _alternatives(d.get("skipped_json"))},
                           "routing_decisions", None if ab is None else bool(ab)))
    return out


def _join(rec: dict, by_trace: dict[str, dict]) -> bool:
    """Fill layer/confidence (and door, for a row whose door was inferred) from the decision record."""
    d = by_trace.get(rec["trace_id"] or "")
    if d is None:
        return False
    if rec["layer"] is None:
        rec["layer"] = _s(d.get("layer"))
    if rec["confidence"] is None:
        rec["confidence"] = _num(d.get("confidence"))
    if rec["abstain"] is None and isinstance(d.get("abstain"), bool):
        rec["abstain"] = d["abstain"]
    if rec["source"] == "routing_decisions" and _s(d.get("door")):
        rec["door"] = d["door"]
    rec["decision_id"] = _s(d.get("decision_id"))
    return True


def _finish(rec: dict) -> dict:
    door = rec["door"]
    for k in ELEMENTS:
        v = rec[k]
        if k == "confidence" and v is None and rec.get("abstain"):
            rec[k] = "n/a (engine abstained)"
        elif v is None:
            rec[k] = na(door)
    rec.setdefault("decision_id", None)
    return rec


def last_decisions(n: int = 5, base: Path | None = None) -> tuple[list[dict], list[str]]:
    """The last ``n`` decisions, newest first, plus notes about unreadable sources."""
    base = base or paths.llm_router_home()
    n = max(1, int(n))
    notes: list[str] = []
    proxy = [_proxy_record(r) for r in proxy_ledger.read_rows(base / proxy_ledger.LEDGER_NAME)[-n:]]
    seen = {p["trace_id"] for p in proxy if p["trace_id"]}
    db = [r for r in _db_records(base / "usage.db", n, notes) if not (r["trace_id"] and r["trace_id"] in seen)]
    by_trace = {d["trace_id"]: d for d in decision_ledger.read_ledger(base) if _s(d.get("trace_id"))}
    recs = sorted(proxy + db, key=lambda r: (r["ts"] is not None, r["ts"] or 0.0), reverse=True)[:n]
    for r in recs:
        _join(r, by_trace)
        _finish(r)
    return recs, notes


def render(rec: dict) -> str:
    """Header (door, model served, tier_reason) and the five elements. Ids only, no prompt text."""
    alts = rec["alternatives"]
    if isinstance(alts, list):
        alts = "; ".join(f"{a.get('model', '?')}: {a.get('reason', '?')}" for a in alts) or "none passed over"
    conf = rec["confidence"]
    lines = [f"{rec['door']} | {rec['model']} | tier_reason: {rec['tier_reason']}",
             f"  1 class:        {rec['class']}",
             f"  2 confidence:   {conf:.2f}" if isinstance(conf, float) else f"  2 confidence:   {conf}",
             f"  3 layer:        {rec['layer']}",
             f"  4 quota state:  {rec['quota_state']}",
             f"  5 alternatives: {alts}"]
    if rec["trace_id"]:
        lines.append(f"  trace: {rec['trace_id']}")
    return "\n".join(lines)
