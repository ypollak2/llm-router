"""Routing observability report — a deep-dive of what LLM Router actually routed.

Joins the runtime ledgers into one markdown report:
  * ``usage.db``               — per-model calls, tokens in/out, latency, saved $
  * ``auto-route-debug.log``   — routing-outcome matrix (DIRECT success/skip/failed)
  * ``enforcement.log``        — overrides (the model did the work itself)

Run:  python -m llm_router.routing_report   →  writes ~/.llm-router/routing_report.md
"""
from __future__ import annotations

import re
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home() / ".llm-router"


def _pctl(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


_START_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION START\] ID=([\d.]+)")
_LINE_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d) [\d:]+\] \[INVOCATION ([\d.]+)\] (.*)$")
_SESSION_RE = re.compile(r"session_id=(\S*)")

# A session id the hook could not resolve. The hook ran, but this is not a user
# prompt — same reason an empty id is excluded, which is what the test suite writes.
_NON_SESSIONS = {"", "unknown", "none", "None"}


class Outcome(str, Enum):
    SUCCESS = "success"      # a local model answered
    FAILED = "failed"        # it was tried and could not
    SKIPPED = "skipped"      # the gate declined to try
    OTHER = "other"          # no terminal line: crashed, truncated, still running


# Terminal markers, most specific first. A rejected draft is NOT a success: S2-6
# discarded it and the turn fell through to Claude.
_TERMINAL = (
    ("DRAFT REJECTED", Outcome.FAILED),
    ("DIRECT SUCCESS", Outcome.SUCCESS),
    ("DIRECT FAILED", Outcome.FAILED),
    ("DIRECT SKIP", Outcome.SKIPPED),
)

# Annotations — they describe WHY an invocation went the way it did, and are
# reported alongside rather than as outcomes. Counting a rescue as an outcome would
# double-count the invocation it belongs to.
_ANNOTATIONS = (
    ("SESSION RESCUE", "session_rescue"),
    ("OKF RESCUE", "okf_rescue"),
    ("DRAFT REJECTED", "rejected"),
    ("PERSISTED", "turns_persisted"),
)


