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
from pathlib import Path

HOME = Path.home() / ".llm-router"


def _pctl(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _outcome_counts(log: Path) -> dict[str, int]:
    keys = ["DIRECT SUCCESS", "DIRECT SKIP", "DIRECT FAILED", "AGENT LOOP SUCCESS"]
    counts = {k: 0 for k in keys}
    if log.exists():
        text = log.read_text(errors="ignore")
        for k in keys:
            counts[k] = len(re.findall(re.escape(k), text))
    return counts


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
