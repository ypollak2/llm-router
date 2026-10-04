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
  history on this machine predates it, so most records read back ``None``
  (never tagged) and are EXCLUDED by default, same as harness. Pass
  ``--include research`` to also count research-tagged sessions (never
  harness -- that population is a test fixture by construction, see
  ``scripts/groundtruth/sources.py``). An all-None window reports 0 organic
  rows, honestly, rather than silently falling back to "everything".
  A unit's or event's kind is resolved by ``session_kind.KindIndex``: the
  session's tag file first, then the kind stamped on the record, then the kind
  stamped on that session's proxy rows. NS/D1/D2 units and D3 events go through
  it; the scorecard prints how many units joined a tag and how many stayed
  untagged (``joins``). D4/G1 read the kind each proxy row was written with:
  rows from before tagging existed stay untagged, so D4's population is the
  tagged rows only, as before.
* **G3 is NOT session-kind filtered.** Completeness is a property of the writer,
  and the session tag is one of the fields under test: filtering rows by
  ``session_kind`` first drops exactly the rows that lack it, so the field read
  100% by construction. G3 counts every row in the window, per row type and per
  field, from the schema's start date (``g3_*`` below, KPIS.md).
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
discipline ``northstar.report()`` uses for NS1). ``--health`` is a third reader of
the same dict: per KPI it prints ``measured``, ``blind`` or ``stale``, the one
reason, and the n; a KPI is stale when its newest data point is older than
``STALE_LIVE_HOURS`` (live ledgers) or ``STALE_BENCHMARK_DAYS`` (frozen
benchmark). Exit code 0 even when KPIs are blind; ``--strict`` exits 1 if any is.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm_router import session_kind

MIN_N = 50
TOO_FEW = "too few to tell"

#: ``--health``: a KPI whose newest data point is older than this is "stale".
#: Live ledgers (units, proxy rows, outcome events, usage.db) turn over daily on a
#: working machine, so two days without a new point means the feed stopped. The
#: frozen benchmark behind O2/D5 is a snapshot by design and is judged on a month.
STALE_LIVE_HOURS = 48.0
STALE_BENCHMARK_DAYS = 30.0

#: KPI codes this file prints, in KPI-SPEC order. Used by the renderer and by
#: tests that assert nothing silently drops a row.
KPI_CODES = ("NS", "O1", "O2", "D1", "D2", "D3", "D4", "D5", "G1", "G2", "G3", "G4")

#: Per-call Claude quota cost relative to Haiku, for D4's cost-weighted share. From
#: the header of ``proxy/claude_tiers.yaml`` (Phase 0.4 probe, 2026-09-29, n=5 calls
#: per model): "Opus cost 1.68x Sonnet and 6.15x Haiku per call on the same prompt".
#: ``tests/test_kpi_definitions.py`` parses that comment and fails if these drift.
#: A tier with no weight here (fable) is left out of the weighted share and counted
#: separately, never given a guessed weight.
TIER_COST_WEIGHT = {"haiku": 1.0, "sonnet": 6.15 / 1.68, "opus": 6.15}

#: G3's fields. Written together by one code path (``proxy/server.py handle``), so one
#: schema start covers all four. ``tier_retry`` is an OUTCOME: null is its legal value
#: ("no retry happened"), so only its presence is required; the other three must be
#: non-null wherever they can apply.
G3_FIELDS = ("session_kind", "tier_policy_version", "tier_proposed", "tier_retry")
_G3_PRESENCE_ONLY = frozenset({"tier_retry"})


def _not_measurable(reason: str, *, seen: int = 0, newest_ts: float | None = None) -> dict[str, Any]:
    return {"value": f"not measurable: {reason}", "n": None, "measurable": False,
            "reason": reason, "seen": seen, "newest_ts": newest_ts}


def _too_few(n: int, *, newest_ts: float | None = None) -> dict[str, Any]:
    return {"value": f"{TOO_FEW} (n={n})", "n": n, "measurable": False,
            "reason": f"{TOO_FEW}: n={n}, need {MIN_N}", "seen": n, "newest_ts": newest_ts}


def _measured(value: str, n: int, *, newest_ts: float | None = None, **extra: Any) -> dict[str, Any]:
    out = {"value": value, "n": n, "measurable": True, "seen": n, "newest_ts": newest_ts}
    out.update(extra)
    return out