def parse_log(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    """One record per invocation, keyed by id. Malformed lines are skipped."""
    records: dict[str, dict[str, Any]] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        m = _START_RE.match(line)
        if m:
            day, iid = m.group(1), m.group(2)
            records.setdefault(iid, {"day": day, "session": None,
                                     "outcome": None, "notes": set()})
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        day, iid, rest = m.group(1), m.group(2), m.group(3)
        rec = records.setdefault(iid, {"day": day, "session": None,
                                       "outcome": None, "notes": set()})
        s = _SESSION_RE.search(rest)
        if s:
            rec["session"] = s.group(1)
        for marker, note in _ANNOTATIONS:
            if marker in rest:
                rec["notes"].add(note)
        if rec["outcome"] is None:
            # First terminal line wins: a line can repeat on retry, the outcome
            # cannot.
            for marker, outcome in _TERMINAL:
                if rest.startswith(marker) or f"] {marker}" in rest or marker in rest.split(":")[0]:
                    rec["outcome"] = outcome
                    break
    return records


def summarise(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-day counts over real user prompts only."""
    days: dict[str, dict[str, Any]] = {}
    for rec in records.values():
        if rec.get("session") in _NON_SESSIONS or rec.get("session") is None:
            continue
        d = days.setdefault(rec["day"], {
            "prompts": 0, "success": 0, "failed": 0, "skipped": 0, "other": 0,
            "session_rescue": 0, "okf_rescue": 0, "rejected": 0,
            "turns_persisted": 0, "rate": 0.0,
        })
        d["prompts"] += 1
        d[(rec["outcome"] or Outcome.OTHER).value] += 1
        for note in rec["notes"]:
            d[note] += 1
    for d in days.values():
        d["rate"] = (d["success"] / d["prompts"]) if d["prompts"] else 0.0
    return days


def _outcome_counts(log: Path) -> dict[str, int]:
    """Outcome matrix over REAL user prompts, one outcome per invocation.

    Counted raw line occurrences before. That included the test suite — 227
    invocations carrying `chain=['ollama/fake-model']` and an empty session id once
    sat in this log — and it could report more successes than prompts, because a
    DIRECT SUCCESS invocation never reaches OUTPUT COMPLETE, so the two were
    disjoint sets. `parse_log` groups by invocation and `summarise` drops
    non-sessions, so the numbers reconcile with the per-day view and with each
    other.
    """
    if not log.exists():
        return {k: 0 for k in ("DIRECT SUCCESS", "DIRECT SKIP", "DIRECT FAILED")}
    with log.open(encoding="utf-8", errors="ignore") as fh:
        days = summarise(parse_log(fh))
    return {
        "DIRECT SUCCESS": sum(d["success"] for d in days.values()),
        "DIRECT SKIP": sum(d["skipped"] for d in days.values()),
        "DIRECT FAILED": sum(d["failed"] for d in days.values()),
    }


def _violations(log: Path) -> int:
    return len(re.findall(r"VIOLATION", log.read_text(errors="ignore"))) if log.exists() else 0


def generate_report() -> str:
    db = HOME / "usage.db"
    if not db.exists():
        return "# LLM Router routing report\n\n(no usage.db yet — nothing has routed)\n"

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """SELECT model, provider, COUNT(*) n,
                      SUM(input_tokens) tin, SUM(output_tokens) tout,
                      ROUND(AVG(latency_ms)) avg_ms, MAX(latency_ms) max_ms,
                      ROUND(SUM(COALESCE(saved_usd,0)), 5) saved
               FROM usage GROUP BY model, provider ORDER BY n DESC"""
        ).fetchall()
        lats = [r[0] for r in con.execute(
            "SELECT latency_ms FROM usage WHERE latency_ms > 0").fetchall()]
        # routing source breakdown (gateway vs hook vs mcp) if reason_code exists
        try:
            srcs = con.execute(
                "SELECT COALESCE(classifier_type,'?'), COUNT(*) FROM routing_decisions "
                "GROUP BY 1 ORDER BY 2 DESC").fetchall()
        except sqlite3.Error:
            srcs = []
    finally:
        con.close()

    total_calls = sum(r[2] for r in rows)
    total_in = sum(r[3] or 0 for r in rows)
    total_out = sum(r[4] or 0 for r in rows)
    total_saved = sum(r[7] or 0 for r in rows)

    L = ["# LLM Router routing report", "", "## Summary",
         f"- **Routed calls:** {total_calls:,}",
         f"- **Tokens routed:** {total_in:,} in · {total_out:,} out · {total_in + total_out:,} total",
         f"- **Estimated saved (vs baseline):** ${total_saved:.4f}",
         f"- **Latency:** p50 {_pctl(lats, 50)/1000:.1f}s · p95 {_pctl(lats, 95)/1000:.1f}s · "
         f"max {(max(lats) if lats else 0)/1000:.1f}s", ""]

    L += ["## By model", "",
          "| model | provider | calls | tok in | tok out | avg ms | max ms | saved $ |",
          "|---|---|--:|--:|--:|--:|--:|--:|"]
    for model, provider, n, tin, tout, avg_ms, max_ms, saved in rows:
        L.append(f"| {model} | {provider} | {n} | {tin or 0:,} | {tout or 0:,} | "
                 f"{int(avg_ms or 0):,} | {int(max_ms or 0):,} | {saved or 0:.5f} |")
    L.append("")

    if srcs:
        L += ["## Routed via", "", "| source | calls |", "|---|--:|"]
        L += [f"| {s} | {n} |" for s, n in srcs] + [""]

    oc = _outcome_counts(HOME / "auto-route-debug.log")
    tot = sum(oc.values()) or 1
    L += ["## Routing outcomes (Claude Code hook path)", "",
          "| outcome | count | % |", "|---|--:|--:|"]
    L += [f"| {k} | {v} | {100*v/tot:.0f}% |" for k, v in oc.items()]
    L += [f"| overrides (model did the work) | {_violations(HOME / 'enforcement.log')} | — |", ""]

    if lats:
        slow = len([x for x in lats if x > 15000])
        L += ["## Latency note", "",
              f"- {slow} call(s) > 15s — typically Ollama model-swap cold-loads. "
              "Set `OLLAMA_MAX_LOADED_MODELS≥2` + `OLLAMA_KEEP_ALIVE=-1` to avoid.", ""]

    return "\n".join(L)


def main() -> None:
    report = generate_report()
    out = HOME / "routing_report.md"
    out.write_text(report)
    print(f"Wrote {out}\n")
    print(report)


if __name__ == "__main__":
    main()
