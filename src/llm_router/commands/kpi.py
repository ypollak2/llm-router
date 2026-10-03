"""`llm-router kpi` -- the North Star scorecard (KPI-SPEC, adopted 2026-10-03).

See ``~/.rsi/research/kpis/KPI-SPEC.md`` for the twelve KPIs this prints: NS,
O1-O2, D1-D5, G1-G4. PR `feat/kpi-instrumentation` (stacked below this one)
added the fields this command reads: ``session_kind`` on every proxy / edit /
agent-call row, ``tier_proposed`` / ``tier_policy_version`` / ``tier_retry``
on every proxy row, and the used/redone/unknown verdict in
``usage_outcomes.jsonl``.

RULE THIS FILE FOLLOWS THROUGHOUT (project CLAUDE.md): unknown is never
rendered as 0. A KPI with no data, or whose n is below ``MIN_N``, prints
``not measurable: <reason>`` or ``too few to tell (n=N)`` -- never a bare
number standing in for "nothing happened". Every value that IS printed
carries its own n and its own window, because the two scorecards side by side
otherwise look comparable when they counted different things.

SCOPE, stated rather than implied:

* **Organic sessions only by default.** ``session_kind`` (research / harness /
  headless / organic / untagged) was added by the instrumentation PR; most
  history on this machine predates it, so most rows read back ``None``
  (never tagged) and are EXCLUDED by default, same as harness. Pass
  ``--include research`` to also count research-tagged sessions (never
  harness -- that population is a test fixture by construction, see
  ``scripts/groundtruth/sources.py``). An all-None window reports 0 organic
  rows, honestly, rather than silently falling back to "everything".
* **O1 is NOT session-kind filtered.** It reads ``usage.db`` via
  ``dashboard_data.summary()``, the same canonical accessor every other
  savings surface uses (see ``commands/savings_report.py``) -- that table
  predates session tagging and carries no ``session_kind`` column. Shown as
  "est." per the 2026-09-27 display rule, with its own note.
* **O2 and D5 need a frozen benchmark.** They do not come from live traffic.
  Configure ``LLM_ROUTER_KPI_BENCHMARK_PATH`` to a JSON file shaped
  ``{"generated_at": "...", "o2": {"acceptable_rate": 0.0-1.0, "n": int},
  "d5": {"accuracy": 0.0-1.0, "under_route_rate": 0.0-1.0, "n": int}}``
  (``scripts/kpi_benchmark_template.json`` is not shipped; the shape is the
  contract). No path configured, or the file does not parse -> "not measured",
  the label the KPI spec itself uses for this gap.
* **G4 is a point-in-time snapshot**, not a rate. ``provider_reset`` records
  only "blocked until T"; it has no record of whether a block was ever
  mistaken, so "wrongly benched" cannot be computed from it. What IS honest:
  how many providers this process sees as blocked right now, and for how
  long. A human confirms whether that is wrong; this command cannot.
* **G1's hook-side p95 is not instrumented anywhere in this codebase** (no
  module records per-invocation hook wall time to a ledger) -- printed as
  "not measurable: hook latency is not instrumented" rather than silently
  dropped. The proxy-side p95 (``added_latency_s`` in proxy_calls.jsonl) IS
  real and is what this prints.
* **G2** only reports the ``failopen`` counter population. It is an all-time
  total, not a windowed one -- ``fail_open.jsonl`` rows carry no timestamp
  (see ``llm_router.failopen._append``), so it cannot be split to "this
  window" vs "before"; printed as a labelled all-time count, not folded into
  a per-100-calls rate the data cannot support. Broader silent-failure
  classes named in the spec (truncation/overflow, Ollama hung) are not wired
  into this counter and are not claimed here.

``compute_scorecard()`` is the single computation; both the text renderer and
``--json`` read its output, so they cannot disagree with each other (the same
discipline ``northstar.report()`` uses for NS1).

``--write-weekly <dir>`` writes ``kpi-YYYY-MM-DD.md`` into that directory.
Nothing is installed; wiring up a schedule is the operator's choice. A
launchd example (macOS) and a cron example:

    # launchd: ~/Library/LaunchAgents/com.llm-router.kpi-weekly.plist
    #   ProgramArguments: llm-router, kpi, --days, 7, --write-weekly, <dir>
    #   StartCalendarInterval: {Weekday: 1, Hour: 8, Minute: 0}
    # cron:
    #   0 8 * * 1  /path/to/llm-router kpi --days 7 --write-weekly <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MIN_N = 50
TOO_FEW = "too few to tell"

#: KPI codes this file prints, in KPI-SPEC order. Used by the renderer and by
#: tests that assert nothing silently drops a row.
KPI_CODES = ("NS", "O1", "O2", "D1", "D2", "D3", "D4", "D5", "G1", "G2", "G3", "G4")


def _not_measurable(reason: str) -> dict[str, Any]:
    return {"value": f"not measurable: {reason}", "n": None, "measurable": False}


def _too_few(n: int) -> dict[str, Any]:
    return {"value": f"{TOO_FEW} (n={n})", "n": n, "measurable": False}


def _measured(value: str, n: int, **extra: Any) -> dict[str, Any]:
    out = {"value": value, "n": n, "measurable": True}
    out.update(extra)
    return out


def _pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def _rate_result(numerator: int, denominator: int, *, label: str = "n") -> dict[str, Any]:
    if denominator <= 0:
        return _not_measurable(f"no {label} in window")
    if denominator < MIN_N:
        return _too_few(denominator)
    return _measured(f"{_pct(numerator / denominator)} (n={denominator})", denominator,
                      numerator=numerator, denominator=denominator)


# ── session-kind filtering ────────────────────────────────────────────────────

def _allowed_kinds(include_research: bool) -> frozenset[str]:
    from llm_router import session_kind as sk

    return frozenset({sk.KIND_ORGANIC, sk.KIND_RESEARCH}) if include_research \
        else frozenset({sk.KIND_ORGANIC})


# ── NS, D1, D2: from northstar's unit stream, session-kind filtered ─────────

def _ns_d1_d2(days: int, allowed: frozenset[str]) -> tuple[dict, dict, dict]:
    """Pooled (not per-session-median) totals over units whose session is
    tagged into ``allowed``. Mirrors the attempted/used accounting
    ``northstar.report()`` already uses, so NS/D1/D2 cannot disagree with
    what ``llm-router northstar`` shows for the same sessions."""
    from llm_router import northstar as ns
    from llm_router import session_kind as sk

    total = attempted = used = untagged = 0
    kind_cache: dict[str, str | None] = {}
    for u in ns.units(days=days):
        sid = u["session_id"]
        kind = kind_cache.get(sid, "_miss")
        if kind == "_miss":
            kind = sk.kind_of(sid)
            kind_cache[sid] = kind
        if kind not in allowed:
            if kind is None:
                untagged += 1
            continue
        total += 1
        is_attempted = u["kind"] in ns.ATTEMPTED_KINDS or u.get("lever") == "proxy"
        if is_attempted:
            attempted += 1
            if u["outcome"] == ns.OUTCOME_USED:
                used += 1
    if total == 0 and untagged:
        why = (f"{untagged} unit(s) in window, all from sessions with no session-kind tag "
               "(tagging starts once the SessionStart hook is deployed); untagged is never "
               "counted as organic")
        return _not_measurable(why), _not_measurable(why), _not_measurable(why)
    ns_result = _rate_result(used, total, label="unit")
    d1_result = _rate_result(attempted, total, label="unit")
    d2_result = _rate_result(used, attempted, label="attempt")
    return ns_result, d1_result, d2_result


# ── D3: redo rate, from usage_outcome ────────────────────────────────────────

def _d3_redo_rate(days: int, allowed: frozenset[str]) -> dict:
    from llm_router import usage_outcome as uo

    rows = uo.judge_recent(days=days)
    used = redone = unknown = 0
    for r in rows:
        if r.get("session_kind") not in allowed:
            continue
        if r["outcome"] == uo.OUTCOME_USED:
            used += 1
        elif r["outcome"] == uo.OUTCOME_REDONE:
            redone += 1
        else:
            unknown += 1
    decided = used + redone
    result = _rate_result(redone, decided, label="decided event")
    result["unknown_window_open"] = unknown
    return result


# ── D4: tier mix, G1/G3: proxy ledger ────────────────────────────────────────

def _proxy_rows(days: int, allowed: frozenset[str]) -> list[dict]:
    from llm_router.proxy import ledger as pl

    rows = pl.read_rows(days=days)
    return [r for r in rows if r.get("session_kind") in allowed]


def _d4_tier_mix(rows: list[dict]) -> dict:
    tiered = [r for r in rows if r.get("tier")]
    if not tiered:
        return _not_measurable("no tiered proxy calls in window")
    if len(tiered) < MIN_N:
        return _too_few(len(tiered))
    mix: dict[str, int] = {}
    cost_by_tier: dict[str, float] = {}
    for r in tiered:
        t = r["tier"]
        mix[t] = mix.get(t, 0) + 1
        cost = None
        try:
            from llm_router.proxy.ledger import anthropic_cost
            cost = anthropic_cost(r)
        except Exception:  # noqa: BLE001 -- a missing cost is dropped, not zeroed
            cost = None
        if cost is not None:
            cost_by_tier[t] = cost_by_tier.get(t, 0.0) + cost
    n = len(tiered)
    parts = ", ".join(f"{t}={_pct(c / n)}" for t, c in sorted(mix.items(), key=lambda kv: -kv[1]))
    return _measured(f"{parts} (n={n})", n, calls_by_tier=mix,
                      cost_usd_by_tier={k: round(v, 4) for k, v in cost_by_tier.items()})


def _g1_latency(rows: list[dict]) -> tuple[dict, dict]:
    hook_result = _not_measurable("hook latency is not instrumented (no ledger records it)")
    served_or_tried = [r for r in rows if isinstance(r.get("added_latency_s"), (int, float))]
    if not served_or_tried:
        return hook_result, _not_measurable("no proxy decisions with added_latency_s in window")
    values = sorted(r["added_latency_s"] for r in served_or_tried)
    n = len(values)
    if n < MIN_N:
        return hook_result, _too_few(n)
    k = max(0, min(n - 1, round(0.95 * (n - 1))))
    p95 = values[k]
    gate = "within +200ms gate" if p95 <= 0.2 else "OVER the +200ms gate"
    return hook_result, _measured(f"proxy decision p95={p95 * 1000:.0f}ms ({gate}) (n={n})", n,
                                   p95_s=round(p95, 4))


def _g3_completeness(rows: list[dict]) -> dict:
    required = ("session_kind", "tier_proposed", "tier_policy_version", "tier_retry")
    if not rows:
        return _not_measurable("no proxy rows in window")
    if len(rows) < MIN_N:
        return _too_few(len(rows))
    complete = sum(1 for r in rows if all(k in r for k in required))
    return _rate_result(complete, len(rows), label="proxy row")


# ── O1: quota avoided (est.), not session-kind filtered ──────────────────────

def _period_for_days(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 9:
        return "week"
    if days <= 35:
        return "month"
    return "all"


def _o1_quota_avoided(days: int) -> dict:
    try:
        from llm_router.dashboard_data import summary

        period = _period_for_days(days)
        s = summary(period)
        n = s.estimated_n
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen
        failopen.record("CHZ-FO-KPI-O1-SUMMARY", exc)
        return _not_measurable("dashboard_data.summary() raised; see llm-router doctor")
    if n == 0:
        return _not_measurable(f"no routed calls with a recorded saving in period={period}")
    display = s.display()
    note = (f"{display} [period={period}; NOT session-kind filtered -- usage.db predates "
            "session tagging]")
    if n < MIN_N:
        return _too_few(n) | {"value": f"{TOO_FEW} ({display}, n={n})"}
    return _measured(note, n, estimated_usd=s.estimated_usd)


# ── O2 / D5: frozen benchmark only ────────────────────────────────────────────

def _benchmark_path() -> Path | None:
    raw = os.environ.get("LLM_ROUTER_KPI_BENCHMARK_PATH", "").strip()
    return Path(raw).expanduser() if raw else None


def _load_benchmark() -> dict | None:
    path = _benchmark_path()
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _o2_quality_held(bench: dict | None) -> dict:
    if bench is None:
        return _not_measurable(
            "no LLM_ROUTER_KPI_BENCHMARK_PATH configured (KPI-SPEC calls this 'not measured')")
    o2 = bench.get("o2") if isinstance(bench, dict) else None
    if not isinstance(o2, dict) or "acceptable_rate" not in o2:
        return _not_measurable("benchmark file has no 'o2' section")
    n = o2.get("n")
    if not isinstance(n, int) or n <= 0:
        return _not_measurable("benchmark 'o2.n' missing or zero")
    if n < MIN_N:
        return _too_few(n)
    return _measured(f"{_pct(o2['acceptable_rate'])} acceptable vs Claude (n={n}, frozen set)", n,
                      generated_at=bench.get("generated_at"))


def _d5_classifier_accuracy(bench: dict | None) -> dict:
    if bench is None:
        return _not_measurable("no LLM_ROUTER_KPI_BENCHMARK_PATH configured (KPI-SPEC calls this 'not measured')")
    d5 = bench.get("d5") if isinstance(bench, dict) else None
    if not isinstance(d5, dict) or "accuracy" not in d5:
        return _not_measurable("benchmark file has no 'd5' section")
    n = d5.get("n")
    if not isinstance(n, int) or n <= 0:
        return _not_measurable("benchmark 'd5.n' missing or zero")
    if n < MIN_N:
        return _too_few(n)
    under = d5.get("under_route_rate")
    under_s = f", under-route={_pct(under)}" if isinstance(under, (int, float)) else ""
    gate = "" if not isinstance(under, (int, float)) else (
        " (within <=10% gate)" if under <= 0.10 else " (OVER the <=10% gate)")
    return _measured(f"{_pct(d5['accuracy'])} exact-tier accuracy{under_s}{gate} (n={n})", n,
                      generated_at=bench.get("generated_at"))


# ── G2: failopen, all-time only ───────────────────────────────────────────────

def _g2_silent_failures() -> dict:
    try:
        from llm_router import failopen

        snap = failopen.snapshot()
    except Exception as exc:  # noqa: BLE001
        return _not_measurable(f"failopen.snapshot() raised: {type(exc).__name__}")
    total = snap.total
    if total is None:
        return _not_measurable("fail-open store is present but unreadable")
    if total == 0:
        return _not_measurable(
            "no fail-open events recorded (cannot tell 'none occurred' from 'not recording')")
    return _measured(
        f"{total} fail-open event(s) recorded, ALL-TIME (no per-event timestamp -- "
        "cannot be windowed or turned into a per-100-calls rate)",
        total, all_time_total=total, by_code=dict(sorted(snap.by_code.items(), key=lambda kv: -kv[1])[:8]),
    )


# ── G4: provider_reset, point-in-time ──────────────────────────────────────────

def _g4_wrongly_benched() -> dict:
    try:
        from llm_router import provider_reset

        resets = provider_reset.all_provider_resets()
    except Exception as exc:  # noqa: BLE001
        return _not_measurable(f"provider_reset read raised: {type(exc).__name__}")
    n = len(resets)
    if n == 0:
        return _measured("0 providers currently benched (point-in-time; cannot confirm "
                          "any PAST bench was wrong)", 0)
    names = ", ".join(sorted(resets))
    return _measured(f"{n} provider(s) currently benched: {names} (point-in-time snapshot -- "
                      "whether a bench is WRONG needs a human to say so)", n, benched=sorted(resets))


# ── assembly ───────────────────────────────────────────────────────────────

def compute_scorecard(days: int = 7, *, include_research: bool = False) -> dict:
    allowed = _allowed_kinds(include_research)
    ns_r, d1_r, d2_r = _ns_d1_d2(days, allowed)
    d3_r = _d3_redo_rate(days, allowed)
    rows = _proxy_rows(days, allowed)
    d4_r = _d4_tier_mix(rows)
    g1_hook_r, g1_proxy_r = _g1_latency(rows)
    g3_r = _g3_completeness(rows)
    o1_r = _o1_quota_avoided(days)
    bench = _load_benchmark()
    o2_r = _o2_quality_held(bench)
    d5_r = _d5_classifier_accuracy(bench)
    g2_r = _g2_silent_failures()
    g4_r = _g4_wrongly_benched()
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_days": days,
        "include_research": include_research,
        "kpis": {
            "NS": ns_r, "O1": o1_r, "O2": o2_r,
            "D1": d1_r, "D2": d2_r, "D3": d3_r, "D4": d4_r, "D5": d5_r,
            "G1_hook": g1_hook_r, "G1_proxy": g1_proxy_r, "G2": g2_r, "G3": g3_r, "G4": g4_r,
        },
    }


_LABELS = {
    "NS": "NS  non-Claude-and-used share (target >80%)",
    "O1": "O1  quota avoided",
    "O2": "O2  quality held",
    "D1": "D1  offered off Claude",
    "D2": "D2  success when tried",
    "D3": "D3  redo rate",
    "D4": "D4  tier mix",
    "D5": "D5  classifier accuracy",
    "G1_hook": "G1  added latency (hook)",
    "G1_proxy": "G1  added latency (proxy)",
    "G2": "G2  silent failures",
    "G3": "G3  ledger completeness (target >=99%)",
    "G4": "G4  wrongly benched providers",
}
_ORDER = ("NS", "O1", "O2", "D1", "D2", "D3", "D4", "D5", "G1_hook", "G1_proxy", "G2", "G3", "G4")


def render_scorecard(data: dict) -> str:
    lines = [
        f"llm-router kpi -- window={data['window_days']}d "
        f"({'organic + research' if data['include_research'] else 'organic only'}) "
        f"generated={data['generated_at']}",
        "",
    ]
    for key in _ORDER:
        r = data["kpis"][key]
        lines.append(f"  {_LABELS[key]:<42s} {r['value']}")
    lines.append("")
    lines.append("O1 is never session-kind filtered (usage.db predates tagging). "
                  "O2/D5 need LLM_ROUTER_KPI_BENCHMARK_PATH. G2/G4 are not windowed "
                  "the same way as the rest -- see each line and the module docstring.")
    return "\n".join(lines)


def write_weekly(data: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y-%m-%d", time.gmtime())
    path = out_dir / f"kpi-{date}.md"
    body = ["# llm-router KPI scorecard", "",
            f"Generated {data['generated_at']}, window {data['window_days']}d, "
            f"{'organic + research' if data['include_research'] else 'organic only'}.",
            "", "| KPI | Value |", "|---|---|"]
    for key in _ORDER:
        r = data["kpis"][key]
        body.append(f"| {_LABELS[key]} | {r['value']} |")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def cmd_kpi(args: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="llm-router kpi", add_help=True)
    ap.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    ap.add_argument("--include", choices=("research",), default=None,
                     help="also count research-tagged sessions (harness is never included)")
    ap.add_argument("--write-weekly", metavar="DIR", default=None,
                     help="write a dated markdown scorecard into DIR")
    ap.add_argument("--json", action="store_true", help="print the raw scorecard dict as JSON")
    parsed = ap.parse_args(args)

    data = compute_scorecard(days=parsed.days, include_research=(parsed.include == "research"))

    if parsed.json:
        print(json.dumps(data, indent=2, sort_keys=False, default=str))
    else:
        print(render_scorecard(data))

    if parsed.write_weekly:
        path = write_weekly(data, Path(parsed.write_weekly))
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(cmd_kpi(sys.argv[1:]))