def _pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def _rate_result(numerator: int, denominator: int, *, label: str = "n",
                 newest_ts: float | None = None, seen: int | None = None) -> dict[str, Any]:
    seen_n = seen if seen is not None else denominator
    if denominator <= 0:
        return _not_measurable(f"no {label} in window", seen=seen_n)
    if denominator < MIN_N:
        out = _too_few(denominator, newest_ts=newest_ts)
        out["seen"] = seen_n
        return out
    return _measured(f"{_pct(numerator / denominator)} (n={denominator})", denominator,
                      newest_ts=newest_ts, seen=seen_n,
                      numerator=numerator, denominator=denominator)


def _num_ts(raw: Any) -> float | None:
    """``raw`` as epoch seconds when it is a real number, else None (a bool is not a time)."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


def _parse_ts(raw: Any) -> float | None:
    """Epoch seconds from an epoch number or an ISO-8601 string (naive = UTC), else None."""
    num = _num_ts(raw)
    if num is not None:
        return num
    if isinstance(raw, str):
        try:
            when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts is not None else None


def _newer(current: float | None, ts: float | None) -> float | None:
    if ts is None:
        return current
    return ts if current is None else max(current, ts)


# ── session-kind filtering ────────────────────────────────────────────────────

def _allowed_kinds(include_research: bool) -> frozenset[str]:
    from llm_router import session_kind as sk

    return frozenset({sk.KIND_ORGANIC, sk.KIND_RESEARCH}) if include_research \
        else frozenset({sk.KIND_ORGANIC})


def _scope_phrase(allowed: frozenset[str]) -> str:
    return "an organic or research" if len(allowed) > 1 else "an organic"


# ── NS, D1, D2: from northstar's unit stream, session-kind joined ───────────

def _ns_d1_d2(days: int, allowed: frozenset[str], index) -> tuple[dict, dict, dict, dict]:
    """Pooled (not per-session-median) totals over units whose session resolves to a
    kind in ``allowed``. Mirrors the attempted/used accounting
    ``northstar.report()`` already uses, so NS/D1/D2 cannot disagree with
    what ``llm-router northstar`` shows for the same sessions.

    ``northstar.units()`` stamps each unit with its session's kind (tag file, then
    the unit's own ledger stamp, then the session's proxy rows); a stream without the
    stamp is resolved here against ``index``. Returns the three results and the join
    counts: how many units found a tag and how many stayed untagged."""
    from llm_router import northstar as ns

    window = joined = untagged = conflicting = total = attempted = used = 0
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_session: dict[str, int] = {}  # counted units per session: how concentrated the population is
    disagree: set[str] = set()
    checked: set[str] = set()
    newest: float | None = None
    for u in ns.units(days=days):
        window += 1
        sid = u.get("session_id")
        if "session_kind" in u:
            kind, source = u["session_kind"], u.get("session_kind_source")
        else:
            res = index.resolve(sid)
            kind, source = res.kind, res.source
        if isinstance(sid, str) and sid not in checked:
            checked.add(sid)
            if index.resolve(sid).ledger_disagrees:
                disagree.add(sid)
        if kind is None:
            untagged += 1
            conflicting += 1 if source == "conflict" else 0
            continue
        joined += 1
        by_source[source or "unknown"] = by_source.get(source or "unknown", 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if kind not in allowed:
            continue
        total += 1
        by_session[str(sid)] = by_session.get(str(sid), 0) + 1
        newest = _newer(newest, _parse_ts(u.get("ts")))
        is_attempted = u["kind"] in ns.ATTEMPTED_KINDS or u.get("lever") == "proxy"
        if is_attempted:
            attempted += 1
            if u["outcome"] == ns.OUTCOME_USED:
                used += 1
    joins = {"window_units": window, "joined": joined, "untagged": untagged,
             "untagged_conflicting": conflicting, "joined_by_source": by_source,
             "joined_by_kind": by_kind, "counted": total, "counted_sessions": len(by_session),
             "largest_session_share": round(max(by_session.values()) / total, 4) if total else None,
             "sessions_where_tag_and_proxy_rows_disagree": len(disagree)}
    if total == 0:
        if window == 0:
            why = "no unit in window"
        elif joined == 0:
            why = (f"{window} unit(s) in window, all from sessions with no session-kind tag "
                   "(tagging starts once the SessionStart hook is deployed); untagged is never "
                   "counted as organic")
        else:
            kinds = ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]))
            why = (f"{window} unit(s) in window, none from {_scope_phrase(allowed)} session: "
                   f"{joined} joined to a tag ({kinds}), {untagged} untagged (never counted as organic)")
        out = _not_measurable(why, seen=window)
        return out, dict(out), dict(out), joins
    return (_rate_result(used, total, label="unit", newest_ts=newest, seen=window),
            _rate_result(attempted, total, label="unit", newest_ts=newest, seen=window),
            _rate_result(used, attempted, label="attempt", newest_ts=newest, seen=window),
            joins)


# ── D3: redo rate, from usage_outcome ────────────────────────────────────────

def _d3_redo_rate(days: int, allowed: frozenset[str], index) -> dict:
    from llm_router import usage_outcome as uo

    rows = uo.judge_recent(days=days)
    used = redone = unknown = 0
    newest: float | None = None
    for r in rows:
        if index.resolve(r.get("session_id"), stamp=r.get("session_kind")).kind not in allowed:
            continue
        if r["outcome"] == uo.OUTCOME_USED:
            used += 1
        elif r["outcome"] == uo.OUTCOME_REDONE:
            redone += 1
        else:
            unknown += 1
            continue
        newest = _newer(newest, _num_ts(r.get("ts")))
    decided = used + redone
    result = _rate_result(redone, decided, label="decided event", newest_ts=newest, seen=len(rows))
    result["unknown_window_open"] = unknown
    return result


# ── proxy ledger: the rows behind D4 and G1 ──────────────────────────────────

def _proxy_population(all_rows: list[dict], days: int, allowed: frozenset[str], now: float) -> dict:
    """The proxy rows in the window, and the subset stamped with an allowed kind. A row's
    kind is the one it was WRITTEN with: rows from before tagging carry none and stay
    untagged (joining today's tag onto rows from before tagging existed would change
    D4's population, not its definition). Rows with no usable ``ts`` cannot be placed
    in a window and are counted, not dropped silently."""
    cutoff = now - days * 86400 if days else None
    window: list[dict] = []
    undated = 0
    for r in all_rows:
        ts = _num_ts(r.get("ts"))
        if ts is None:
            undated += 1
        elif cutoff is None or ts >= cutoff:
            window.append(r)
    kept: list[dict] = []
    untagged = other = 0
    for r in window:
        kind = r.get("session_kind")
        if kind not in session_kind.VALID_KINDS:
            untagged += 1
        elif kind in allowed:
            kept.append(r)
        else:
            other += 1
    return {"window": window, "allowed": kept, "untagged": untagged, "other_kind": other,
            "undated": undated, "scope": _scope_phrase(allowed)}


def _d4_tier_mix(pop: dict) -> dict:
    """Tier mix of Claude calls, by call count and weighted by per-call quota cost
    (``TIER_COST_WEIGHT``). Population: organic sessions by default; research and
    harness are out unless ``--include research`` (harness never)."""
    seen = len(pop["window"])
    tiered = [r for r in pop["allowed"] if r.get("tier")]
    if not tiered:
        return _not_measurable(
            f"no tiered proxy call from {pop['scope']} session in window "
            f"({pop['untagged']} untagged and {pop['other_kind']} other-kind row(s) excluded)", seen=seen)
    if len(tiered) < MIN_N:
        out = _too_few(len(tiered))
        out["seen"] = seen
        return out
    mix: dict[str, int] = {}
    cost_by_tier: dict[str, float] = {}
    newest: float | None = None
    for r in tiered:
        t = r["tier"]
        mix[t] = mix.get(t, 0) + 1
        newest = _newer(newest, _num_ts(r.get("ts")))
        cost = None
        try:
            from llm_router.proxy.ledger import anthropic_cost
            cost = anthropic_cost(r)
        except Exception:  # noqa: BLE001 -- a missing cost is dropped, not zeroed
            cost = None
        if cost is not None:
            cost_by_tier[t] = cost_by_tier.get(t, 0.0) + cost
    n = len(tiered)
    calls = ", ".join(f"{t}={_pct(c / n)}" for t, c in sorted(mix.items(), key=lambda kv: -kv[1]))
    weighted = {t: TIER_COST_WEIGHT[t] * c for t, c in mix.items() if t in TIER_COST_WEIGHT}
    unweighted = sum(c for t, c in mix.items() if t not in TIER_COST_WEIGHT)
    total_w = sum(weighted.values())
    shares = {t: w / total_w for t, w in weighted.items()} if total_w > 0 else {}
    weights = " : ".join(f"{t} {w:.2f}".replace(".00", "") for t, w in TIER_COST_WEIGHT.items())
    if shares:
        cw = (f"cost-weighted ({weights} per call): "
              + ", ".join(f"{t}={_pct(v)}" for t, v in sorted(shares.items(), key=lambda kv: -kv[1])))
    else:
        cw = "cost-weighted: not measurable (no tier with a known weight)"
    if unweighted:
        cw += f" [{unweighted} call(s) on an unweighted tier left out]"
    return _measured(f"{calls} (n={n}) | {cw}", n, newest_ts=newest, seen=seen,
                      calls_by_tier=mix, cost_weighted_share={t: round(v, 4) for t, v in shares.items()},
                      cost_weights=dict(TIER_COST_WEIGHT), unweighted_calls=unweighted,
                      cost_usd_by_tier={k: round(v, 4) for k, v in cost_by_tier.items()})


def _g1_latency(pop: dict) -> tuple[dict, dict]:
    hook_result = _not_measurable("hook latency is not instrumented (no ledger records it)")
    seen = len(pop["window"])
    served_or_tried = [r for r in pop["allowed"] if isinstance(r.get("added_latency_s"), (int, float))]
    if not served_or_tried:
        return hook_result, _not_measurable("no proxy decisions with added_latency_s in window", seen=seen)
    values = sorted(r["added_latency_s"] for r in served_or_tried)
    newest: float | None = None
    for r in served_or_tried:
        newest = _newer(newest, _num_ts(r.get("ts")))
    n = len(values)
    if n < MIN_N:
        out = _too_few(n, newest_ts=newest)
        out["seen"] = seen
        return hook_result, out
    k = max(0, min(n - 1, round(0.95 * (n - 1))))
    p95 = values[k]
    gate = "within +200ms gate" if p95 <= 0.2 else "OVER the +200ms gate"
    return hook_result, _measured(f"proxy decision p95={p95 * 1000:.0f}ms ({gate}) (n={n})", n,
                                   newest_ts=newest, seen=seen, p95_s=round(p95, 4))


# ── G3: ledger completeness, per row type and per field ──────────────────────
#
# 2026-10-04, real ledger: the old definition (every row, every field non-null,
# organic-tagged rows only) read 0.4% (n=2,214) while the data was near-complete.
# Three defects, one each: (1) ``tier_retry`` is null on every call that was not
# retried -- null is its correct value, so requiring it non-null scored the ~97% of
# rows with no retry as incomplete; (2) ``tier_proposed`` is null on side calls and
# first-call floors, where no classifier runs and no proposal exists; (3) the row
# filter ``session_kind in organic`` removed exactly the rows lacking the tag, so
# that field was complete by construction. A row is complete when every field that
# CAN apply to its type is recorded.

def _usable_session(row: dict) -> bool:
    sid = row.get("session_id")
    return isinstance(sid, str) and sid not in ("", "unknown")


def g3_row_type(row: dict) -> str:
    """The row's type, which decides which G3 fields can apply to it:

    ``served``          answered by a non-Claude backend before any tier decision ran
    ``tiers_off``       the proxy ran with ``--tiers off``: no policy, no version
    ``side_call``       a call with no client tools (titles, summaries): never classified
    ``pinned``          model kept as requested (unknown/pinned model, ``opus:`` pin)
    ``first_call``      first-call floors: kept before the classifier runs
    ``decision_error``  the tier decision raised: a defect, every field is owed
    ``undecided``       tiers on, forwarded, and NO decision recorded: a defect
    ``classified``      the classifier ran and a proposal exists
    """
    from llm_router.proxy import tiers as pt

    if row.get("decision") == "served":
        return "served"
    if row.get("tier_mode") == "off":
        return "tiers_off"
    reason = row.get("tier_reason")
    if reason is None:
        return "undecided"
    if reason == pt.REASON_SIDE_CALL:
        return "side_call"
    if reason in (pt.REASON_UNKNOWN_MODEL, pt.REASON_CONFIG_PINNED, pt.REASON_USER_PINNED,
                  pt.REASON_EXPLICIT_OPUS_PIN):
        return "pinned"
    if reason == pt.REASON_DECISION_ERROR:
        return "decision_error"
    first_call_keeps = (pt.REASON_FIRST_CALL, pt.REASON_LONG_FIRST_PROMPT)
    if reason in first_call_keeps or (reason == pt.REASON_QUOTA_PRESSURE
                                      and row.get("tier_detail") in first_call_keeps):
        return "first_call"
    return "classified"


_G3_BASE = ("session_kind", "tier_policy_version", "tier_retry")
_G3_REQUIRED = {
    "classified": G3_FIELDS, "decision_error": G3_FIELDS, "undecided": G3_FIELDS,
    "side_call": _G3_BASE, "pinned": _G3_BASE, "first_call": _G3_BASE, "served": _G3_BASE,
    "tiers_off": ("session_kind", "tier_retry"),
}


def g3_required_fields(row: dict) -> tuple[str, ...]:
    """The G3 fields that can apply to this row. The session tag cannot apply to a
    request that names no session (nothing to look a tag up by)."""
    fields = _G3_REQUIRED[g3_row_type(row)]
    return fields if _usable_session(row) else tuple(f for f in fields if f != "session_kind")


def _g3_recorded(row: dict, field: str) -> bool:
    return field in row if field in _G3_PRESENCE_ONLY else row.get(field) is not None


def _g3_completeness(all_rows: list[dict], days: int, now: float,
                     override_since: float | None) -> dict:
    # Schema start: the first row that carries each field's key; a row counts once
    # EVERY field exists in the schema, so the start is the latest of those firsts.
    first: dict[str, float | None] = {f: None for f in G3_FIELDS}
    for r in all_rows:
        ts = _num_ts(r.get("ts"))
        if ts is None:
            continue
        for f in G3_FIELDS:
            if f in r and (first[f] is None or ts < first[f]):
                first[f] = ts
    if override_since is not None:
        since, source = override_since, "override"
    else:
        absent = [f for f, t in first.items() if t is None]
        if absent:
            return _not_measurable(f"no proxy row carries {', '.join(absent)} yet "
                                   "(the schema has not started)", seen=len(all_rows))
        since, source = max(t for t in first.values() if t is not None), "first row carrying every field"
    cutoff = now - days * 86400 if days else None
    window: list[tuple[float, dict]] = []
    undated = 0
    for r in all_rows:
        ts = _num_ts(r.get("ts"))
        if ts is None:
            undated += 1
        elif cutoff is None or ts >= cutoff:
            window.append((ts, r))
    counted = [(ts, r) for ts, r in window if ts >= since]
    before = len(window) - len(counted)
    stats = {f: {"applicable": 0, "recorded": 0} for f in G3_FIELDS}
    by_type: dict[str, dict[str, int]] = {}
    complete = no_field = 0
    newest: float | None = None
    for ts, r in counted:
        rtype = g3_row_type(r)
        required = g3_required_fields(r)
        t = by_type.setdefault(rtype, {"rows": 0, "complete": 0})
        if not required:
            no_field += 1
            continue
        t["rows"] += 1
        newest = _newer(newest, ts)
        ok = True
        for f in required:
            stats[f]["applicable"] += 1
            if _g3_recorded(r, f):
                stats[f]["recorded"] += 1
            else:
                ok = False
        if ok:
            complete += 1
            t["complete"] += 1
    n = len(counted) - no_field
    since_s = _iso(since)
    extras = {
        "schema_since": since_s, "schema_since_source": source,
        "field_first_seen": {f: _iso(t) for f, t in first.items()},
        "window_rows": len(window), "rows_before_schema": before, "undated_rows": undated,
        "rows_counted": n, "rows_no_applicable_field": no_field, "rows_by_type": by_type,
        "fields": {f: {"applicable": st["applicable"], "recorded": st["recorded"],
                       "not_applicable": len(counted) - st["applicable"],
                       "coverage": (round(st["recorded"] / st["applicable"], 4)
                                    if st["applicable"] else None)}
                   for f, st in stats.items()},
    }
    if not window:
        out = _not_measurable("no proxy row in window", seen=0)
    elif n == 0:
        why = (f"{len(window)} row(s) in window, all before the schema start ({since_s}); "
               "pass --schema-since to audit older rows" if not counted
               else "no row in window has an applicable G3 field")
        out = _not_measurable(why, seen=len(window))
    else:
        out = _rate_result(complete, n, label="proxy row", newest_ts=newest, seen=len(window))
        if out["measurable"]:
            per_field = ", ".join(
                f"{f} {_pct(extras['fields'][f]['coverage'])}"
                + (f" of {st['applicable']} applicable" if st["applicable"] < n else "")
                for f, st in stats.items() if st["applicable"])
            out["value"] = (f"{_pct(complete / n)} (n={n}; since {since_s[:10]}; {per_field}; "
                            f"{before} row(s) before schema excluded)")
    out.update(extras)
    return out


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
        from llm_router.dashboard_data import newest_timestamp, summary

        period = _period_for_days(days)
        s = summary(period)
        n = s.estimated_n
        newest = newest_timestamp()
    except Exception as exc:  # noqa: BLE001
        # This command is read-only: a failure here is reported, not recorded via
        # failopen (that would write to ~/.llm-router from a reporting command).
        return _not_measurable(f"dashboard_data.summary() raised {type(exc).__name__}; see llm-router doctor")
    if n == 0:
        return _not_measurable(f"no routed calls with a recorded saving in period={period}")
    display = s.display()
    note = (f"{display} [period={period}; NOT session-kind filtered -- usage.db predates "
            "session tagging]")
    if n < MIN_N:
        return _too_few(n, newest_ts=newest) | {"value": f"{TOO_FEW} ({display}, n={n})"}
    return _measured(note, n, newest_ts=newest, estimated_usd=s.estimated_usd)


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


def _bench_newest(bench: dict) -> float | None:
    return _parse_ts(bench.get("generated_at")) if isinstance(bench, dict) else None


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
        return _too_few(n, newest_ts=_bench_newest(bench))
    return _measured(f"{_pct(o2['acceptable_rate'])} acceptable vs Claude (n={n}, frozen set)", n,
                      newest_ts=_bench_newest(bench), generated_at=bench.get("generated_at"))


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
        return _too_few(n, newest_ts=_bench_newest(bench))
    under = d5.get("under_route_rate")
    under_s = f", under-route={_pct(under)}" if isinstance(under, (int, float)) else ""
    gate = "" if not isinstance(under, (int, float)) else (
        " (within <=10% gate)" if under <= 0.10 else " (OVER the <=10% gate)")
    return _measured(f"{_pct(d5['accuracy'])} exact-tier accuracy{under_s}{gate} (n={n})", n,
                      newest_ts=_bench_newest(bench), generated_at=bench.get("generated_at"))


# ── G2: failopen, all-time only ───────────────────────────────────────────────

def _g2_silent_failures() -> dict:
    try:
        from llm_router import failopen

        snap = failopen.snapshot()
        try:
            newest = failopen.store_path().stat().st_mtime
        except OSError:
            newest = None
    except Exception as exc:  # noqa: BLE001
        return _not_measurable(f"failopen.snapshot() raised: {type(exc).__name__}")
    total = snap.total
    if total is None:
        return _not_measurable("fail-open store is present but unreadable")
    if total == 0:
        return _not_measurable(
            "no fail-open events recorded (cannot tell 'none occurred' from 'not recording')")
    # Events carry no timestamp; the store file's last write is the newest one can say.
    return _measured(
        f"{total} fail-open event(s) recorded, ALL-TIME (no per-event timestamp -- "
        "cannot be windowed or turned into a per-100-calls rate)",
        total, newest_ts=newest, all_time_total=total,
        by_code=dict(sorted(snap.by_code.items(), key=lambda kv: -kv[1])[:8]),
    )


# ── G4: provider_reset, point-in-time ──────────────────────────────────────────

def _g4_wrongly_benched() -> dict:
    try:
        from llm_router import provider_reset

        resets = provider_reset.all_provider_resets()
    except Exception as exc:  # noqa: BLE001
        return _not_measurable(f"provider_reset read raised: {type(exc).__name__}")
    n = len(resets)
    now = time.time()  # a snapshot of this moment: as fresh as it can be
    if n == 0:
        return _measured("0 providers currently benched (point-in-time; cannot confirm "
                          "any PAST bench was wrong)", 0, newest_ts=now)
    names = ", ".join(sorted(resets))
    return _measured(f"{n} provider(s) currently benched: {names} (point-in-time snapshot -- "
                      "whether a bench is WRONG needs a human to say so)", n, newest_ts=now,
                      benched=sorted(resets))


# ── assembly ───────────────────────────────────────────────────────────────

def compute_scorecard(days: int = 7, *, include_research: bool = False,
                      schema_since: float | None = None, now: float | None = None) -> dict:
    from llm_router import session_kind as sk
    from llm_router.proxy import ledger as pl

    now_ts = time.time() if now is None else now
    allowed = _allowed_kinds(include_research)
    all_rows = pl.read_rows()
    index = sk.KindIndex(all_rows)
    ns_r, d1_r, d2_r, joins = _ns_d1_d2(days, allowed, index)
    d3_r = _d3_redo_rate(days, allowed, index)
    pop = _proxy_population(all_rows, days, allowed, now_ts)
    d4_r = _d4_tier_mix(pop)
    g1_hook_r, g1_proxy_r = _g1_latency(pop)
    g3_r = _g3_completeness(all_rows, days, now_ts, schema_since)
    o1_r = _o1_quota_avoided(days)
    bench = _load_benchmark()
    o2_r = _o2_quality_held(bench)
    d5_r = _d5_classifier_accuracy(bench)
    g2_r = _g2_silent_failures()
    g4_r = _g4_wrongly_benched()
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "generated_ts": now_ts,
        "window_days": days,
        "include_research": include_research,
        "joins": joins,
        "kpis": {
            "NS": ns_r, "O1": o1_r, "O2": o2_r,
            "D1": d1_r, "D2": d2_r, "D3": d3_r, "D4": d4_r, "D5": d5_r,
            "G1_hook": g1_hook_r, "G1_proxy": g1_proxy_r, "G2": g2_r, "G3": g3_r, "G4": g4_r,
        },
    }


# ── --health: measured / blind / stale, per KPI ──────────────────────────────

STATE_MEASURED = "measured"
STATE_BLIND = "blind"
STATE_STALE = "stale"

#: KPIs read from the frozen benchmark: judged against STALE_BENCHMARK_DAYS.
_BENCHMARK_KPIS = frozenset({"O2", "D5"})


def _age(hours: float) -> str:
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


def compute_health(data: dict, *, stale_hours: float = STALE_LIVE_HOURS,
                   now: float | None = None) -> dict:
    """Per KPI: ``measured`` (a number, with a newest data point inside the stale
    threshold), ``blind`` (no number: nothing to count, too few, or not instrumented,
    or a number nobody can date) or ``stale`` (a number whose newest data point is
    older than the threshold). One reason each, and the n. Reads the scorecard dict, so
    it cannot disagree with it."""
    now_ts = now if now is not None else data["generated_ts"]
    out: dict[str, dict] = {}
    for key in _ORDER:
        r = data["kpis"][key]
        limit_h = STALE_BENCHMARK_DAYS * 24 if key in _BENCHMARK_KPIS else stale_hours
        newest = r.get("newest_ts")
        age_h = (now_ts - newest) / 3600 if newest is not None else None
        if not r["measurable"]:
            state, reason, n = STATE_BLIND, r["reason"], r["seen"]
        elif age_h is None:
            state, n = STATE_BLIND, r["n"]
            reason = "measured, but its data carries no timestamp: cannot tell how old it is"
        elif age_h > limit_h:
            state, n = STATE_STALE, r["n"]
            reason = f"newest data point is {_age(age_h)} old, over the {_age(limit_h)} limit"
        else:
            state, n = STATE_MEASURED, r["n"]
            reason = f"newest data point {_age(max(age_h, 0.0))} old"
        out[key] = {"state": state, "reason": reason, "n": n, "newest_ts": newest,
                    "newest": _iso(newest), "age_hours": round(age_h, 2) if age_h is not None else None,
                    "stale_after_hours": limit_h}
    counts = {s: sum(1 for v in out.values() if v["state"] == s)
              for s in (STATE_MEASURED, STATE_BLIND, STATE_STALE)}
    return {"generated_at": data["generated_at"], "window_days": data["window_days"],
            "stale_after_hours": stale_hours, "benchmark_stale_after_days": STALE_BENCHMARK_DAYS,
            "counts": counts, "kpis": out}


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


def _join_line(joins: dict) -> str:
    w, j, u = joins["window_units"], joins["joined"], joins["untagged"]
    src = ", ".join(f"{k} {v}" for k, v in sorted(joins["joined_by_source"].items(), key=lambda kv: -kv[1]))
    extra = []
    if joins["untagged_conflicting"]:
        extra.append(f"{joins['untagged_conflicting']} of them from sessions whose proxy rows disagree")
    if joins["sessions_where_tag_and_proxy_rows_disagree"]:
        extra.append(f"{joins['sessions_where_tag_and_proxy_rows_disagree']} session(s) whose tag file "
                     "disagrees with their proxy rows (the tag file wins)")
    pop = ""
    if joins["counted"]:
        pop = (f" The counted population is {joins['counted']:,} unit(s) from "
               f"{joins['counted_sessions']} session(s); the largest is "
               f"{_pct(joins['largest_session_share'], 0)} of it.")
    return (f"NS/D1/D2 units: {w:,} in window; {j:,} joined to a session-kind tag"
            + (f" ({src})" if src else "") + f"; {u:,} untagged (never counted as organic)"
            + (f"; {'; '.join(extra)}" if extra else "") + "." + pop)


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
    lines.append(_join_line(data["joins"]))
    lines.append("O1 is never session-kind filtered (usage.db predates tagging); G3 is not "
                  "session-kind filtered either (see KPIS.md). "
                  "O2/D5 need LLM_ROUTER_KPI_BENCHMARK_PATH. G2/G4 are not windowed "
                  "the same way as the rest -- see each line and the module docstring.")
    return "\n".join(lines)


def render_health(health: dict) -> str:
    lines = [
        f"llm-router kpi --health -- window={health['window_days']}d generated={health['generated_at']}",
        f"  stale = newest data point older than {_age(health['stale_after_hours'])} "
        f"(live ledgers) or {health['benchmark_stale_after_days']:.0f}d (frozen benchmark, O2/D5)",
        "",
    ]
    for key in _ORDER:
        h = health["kpis"][key]
        lines.append(f"  {_LABELS[key]:<42s} {h['state']:<9s} n={str(h['n']):<7} {h['reason']}")
    c = health["counts"]
    lines += ["", f"{c[STATE_MEASURED]} measured, {c[STATE_BLIND]} blind, {c[STATE_STALE]} stale "
                  "(exit 0; --strict exits 1 if any KPI is blind)"]
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
    ap.add_argument("--schema-since", metavar="WHEN", default=None,
                     help="G3: count proxy rows from WHEN (YYYY-MM-DD, an ISO time, or epoch "
                          "seconds) instead of from the first row that carries the fields")
    ap.add_argument("--health", action="store_true",
                     help="per KPI: measured, blind or stale, with the one reason and the n")
    ap.add_argument("--strict", action="store_true",
                     help="with --health: exit 1 if any KPI is blind (default exit 0)")
    ap.add_argument("--stale-hours", type=float, default=STALE_LIVE_HOURS,
                     help=f"with --health: a live KPI is stale past this age (default {STALE_LIVE_HOURS:.0f})")
    parsed = ap.parse_args(args)
    if parsed.strict and not parsed.health:
        ap.error("--strict needs --health")
    since = None
    if parsed.schema_since is not None:
        since = _parse_ts(parsed.schema_since)
        if since is None:
            try:
                since = _parse_ts(float(parsed.schema_since))
            except ValueError:
                ap.error(f"--schema-since: cannot read {parsed.schema_since!r} as a date or epoch")

    data = compute_scorecard(days=parsed.days, include_research=(parsed.include == "research"),
                             schema_since=since)

    exit_code = 0
    if parsed.health:
        health = compute_health(data, stale_hours=parsed.stale_hours)
        print(json.dumps(health, indent=2, default=str) if parsed.json else render_health(health))
        if parsed.strict and health["counts"][STATE_BLIND]:
            exit_code = 1
    elif parsed.json:
        print(json.dumps(data, indent=2, sort_keys=False, default=str))
    else:
        print(render_scorecard(data))

    if parsed.write_weekly:
        path = write_weekly(data, Path(parsed.write_weekly))
        print(f"\nwrote {path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(cmd_kpi(sys.argv[1:]))
