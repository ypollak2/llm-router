#!/usr/bin/env python3
"""Reconcile the proxy ledger's real Anthropic spend with an independent total.

Read-only. For each proxied session it compares

  * ledger:    ``proxy.cost_accounting`` real Anthropic USD over the session's rows
  * reference: the session transcript's assistant usage (deduplicated by
               ``message.id``, last record wins), priced with the same rates, OR a
               ``--reference-json`` {session_id: total_cost_usd} (Claude Code's own
               ``total_cost_usd`` from ``claude -p --output-format json``)

and applies the two PR 8 checks:

  * FULLY FORWARDED session: ledger cost within ``--tolerance`` (default 3%).
  * FULLY LOCAL session:     ledger Anthropic tokens exactly 0.

Unknown usage is null only on rows written since #261; older rows carry zeros
for it, so a session spanning that change can look reconciled when it is not.

Mixed sessions are reported, not pass/failed: for them the transcript prices the
locally served turns at list price (the phantom), so a gap there is expected.

Usage:
  reconcile_proxy_cost.py [--ledger FILE] [--projects-dir DIR] [--exclude SID ...]
                          [--sessions N] [--reference-json FILE] [--tolerance 0.03] [--json]
Exit status 1 when any applicable check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_router.proxy import cost_accounting as ca  # noqa: E402
from llm_router.proxy import ledger  # noqa: E402


def transcript_messages(path: Path) -> dict[str, dict]:
    """``{message.id: {model, usage}}`` for a session: its transcript plus the
    sub-agent transcripts under ``<session>/subagents/`` (they share the
    session id at the proxy; leaving them out made 4 of the first 5 real
    sessions look 80% unreconciled)."""
    out: dict[str, dict] = {}
    files = [path, *sorted((path.parent / path.stem).glob("**/*.jsonl"))]
    for f in files:
        _read_transcript(f, out)
    return out


def _read_transcript(path: Path, out: dict[str, dict]) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        if m.get("id") and isinstance(m.get("usage"), dict):
            out[m["id"]] = {"model": m.get("model"), "usage": m["usage"]}


def price_message(msg: dict) -> float | None:
    u = ledger.normalize_usage(msg["usage"])
    return ledger._price_tokens(msg.get("model") or "", input_tokens=u["input_tokens"],  # noqa: SLF001
                                output_tokens=u["output_tokens"],
                                cache_read_input_tokens=u["cache_read_input_tokens"],
                                cache_creation_5m=u["cache_creation_5m"],
                                cache_creation_1h=u["cache_creation_1h"])


def reconcile_session(sid: str, rows: list[dict], messages: dict[str, dict] | None,
                      reference_usd: float | None, tolerance: float) -> dict:
    rows = [ca.annotate(dict(r)) if not ca.has_cost_fields(r) else r for r in rows]
    s = ca.proxy_session_cost(rows)
    out: dict = {"session_id": sid, "calls": s["calls"], "local": s["calls_local"],
                 "anthropic": s["calls_anthropic"], "ledger_usd": s["real_anthropic_usd"]}
    if s["calls_local"] == 0:
        kind = "forwarded"
    elif s["calls_anthropic"] == 0:
        kind = "local"
    else:
        kind = "mixed"
    out["kind"] = kind
    if kind == "local":
        out["check"] = ca.check_local(rows)
    if messages is not None:
        joined = [r for r in rows if r.get("msg_id") in messages and ca.served_by(r) == ca.SERVED_BY_ANTHROPIC]
        t_cost = [price_message(messages[r["msg_id"]]) for r in joined]
        out["joined"] = len(joined)
        out["ledger_rows_not_in_transcript"] = sum(
            1 for r in rows if ca.served_by(r) == ca.SERVED_BY_ANTHROPIC and r.get("msg_id") not in messages)
        out["ledger_usd_not_in_transcript"] = round(sum(
            ca.row_cost(r) or 0.0 for r in rows
            if ca.served_by(r) == ca.SERVED_BY_ANTHROPIC and r.get("msg_id") not in messages), 4)
        out["transcript_usd_joined"] = round(sum(c for c in t_cost if c is not None), 4)
        out["ledger_usd_joined"] = round(sum(ca.row_cost(r) or 0.0 for r in joined), 4)
        out["transcript_usd_session"] = round(sum(c for c in map(price_message, messages.values()) if c is not None), 4)
        served_ids = [r["msg_id"] for r in rows if ca.served_by(r) == ca.SERVED_BY_LOCAL and r.get("msg_id") in messages]
        out["phantom_usd_local_turns"] = round(sum(price_message(messages[i]) or 0.0 for i in served_ids), 4)
        if reference_usd is None:
            reference_usd = out["transcript_usd_session"]
    if reference_usd is not None and kind == "forwarded":
        out["check"] = ca.check_forwarded(rows, reference_usd, tolerance)
    return out


def run(ledger_path: Path, projects_dir: Path | None, exclude: set[str], limit: int | None,
        reference: dict[str, float], tolerance: float) -> dict:
    by_session: dict[str, list[dict]] = {}
    for r in ledger.read_rows(ledger_path):
        sid = r.get("session_id")
        if sid and sid not in exclude:
            by_session.setdefault(sid, []).append(r)
    ordered = sorted(by_session, key=lambda k: -len(by_session[k]))
    results = []
    for sid in ordered:
        msgs = None
        if projects_dir is not None:
            found = sorted(projects_dir.glob(f"*/{sid}.jsonl"))
            msgs = transcript_messages(found[0]) if found else None
        if msgs is None and sid not in reference and not any(ca.served_by(r) == "local" for r in by_session[sid]):
            continue  # nothing to compare a forwarded session against
        results.append(reconcile_session(sid, by_session[sid], msgs, reference.get(sid), tolerance))
        if limit and len(results) >= limit:
            break
    checks = [r["check"] for r in results if r.get("check", {}).get("applicable")]
    return {"sessions": results, "checks": len(checks), "failed": sum(1 for c in checks if not c["ok"]),
            "tolerance": tolerance}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", type=Path, default=ledger.ledger_path())
    ap.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--sessions", type=int, default=None, help="largest N sessions only")
    ap.add_argument("--reference-json", type=Path, default=None)
    ap.add_argument("--tolerance", type=float, default=ca.DEFAULT_TOLERANCE)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ref = json.loads(a.reference_json.read_text()) if a.reference_json else {}
    rep = run(a.ledger, a.projects_dir, set(a.exclude), a.sessions, ref, a.tolerance)
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        for r in rep["sessions"]:
            chk = r.get("check") or {}
            verdict = "n/a" if not chk.get("applicable") else ("PASS" if chk["ok"] else "FAIL")
            print(f"{r['session_id'][:8]} {r['kind']:9s} calls={r['calls']:4d} local={r['local']:4d} "
                  f"ledger=${r['ledger_usd']:.4f} joined_ledger=${r.get('ledger_usd_joined', 'na')} "
                  f"joined_transcript=${r.get('transcript_usd_joined', 'na')} "
                  f"not_in_transcript={r.get('ledger_rows_not_in_transcript', 'na')}"
                  f"(${r.get('ledger_usd_not_in_transcript', 'na')}) "
                  f"phantom=${r.get('phantom_usd_local_turns', 'na')} {verdict}")
        print(f"{rep['checks']} applicable check(s), {rep['failed']} failed (tolerance {a.tolerance:.0%})")
    return 1 if rep["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
