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
  stamped on that session's proxy rows, then (last resort) the backfill sidecar --
  see ``--backfill-tags`` below. NS/D1/D2 units and D3 events go through it; the
  scorecard prints how many units joined a tag and how many stayed untagged
  (``joins``), and states how much of each affected KPI's n was backfilled, e.g.
  "n=3,674 (1,210 backfilled)". D4/G1 read the kind each proxy row was written
  with: rows from before tagging existed stay untagged, so D4's population is the
  tagged rows only, as before -- the sidecar is never consulted for D4/G1.
* **``--backfill-tags [--dry-run]``** derives a ``session_kind`` for every session
  (in a ledger, or with a north-star unit) that has no live evidence, from its
  Claude Code transcript's ``cwd``/``entrypoint`` (``session_kind.classify_with_basis``
  -- the SAME rules the live tagger uses), and appends one row per session to the sidecar
  ``session_kind_backfill.jsonl`` (``session_kind_backfill.py``). Insufficient
  evidence -> ``"unknown"``, never ``"organic"``. Write-once per session; a live
  tag always wins (the sidecar is consulted only as ``KindIndex.resolve``'s last
  resort); deleting the sidecar restores the pre-backfill behaviour exactly. The
  sidecar is read ONLY by ``kpi`` itself (NS/D1/D2 through ``northstar.units(backfill=True)``,
  D3 through its own ``KindIndex``) and by ``--backfill-tags`` / ``--validate-backfill``:
  never by the proxy, any hook, the Stop line or the quality breaker (``backfill`` is
  off by default in ``northstar``; see ``session_kind``'s docstring). The
  original ledgers are read, never written. ``--dry-run`` reports what would be
  written and writes nothing. ``--validate-backfill`` (read-only, counts only) runs
  the same rules on sessions that DO have a live kind and prints agreement and a
  confusion table.
* **G3 is NOT session-kind filtered.** Completeness is a property of the writer,
  and the session tag is one of the fields under test: filtering rows by
  ``session_kind`` first drops exactly the rows that lack it, so the field read
  100% by construction. G3 counts every row in the window, per row type and per
  field, from the schema's start date (``g3_*`` below, KPIS.md).
* **O1 is NOT session-kind filtered.** It reads ``usage.db`` via
  ``dashboard_data.summary()``, the same canonical accessor every other
  savings surface uses (see ``commands/savings_report.py``) -- that table
  predates session tagging and carries no ``session_kind`` column. Shown as
  "est." per the 2026-09-27 display rule, with its own note. Where the proxy
  ledger has >= ``MIN_N`` calls whose real Anthropic usage is recorded and
  consistent (``proxy.cost_accounting``), O1 prints those RECONCILED figures
  instead (real spend + counterfactual avoided); Claude Code's own
  ``total_cost_usd`` is phantom on a proxied session and is never read.
* **O2 and D5 need a frozen benchmark.** They do not come from live traffic.
  ``scripts/build_kpi_benchmark.py`` builds the file from the blind A/B truth
  (committed copy: ``docs/repo_goals/kpi_benchmark.json``, ids and labels only).
  A configured path that does not exist reports ``CHZ-KPI-BENCH-MISSING``, one that
  does not parse ``CHZ-KPI-BENCH-MALFORMED``: both "not measurable", never 0%.
  Configure ``LLM_ROUTER_KPI_BENCHMARK_PATH`` to a JSON file shaped
  ``{"generated_at": "...", "o2": {"acceptable_rate": 0.0-1.0, "n": int},
  "d5": {"accuracy": 0.0-1.0, "under_route_rate": 0.0-1.0, "n": int}}``
  (``scripts/kpi_benchmark_template.json`` is not shipped; the shape is the
  contract). No path configured, or the file does not parse -> "not measured",
  the label the KPI spec itself uses for this gap.
* **G1 hook latency** comes from ``hook_latency.jsonl`` (``llm_router.hook_latency``):
  one row per hook invocation, p50 / p95 per hook against that hook's budget (the
  one table ``hook_latency.HOOK_BUDGETS_MS``). NOT session-kind filtered -- a row
  carries no session id. A hook the host KILLS at its timeout writes no row; kills
  are shown from the fail-open ledger (``CHZ-HOOK-KILLED``). The proxy-side half is
  G1_proxy: p50 / p95 of ``tier_decision_s`` in proxy_calls.jsonl, turn-first and
  continuation calls apart.
* **classifier shadow** (informational, outside ``kpis``) is the local LLM classifier's
  shadow log (``classifier_shadow.jsonl``, written by ``proxy/llm_shadow``): calls, sessions,
  agreement with the rules' tier, tier distributions, cheap share, fallback rate, p50 / p95 ms,
  drops and calls per turn. Organic sessions only unless research is included; hashes and
  tiers only. See ``_classifier_shadow_summary`` for each definition.
* **G2 silent failures** is fail-open events per 100 calls over the window, from
  the ``ts`` every ``failopen.record`` row now carries. "Calls" are the hook
  invocations plus the proxy calls recorded in the window, all session kinds,
  because a fail-open row names no session and the numerator cannot be filtered
  to organic. Rows written before the timestamp existed cannot be placed in a
  window; they stay in a labelled ALL-TIME line and are never guessed into one.
  The window starts no earlier than the first timestamped evidence (a timestamped
  fail-open row or a recorded hook call), so the denominator never counts calls
  from a period the numerator could not see. Broader silent-failure classes named
  in the spec (truncation/overflow, Ollama hung) are not wired into this counter
  and are not claimed here.
* **G4 wrongly benched providers** is wrong benches per 100 benches, from
  ``provider_bench.jsonl`` (``llm_router.provider_bench_log``). A bench is wrong
  when the owner cleared it with ``llm-router provider unban`` before it lapsed,
  or a call to that provider succeeded before its reset time. Zero benches in the
  window is "not measurable", never 0%; a bench still in force can still turn out
  wrong and is reported separately. The providers benched right now (the old
  point-in-time reading) is kept as a detail line.

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
import math
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

#: Fields added to the proxy ledger after G3 started (M0.5). Each is measured only on
#: rows written at or after ITS OWN first appearance in the ledger, so rows from
#: before the field existed never lower G3, and a field no row carries yet is simply
#: not applicable (it does not make G3 "not measurable"). ``cls_ms`` and ``cls_arm``
#: are null by design until the classifier runs (M1, M2), so only their presence is
#: required. ``tier_haiku_block`` exists only when the policy proposed Haiku.
G3_LATE_FIELDS = ("text_sha", "has_mid_system", "req_bytes", "cls_source", "cls_ms", "cls_arm",
                  "cls_applied", "tier_haiku_block")
_G3_LATE_PRESENCE_ONLY = frozenset({"cls_ms", "cls_arm"})


def _not_measurable(reason: str, *, seen: int = 0, newest_ts: float | None = None) -> dict[str, Any]:
    return {"value": f"not measurable: {reason}", "n": None, "measurable": False,
            "reason": reason, "seen": seen, "newest_ts": newest_ts}


def _too_few(n: int, *, newest_ts: float | None = None, backfilled: int = 0) -> dict[str, Any]:
    note = f", {backfilled:,} backfilled" if backfilled else ""
    return {"value": f"{TOO_FEW} (n={n}{note})", "n": n, "measurable": False,
            "reason": f"{TOO_FEW}: n={n}, need {MIN_N}", "seen": n, "newest_ts": newest_ts,
            "backfilled": backfilled}


def _measured(value: str, n: int, *, newest_ts: float | None = None, **extra: Any) -> dict[str, Any]:
    out = {"value": value, "n": n, "measurable": True, "seen": n, "newest_ts": newest_ts}
    out.update(extra)
    return out


def _pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def _rate_result(numerator: int, denominator: int, *, label: str = "n",
                 newest_ts: float | None = None, seen: int | None = None,
                 backfilled: int = 0) -> dict[str, Any]:
    """``backfilled`` is how many of ``denominator`` resolved their session_kind from
    the backfill sidecar (``session_kind.SOURCE_BACKFILL``) rather than a live tag,
    stamp or ledger agreement -- stated on the line, never folded in silently."""
    seen_n = seen if seen is not None else denominator
    if denominator <= 0:
        return _not_measurable(f"no {label} in window", seen=seen_n)
    if denominator < MIN_N:
        out = _too_few(denominator, newest_ts=newest_ts, backfilled=backfilled)
        out["seen"] = seen_n
        return out
    note = f", {backfilled:,} backfilled" if backfilled else ""
    return _measured(f"{_pct(numerator / denominator)} (n={denominator}{note})", denominator,
                      newest_ts=newest_ts, seen=seen_n, backfilled=backfilled,
                      numerator=numerator, denominator=denominator)


def _num_ts(raw: Any) -> float | None:
    """``raw`` as epoch seconds when it is a real, plausible number, else None (a bool,
    NaN, infinity or a value past year 5000 is not a time a ledger row can carry)."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and abs(raw) < 1e11:
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
        try:
            return _num_ts((when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp())
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    except (OverflowError, OSError, ValueError):
        return None


class _Window:
    """An absolute window ``[since, until]`` (``--since`` / ``--until``).

    ``days`` stays the relative form every reader already takes, so under a window the
    scorecard hands them ``now=until`` and ``days=(until-since)/86400``. A reader that
    anchors its cutoff to the wall clock instead (transcript mtimes, ``usage.db``,
    ``read_rows(days=)``) is called with :attr:`wall_days`, which reaches back to
    ``since``, and its rows are then cut to the window by :meth:`covers`."""

    def __init__(self, since: float, until: float) -> None:
        self.since, self.until = since, until

    @property
    def days(self) -> float:
        return (self.until - self.since) / 86400.0

    @property
    def wall_days(self) -> int:
        return max(1, math.ceil((time.time() - self.since) / 86400.0))

    def covers(self, raw: Any) -> bool:
        """A row with no usable timestamp cannot be placed in a window: it is out."""
        ts = _parse_ts(raw)
        return ts is not None and self.since <= ts <= self.until


def _wall_days(days: float, window: "_Window | None") -> float:
    return days if window is None else window.wall_days


def _window_label(data: dict) -> str:
    w = data.get("window")
    return f"{w['since']}..{w['until']}" if w else f"{data['window_days']}d"


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

def _ns_d1_d2(days: int, allowed: frozenset[str], index,
              win: "_Window | None" = None) -> tuple[dict, dict, dict, dict]:
    """Pooled (not per-session-median) totals over units whose session resolves to a
    kind in ``allowed``. Mirrors the attempted/used accounting
    ``northstar.report()`` already uses, so NS/D1/D2 cannot disagree with
    what ``llm-router northstar`` shows for the same sessions.

    ``northstar.units()`` stamps each unit with its session's kind (tag file, then
    the unit's own ledger stamp, then the session's proxy rows); a stream without the
    stamp is resolved here against ``index``. Returns the three results and the join
    counts: how many units found a tag and how many stayed untagged."""
    from llm_router import northstar as ns

    window = joined = untagged = conflicting = total = attempted = used = used_heuristic = 0
    backfilled_total = backfilled_attempted = 0
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_session: dict[str, int] = {}  # counted units per session: how concentrated the population is
    disagree: set[str] = set()
    checked: set[str] = set()
    newest: float | None = None
    # backfill=True: this KPI is the one reader that resolves a unit's kind from the
    # backfill sidecar. northstar.units() leaves it off for every hot-path caller.
    for u in ns.units(days=_wall_days(days, win), backfill=True):
        if win is not None and not win.covers(u.get("ts")):
            continue
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
        is_backfilled = source == session_kind.SOURCE_BACKFILL
        if is_backfilled:
            backfilled_total += 1
        by_session[str(sid)] = by_session.get(str(sid), 0) + 1
        newest = _newer(newest, _parse_ts(u.get("ts")))
        is_attempted = u["kind"] in ns.ATTEMPTED_KINDS or u.get("lever") == "proxy"
        if is_attempted:
            attempted += 1
            if is_backfilled:
                backfilled_attempted += 1
            # NS and D2 count STRICT-used only (PLAN M0.2). The old heuristic numerator is
            # kept as a diagnostic, outside KPI_CODES.
            if ns.is_strict_used(u):
                used += 1
            if u["outcome"] == ns.OUTCOME_USED:
                used_heuristic += 1
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
        joins["diag"] = {"NS_heuristic": _heuristic_diag(dict(out)), "D2_heuristic": _heuristic_diag(dict(out))}
        return _strict_note(out), dict(out), _strict_note(dict(out)), joins
    joins["diag"] = {
        "NS_heuristic": _heuristic_diag(_rate_result(
            used_heuristic, total, label="unit", newest_ts=newest, seen=window, backfilled=backfilled_total)),
        "D2_heuristic": _heuristic_diag(_rate_result(
            used_heuristic, attempted, label="attempt", newest_ts=newest, seen=window,
            backfilled=backfilled_attempted)),
    }
    return (_strict_note(_rate_result(used, total, label="unit", newest_ts=newest, seen=window,
                                      backfilled=backfilled_total)),
            _rate_result(attempted, total, label="unit", newest_ts=newest, seen=window,
                         backfilled=backfilled_total),
            _strict_note(_rate_result(used, attempted, label="attempt", newest_ts=newest, seen=window,
                                      backfilled=backfilled_attempted)),
            joins)


def _verify_shadow(days: int) -> dict | None:
    """Informational counts of units in the window that carry a verify record. Counts only:
    never read by NS, D1 or D2 (verifier PR B, shadow). None when no unit carries one."""
    from llm_router import northstar as ns

    records = ns.load_verify_records()  # joined here, not via units(): NS/D2 must not see them
    if not records:
        return None
    c = {"verified": 0, "weak": 0, "failed": 0, "unavailable": 0}
    for u in ns.units(days=days, backfill=True):
        s = (records.get(u.get("unit_id")) or {}).get("verify_status")
        if s in ("pass_f2p", "pass_f2p_model"):
            c["verified"] += 1
        elif s == "pass_p2p":
            c["weak"] += 1
        elif s == "fail":
            c["failed"] += 1
        elif s in ("unavailable", "not_applicable"):
            c["unavailable"] += 1
    return c if any(c.values()) else None


def _strict_note(result: dict) -> dict:
    """Name the strict rule on a measured NS or D2 result: a ``reason`` and a printed line."""
    from llm_router import northstar as ns

    if not result.get("measurable"):
        return result  # an unmeasurable result already carries its own reason; leave it as it was
    result.setdefault("reason", ns.STRICT_RULE_TEXT)
    result["lines"] = list(result.get("lines", ())) + [ns.STRICT_RULE_TEXT]
    return result


def _heuristic_diag(result: dict) -> dict:
    """The pre-M0.2 numerator (heuristic outcome == used), shown beside the strict one."""
    result = dict(result)
    note = "heuristic outcome=used; not a target (strict NS and D2 are the targets, PLAN M0.2)"
    if result.get("measurable"):
        result["reason"] = note
    else:
        result["reason"] = f"{result.get('reason')} ({note})"
    result["lines"] = [note]
    return result


# ── D3: redo rate, from usage_outcome ────────────────────────────────────────

def _d3_redo_rate(days: int, allowed: frozenset[str], index, win: "_Window | None" = None) -> dict:
    from llm_router import usage_outcome as uo

    rows = uo.judge_recent(days=_wall_days(days, win))
    if win is not None:
        rows = [r for r in rows if win.covers(r.get("ts"))]
    used = redone = unknown = backfilled = 0
    newest: float | None = None
    for r in rows:
        res = index.resolve(r.get("session_id"), stamp=r.get("session_kind"))
        if res.kind not in allowed:
            continue
        if r["outcome"] == uo.OUTCOME_USED:
            used += 1
        elif r["outcome"] == uo.OUTCOME_REDONE:
            redone += 1
        else:
            unknown += 1
            continue
        if res.source == session_kind.SOURCE_BACKFILL:
            backfilled += 1
        newest = _newer(newest, _num_ts(r.get("ts")))
    decided = used + redone
    result = _rate_result(redone, decided, label="decided event", newest_ts=newest, seen=len(rows),
                          backfilled=backfilled)
    result["unknown_window_open"] = unknown
    result["used"], result["redone"] = used, redone
    return result


def _fold_user_signals(d3: dict, days: int, now: float) -> dict:
    """D3 with the receipt band's presses (``user_signal``) folded in.

    OWNER RULE (2026-10-05): "used" needs a passing test, and a keep press is not one.
    ``user_redone`` joins D3 as decided redo events (numerator and denominator);
    ``user_kept`` is reported on its own line and is never added to anything: not to
    D3's denominator, and never to NS, D1 or D2, which this function does not touch.
    ``tests/test_user_signal_kpi.py`` fails if a keep ever moves NS, D1 or D2."""
    from llm_router import user_signal

    sig = user_signal.summarize(days, now=now)
    kept, user_redone = sig["kept"], sig["redone"]
    out = dict(d3)
    if user_redone:
        redone = d3.get("redone", 0) + user_redone
        decided = d3.get("used", 0) + redone
        out = _rate_result(redone, decided, label="decided event",
                           newest_ts=_newer(d3.get("newest_ts"), sig["newest_ts"]),
                           seen=(d3.get("seen") or 0) + user_redone, backfilled=d3.get("backfilled", 0))
        out.update(unknown_window_open=d3.get("unknown_window_open"),
                   used=d3.get("used", 0), redone=redone)
    out["user_kept"], out["user_redone"] = kept, user_redone
    out["lines"] = list(d3.get("lines", ())) + [
        f"user_redone n={user_redone} (redo on Claude pressed on the receipt band; counted in D3 as "
        "decided redo events; the row has no session id, so it is not session-kind filtered)",
        f"user_kept n={kept} (keep pressed; shown only: never counted as used, so never in "
        "NS, D1, D2 or D3's denominator)",
    ]
    return out


# ── proxy ledger: the rows behind D4 and G1 ──────────────────────────────────

def _proxy_population(all_rows: list[dict], days: int, allowed: frozenset[str], now: float,
                      until: float | None = None) -> dict:
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
        elif (cutoff is None or ts >= cutoff) and (until is None or ts <= until):
            window.append(r)
    kept: list[dict] = []
    untagged = other = 0
    for r in window:
        # The owner's override beats the kind the row was written with; with none, the
        # stamp is all there is (a tag file is never joined on: see the docstring).
        kind = session_kind.override_of(r.get("session_id")) or r.get("session_kind")
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
    tiered = [r for r in pop["allowed"] if isinstance(r.get("tier"), str) and r["tier"]]
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


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest rank on the n-1 scale (the rule the proxy p95 always used)."""
    k = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[k]


def _g1_segment(values: list[float]) -> dict[str, Any]:
    """n, p50 and p95 (seconds) of one latency segment; percentiles are None below MIN_N."""
    n = len(values)
    if n < MIN_N:
        return {"n": n, "p50_s": None, "p95_s": None}
    ordered = sorted(values)
    return {"n": n, "p50_s": round(_percentile(ordered, 0.50), 4),
            "p95_s": round(_percentile(ordered, 0.95), 4)}


def _g1_proxy(pop: dict) -> dict:
    """Proxy tier-decision latency: p50 / p95 of ``tier_decision_s`` with n, split into
    turn-first and continuation calls. Side calls never run a classifier and are left
    out (counted in ``side_call_excluded``). ``added_latency_s`` is not used: it is 0.0
    on every forwarded row, which is why this KPI used to print 0 ms."""
    seen = len(pop["window"])
    first: list[float] = []
    cont: list[float] = []
    side = 0
    newest: float | None = None
    for r in pop["allowed"]:
        v = r.get("tier_decision_s")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if r.get("tier_reason") == "side_call":
            side += 1
            continue
        (cont if r.get("step_class") == "continuation" else first).append(float(v))
        newest = _newer(newest, _num_ts(r.get("ts")))
    n = len(first) + len(cont)
    if not n:
        return _not_measurable("no proxy decisions with tier_decision_s in window", seen=seen)
    if n < MIN_N:
        out = _too_few(n, newest_ts=newest)
        out["seen"] = seen
        return out
    seg_first, seg_cont = _g1_segment(first), _g1_segment(cont)

    def _fmt(name: str, seg: dict[str, Any]) -> str:
        if seg["p50_s"] is None:
            return f"{name} {TOO_FEW} (n={seg['n']})"
        return (f"{name} p50={seg['p50_s'] * 1000:.0f}ms p95={seg['p95_s'] * 1000:.0f}ms "
                f"(n={seg['n']})")

    return _measured(f"{_fmt('turn-first', seg_first)} | {_fmt('continuation', seg_cont)}", n,
                      newest_ts=newest, seen=seen, turn_first=seg_first, continuation=seg_cont,
                      side_call_excluded=side)


def _in_window(ts: Any, since: float, until: float) -> bool:
    """A row without a usable timestamp cannot be placed in a window: it is out,
    never read as time zero."""
    t = _num_ts(ts)
    return t is not None and since <= t <= until


def _g1_hook(days: int, now: float, killed: int | None) -> dict:
    """p50 / p95 wall time per hook against that hook's budget.

    ``killed`` is the number of ``CHZ-HOOK-KILLED`` events in the window, or
    ``None`` when the fail-open ledger cannot say (no timestamped event exists
    yet): a host kill leaves no row here, so it is reported beside the log.
    """
    from llm_router import hook_latency as hl

    rows = hl.read_rows(since=now - days * 86400.0, until=now)
    if not rows:
        return _not_measurable(
            "no hook invocation recorded in window (a hook records itself only after its "
            "installed copy is updated to a version that carries the recorder)")
    by_hook: dict[str, list[dict]] = {}
    for r in rows:
        by_hook.setdefault(str(r.get("hook")), []).append(r)

    hooks: dict[str, dict] = {}
    lines: list[str] = []
    over: list[str] = []
    worst: tuple[float, str, int] | None = None
    thin = 0
    for name in sorted(by_hook):
        rs = by_hook[name]
        values = sorted(float(r["elapsed_ms"]) for r in rs)
        n = len(values)
        budget = hl.budget_ms(name)
        timed_out = sum(1 for r in rs if r.get("timed_out") is True)
        entry: dict[str, Any] = {"n": n, "budget_ms": budget, "timed_out": timed_out}
        if n < MIN_N:
            thin += 1
            lines.append(f"{name}: {TOO_FEW} (n={n}); budget {budget}ms; {timed_out} hit it")
        else:
            p50, p95 = _percentile(values, 0.50), _percentile(values, 0.95)
            entry.update(p50_ms=round(p50, 1), p95_ms=round(p95, 1))
            verdict = "within budget" if p95 <= budget else "OVER budget"
            lines.append(f"{name}: p50={p50:.0f}ms p95={p95:.0f}ms vs {budget}ms budget "
                         f"({verdict}); {timed_out} of {n} hit the budget")
            if p95 > budget:
                over.append(f"{name} p95={p95:.0f}ms>{budget}ms")
            if worst is None or p95 / budget > worst[0]:
                worst = (p95 / budget, name, round(p95))
        hooks[name] = entry
    if killed is None:
        lines.append("killed by the host (leaves no row) (auto-route only): not countable yet -- no "
                     "timestamped fail-open event exists to count CHZ-HOOK-KILLED from")
    else:
        lines.append(f"killed by the host (leaves no row; CHZ-HOOK-KILLED in the fail-open "
                     f"ledger) (auto-route only): {killed} in window")

    n_rows = len(rows)
    newest = rows[-1]["ts"]                      # read_rows is oldest first
    extra = {"hooks": hooks, "lines": lines, "killed": killed}
    if worst is None:
        return _too_few(n_rows, newest_ts=newest) | {
            "value": f"{TOO_FEW} (n={n_rows} rows over {len(by_hook)} hook(s); need >={MIN_N} per hook)",
            **extra}
    tail = f"; {thin} hook(s) {TOO_FEW}" if thin else ""
    if over:
        head = f"OVER budget: {', '.join(over)}"
    else:
        head = f"all {len(by_hook) - thin} measurable hook(s) within budget (worst p95 {worst[1]} {worst[2]}ms)"
    return _measured(f"{head}{tail} (n={n_rows} invocations)", n_rows, newest_ts=newest, **extra)


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

    ``tiers_off``       the proxy ran with ``--tiers off``: no policy, no version (even on a
                        row it served itself: the version is null whenever tiers are off)
    ``served``          answered by a non-Claude backend before any tier decision ran
    ``side_call``       a call with no client tools (titles, summaries): never classified
    ``pinned``          model kept as requested (unknown/pinned model, ``opus:`` pin)
    ``first_call``      first-call floors: kept before the classifier runs
    ``decision_error``  the tier decision raised: a defect, every field is owed
    ``undecided``       tiers on, forwarded, and NO decision recorded: a defect
    ``classified``      the classifier ran and a proposal exists
    """
    from llm_router.proxy import tiers as pt

    if row.get("tier_mode") == "off":
        return "tiers_off"
    if row.get("decision") == "served":
        return "served"
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


def _g3_recorded(row: dict, field: str, presence_only: frozenset[str] = _G3_PRESENCE_ONLY) -> bool:
    return field in row if field in presence_only else row.get(field) is not None


def _g3_late_first_seen(all_rows: list[dict]) -> dict[str, float | None]:
    """The timestamp of the first ledger row carrying each late field's key."""
    first: dict[str, float | None] = {f: None for f in G3_LATE_FIELDS}
    for r in all_rows:
        ts = _num_ts(r.get("ts"))
        if ts is None:
            continue
        for f in G3_LATE_FIELDS:
            if f in r and (first[f] is None or ts < first[f]):
                first[f] = ts
    return first


def _g3_late_required(row: dict, ts: float, late_first: dict[str, float | None]) -> tuple[str, ...]:
    """The late fields owed by this row: those whose key already existed in the
    ledger when the row was written. ``tier_haiku_block`` is owed only on a row the
    policy proposed Haiku for."""
    out = []
    for f in G3_LATE_FIELDS:
        t0 = late_first[f]
        if t0 is None or ts < t0:
            continue
        if f == "tier_haiku_block" and row.get("tier_proposed") != "haiku":
            continue
        out.append(f)
    return tuple(out)


def _g3_completeness(all_rows: list[dict], days: int, now: float,
                     override_since: float | None, until: float | None = None) -> dict:
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
    late_first = _g3_late_first_seen(all_rows)
    cutoff = now - days * 86400 if days else None
    window: list[tuple[float, dict]] = []
    undated = 0
    for r in all_rows:
        ts = _num_ts(r.get("ts"))
        if ts is None:
            undated += 1
        elif (cutoff is None or ts >= cutoff) and (until is None or ts <= until):
            window.append((ts, r))
    counted = [(ts, r) for ts, r in window if ts >= since]
    before = len(window) - len(counted)
    stats = {f: {"applicable": 0, "recorded": 0} for f in G3_FIELDS + G3_LATE_FIELDS}
    by_type: dict[str, dict[str, int]] = {}
    by_session: dict[str, int] = {}
    complete = no_field = 0
    newest: float | None = None
    for ts, r in counted:
        rtype = g3_row_type(r)
        required = g3_required_fields(r) + _g3_late_required(r, ts, late_first)
        t = by_type.setdefault(rtype, {"rows": 0, "complete": 0})
        if not required:
            no_field += 1
            continue
        t["rows"] += 1
        if _usable_session(r):
            by_session[r["session_id"]] = by_session.get(r["session_id"], 0) + 1
        newest = _newer(newest, ts)
        ok = True
        for f in required:
            stats[f]["applicable"] += 1
            if _g3_recorded(r, f, presence_only=_G3_PRESENCE_ONLY | _G3_LATE_PRESENCE_ONLY):
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
        "field_first_seen": {**{f: _iso(t) for f, t in first.items()},
                             **{f: _iso(t) for f, t in late_first.items() if t is not None}},
        "window_rows": len(window), "rows_before_schema": before, "undated_rows": undated,
        "rows_counted": n, "rows_no_applicable_field": no_field, "rows_by_type": by_type,
        "counted_sessions": len(by_session),
        "largest_session_share": round(max(by_session.values()) / n, 4) if by_session and n else None,
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
            out["value"] = (f"{_pct(complete / n)} (n={n}; since {since_s[:10]}; "
                            f"{len(by_session)} session(s); {per_field}; "
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


def _o1_reconciled(days: int, win: "_Window | None" = None) -> dict | None:
    """O1 from the proxy ledger's REAL per-call usage (``proxy.cost_accounting``),
    over sessions whose every row carries cost fields and passes the ledger's
    own consistency check. ``None`` when fewer than ``MIN_N`` calls are
    reconciled: the caller then keeps the "est." figure. Claude Code's own
    ``total_cost_usd`` is never read here (phantom on a proxied session)."""
    from llm_router.proxy import cost_accounting as ca
    from llm_router.proxy import ledger as pl

    rows = pl.read_rows(days=_wall_days(days, win))
    if win is not None:
        rows = [r for r in rows if win.covers(r.get("ts"))]
    by_session: dict[str, list[dict]] = {}
    for r in rows:
        # Rows without a session_id share one "" bucket: a single bad row there
        # vetoes the others, which fails safe (they fall back to the est. figure).
        by_session.setdefault(r.get("session_id") or "", []).append(r)
    good = [rs for rs in by_session.values()
            if all(ca.has_cost_fields(r) for r in rs) and ca.proxy_session_cost(rs)["reconciled"]]
    calls = sum(len(rs) for rs in good)
    if calls < MIN_N:
        return None
    sums = [ca.proxy_session_cost(rs) for rs in good]
    real = sum(x["real_anthropic_usd"] for x in sums)
    avoided = sum(x["est_avoided_usd"] for x in sums)
    legacy = len(rows) - calls
    value = (f"reconciled: ${avoided:.2f} avoided (counterfactual on the ledger's real usage), "
             f"real Anthropic spend ${real:.2f} [period={_period_for_days(days)}; {len(good)} session(s), "
             f"n={calls} calls; {legacy} ledger call(s) not reconciled, excluded; NOT session-kind filtered]")
    stamps = [r["ts"] for rs in good for r in rs if isinstance(r.get("ts"), (int, float))]
    return _measured(value, calls, newest_ts=max(stamps) if stamps else None, seen=calls,
                      reconciled=True, avoided_usd=round(avoided, 4),
                      real_anthropic_usd=round(real, 4), sessions=len(good))


def _o1_quota_avoided(days: int, win: "_Window | None" = None) -> dict:
    try:
        reconciled = _o1_reconciled(days, win)
    except Exception:  # noqa: BLE001 -- a ledger read problem keeps the "est." figure
        reconciled = None
    if reconciled is not None:
        return reconciled
    if win is not None:
        # The "est." figure is usage.db's summary(period), a window relative to now.
        return _not_measurable("no reconciled proxy calls in the absolute window, and the "
                               "usage.db estimate only exists for a period relative to now")
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
            "session tagging; not reconciled against the proxy ledger]")
    if n < MIN_N:
        return _too_few(n, newest_ts=newest) | {"value": f"{TOO_FEW} ({display}, n={n})"}
    return _measured(note, n, newest_ts=newest, estimated_usd=s.estimated_usd)


# ── O2 / D5: frozen benchmark only ────────────────────────────────────────────

def _benchmark_path() -> Path | None:
    raw = os.environ.get("LLM_ROUTER_KPI_BENCHMARK_PATH", "").strip()
    return Path(raw).expanduser() if raw else None


#: Reasons printed when the benchmark file is configured but cannot be used. The
#: code is stable so an operator can grep for it; none of them is ever a 0%.
BENCH_MISSING = "CHZ-KPI-BENCH-MISSING"
BENCH_MALFORMED = "CHZ-KPI-BENCH-MALFORMED"


def _load_benchmark() -> dict | None:
    """The parsed benchmark, or None when unconfigured, missing or malformed.

    A problem is never an exception and never a number: ``_bench_problem`` names it
    (with a stable code) and O2/D5 report "not measurable" with that reason."""
    path = _benchmark_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _bench_problem() -> str | None:
    """Why a configured benchmark is unusable, with its code; None if fine or unset."""
    path = _benchmark_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"{BENCH_MISSING}: {path} does not exist"
    except OSError as exc:
        return f"{BENCH_MISSING}: {path} unreadable ({type(exc).__name__})"
    except ValueError:
        return f"{BENCH_MALFORMED}: {path} is not valid JSON"
    if not isinstance(data, dict):
        return f"{BENCH_MALFORMED}: {path} top level is not a JSON object"
    return None


def _no_bench_reason() -> str:
    return _bench_problem() or (
        "no LLM_ROUTER_KPI_BENCHMARK_PATH configured (KPI-SPEC calls this 'not measured')")


def _bench_newest(bench: dict) -> float | None:
    return _parse_ts(bench.get("generated_at")) if isinstance(bench, dict) else None


def _o2_quality_held(bench: dict | None) -> dict:
    if bench is None:
        return _not_measurable(_no_bench_reason())
    o2 = bench.get("o2") if isinstance(bench, dict) else None
    if not isinstance(o2, dict) or "acceptable_rate" not in o2:
        return _not_measurable("benchmark file has no 'o2' section")
    n = o2.get("n")
    if not isinstance(n, int) or n <= 0:
        return _not_measurable("benchmark 'o2.n' missing or zero")
    if n < MIN_N:
        return _too_few(n, newest_ts=_bench_newest(bench)) | {"lines": [
            f"unscored, below n={MIN_N}: {_pct(o2['acceptable_rate'])} acceptable (n={n})"]}
    return _measured(f"{_pct(o2['acceptable_rate'])} acceptable vs Claude (n={n}, frozen set)", n,
                      newest_ts=_bench_newest(bench), generated_at=bench.get("generated_at"))


def _d5_never_haiku(d5: dict) -> bool:
    """True only when the benchmark says the cheapest tier was predicted zero times.
    A missing ``haiku`` key (or no counts at all) is unknown, not zero."""
    counts = d5.get("predicted_tier_counts")
    return isinstance(counts, dict) and "haiku" in counts and not counts["haiku"]


def _d5_predicted_line(d5: dict) -> list[str]:
    """The predicted tier distribution, plus a qualifier when the classifier never
    predicted the cheapest tier: an under-route rate of 0% is then vacuous."""
    counts = d5.get("predicted_tier_counts")
    if not isinstance(counts, dict) or not counts:
        return []
    dist = ", ".join(f"{t} {counts[t]}" for t in ("haiku", "sonnet", "opus") if t in counts)
    line = f"classifier predicted: {dist}"
    if _d5_never_haiku(d5):
        line += " -- never predicted haiku, so the under-route rate is not informative"
    return [line]


def _d5_classifier_accuracy(bench: dict | None) -> dict:
    if bench is None:
        return _not_measurable(_no_bench_reason())
    d5 = bench.get("d5") if isinstance(bench, dict) else None
    if not isinstance(d5, dict) or "accuracy" not in d5:
        return _not_measurable("benchmark file has no 'd5' section")
    n = d5.get("n")
    if not isinstance(n, int) or n <= 0:
        return _not_measurable("benchmark 'd5.n' missing or zero")
    under = d5.get("under_route_rate")
    dist = _d5_predicted_line(d5)
    if n < MIN_N:
        under_n = f", under-route={_pct(under)}" if isinstance(under, (int, float)) else ""
        return _too_few(n, newest_ts=_bench_newest(bench)) | {"lines": [
            f"unscored, below n={MIN_N}: {_pct(d5['accuracy'])} exact-tier accuracy{under_n} (n={n})",
            *dist]}
    under_s = f", under-route={_pct(under)}" if isinstance(under, (int, float)) else ""
    never = _d5_never_haiku(d5)
    gate = "" if not isinstance(under, (int, float)) else (
        " (within <=10% gate)" if under <= 0.10 else " (OVER the <=10% gate)")
    if never:  # an under-route rate is vacuous when the cheapest tier is never predicted
        gate = " (never predicted haiku: under-route not informative)"
    res = _measured(f"{_pct(d5['accuracy'])} exact-tier accuracy{under_s}{gate} (n={n})", n,
                    newest_ts=_bench_newest(bench), generated_at=bench.get("generated_at"),
                    lines=dist)
    if never:
        res["health_note"] = "never predicted haiku: under-route rate not informative"
    return res


# ── G2: fail-open events per 100 calls ─────────────────────────────────────────

_TOP_CODES = 5


def _g2_silent_failures(days: int, now: float, proxy_rows: list[dict]) -> dict:
    from llm_router import failopen
    from llm_router import hook_latency as hl

    try:
        snap = failopen.snapshot()
    except Exception as exc:  # noqa: BLE001
        return _not_measurable(f"failopen.snapshot() raised: {type(exc).__name__}")
    total = snap.total
    if total is None:
        return _not_measurable("fail-open store is present but unreadable")

    since_req = now - days * 86400.0
    probe = failopen.windowed(until=now)
    if not probe.readable:
        return _not_measurable("fail-open store is present but unreadable")
    hook_all = hl.read_rows(until=now)
    evidence = [t for t in (probe.first_ts, hook_all[0]["ts"] if hook_all else None) if t is not None]
    alltime = (f"all-time: {total} fail-open event(s) recorded, {probe.untimestamped} of them "
               "from before per-event timestamps (cannot be placed in any window)")
    if not evidence:
        return _not_measurable(
            "no timestamped fail-open event and no recorded hook call exist yet, so there is "
            "no window to take a rate over") | {"lines": [alltime]}

    # The window starts at the first timestamped evidence, never earlier: the
    # denominator must not count calls from a period the numerator cannot see.
    since = max(since_req, min(evidence))
    win = failopen.windowed(since=since, until=now)
    hook_ts = [r["ts"] for r in hook_all if since <= r["ts"] <= now]
    proxy_ts = [t for t in (_num_ts(r.get("ts")) for r in proxy_rows) if t is not None and since <= t <= now]
    # A host-killed hook writes no hook_latency row but DOES leave a CHZ-HOOK-KILLED
    # event, which is in the numerator. Count it in the denominator too: it was an
    # invocation. Without it the rate is biased upward by the kill rate.
    hook_killed = win.by_code.get("CHZ-HOOK-KILLED", 0)
    hook_calls, proxy_calls = len(hook_ts) + hook_killed, len(proxy_ts)
    calls = hook_calls + proxy_calls
    observed = len(hook_ts) + len(proxy_ts)                 # calls that left a row
    kill_ts = win.last_ts_by_code.get("CHZ-HOOK-KILLED")
    # Every timestamp that counts toward ``calls``, kill events included; the list
    # can be empty only when calls == 0.
    stamps = hook_ts + proxy_ts + ([kill_ts] if hook_killed and kill_ts is not None else [])
    newest = max(stamps) if stamps else None                 # the feed's last sign of life
    events = win.in_window
    lines = [alltime]
    if since > since_req:
        lines.append(f"window starts at the first timestamped evidence, {_iso(since)} "
                     f"(requested {days}d back)")
    base = {"all_time_total": total, "untimestamped": probe.untimestamped, "events": events,
            "calls": calls, "hook_calls": hook_calls, "hook_killed": hook_killed,
            "proxy_calls": proxy_calls,
            "window_start": round(since, 3), "lines": lines}

    if calls == 0:
        return _not_measurable(f"no hook invocation or proxy call recorded since {_iso(since)}") | base
    if observed == 0:
        # Every counted call is a host kill: the denominator is the numerator's own
        # events, so the rate would be 100% by construction. Say so, don't print it.
        return _not_measurable(
            f"n={calls} call(s) from killed hooks only (no hook_latency row or proxy call in "
            f"the window since {_iso(since)}), so a rate would be 100% by construction",
            seen=calls, newest_ts=newest) | base
    if calls < MIN_N:
        return _too_few(calls, newest_ts=newest) | {"value": f"{TOO_FEW} (n={calls} calls; {events} fail-open "
                                                             f"event(s) so far)"} | base
    if probe.first_ts is None:
        return _not_measurable(
            "no timestamped fail-open event has ever been recorded here, so 0 cannot be told "
            "from 'timestamps are not being written'", seen=calls, newest_ts=newest) | base

    by_code = dict(sorted(win.by_code.items(), key=lambda kv: (-kv[1], kv[0])))
    top = list(by_code.items())[:_TOP_CODES]
    rate = events / calls * 100.0
    lines.insert(0, f"calls: {hook_calls} hook invocation(s) (incl. {hook_killed} killed by the host, "
                    f"which leave no latency row) + {proxy_calls} proxy call(s), all "
                    "session kinds (a fail-open row names no session)")
    for i, (code, n) in enumerate(top):
        lines.insert(1 + i, f"  {code}: {n / calls * 100.0:.2f} per 100 calls ({n})")
    return _measured(
        f"{rate:.2f} per 100 calls ({events} events / {calls} calls, "
        f"{(now - since) / 86400.0:.1f}d window)", calls, newest_ts=newest,
        by_code=by_code,
        by_code_per_100={c: round(n / calls * 100.0, 3) for c, n in by_code.items()},
        top_codes=[{"code": c, "events": n, "per_100_calls": round(n / calls * 100.0, 3)}
                                    for c, n in top],
        rate_per_100=round(rate, 3), **{k: v for k, v in base.items() if k != "lines"},
        lines=lines,
    )


def _killed_hooks(days: int, now: float) -> int | None:
    """``CHZ-HOOK-KILLED`` events in the window, or ``None`` when no timestamped
    fail-open event exists (the count would be a guess, not a zero)."""
    from llm_router import failopen

    probe = failopen.windowed(until=now)
    if not probe.readable or probe.first_ts is None:
        return None
    by_code = failopen.windowed(since=now - days * 86400.0, until=now).by_code
    # Timestamped events exist (checked above), so no entry is a real 0, not an unknown.
    return by_code["CHZ-HOOK-KILLED"] if "CHZ-HOOK-KILLED" in by_code else 0


# ── G4: wrongly benched providers ──────────────────────────────────────────────

def _g4_wrongly_benched(days: int, now: float) -> dict:
    from llm_router import provider_bench_log as bl
    from llm_router import provider_reset

    lines: list[str] = []
    try:
        resets = provider_reset.all_provider_resets(now)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"benched right now: unreadable ({type(exc).__name__})")
    else:
        if resets:
            lines.append("benched right now: " + ", ".join(
                f"{n} until {_iso(u)}" for n, u in sorted(resets.items())) + " (point-in-time)")
        else:
            lines.append("benched right now: none (point-in-time)")

    j = bl.judge(since=now - days * 86400.0, now=now)
    base = {"benches": j.benches, "wrong": j.wrong, "wrong_by_unban": j.wrong_by_unban,
            "wrong_by_success": j.wrong_by_success, "active": j.active,
            "by_trigger": dict(j.by_trigger), "lines": lines}
    if j.benches == 0:
        return _not_measurable(
            "no provider bench recorded in window (0 benches: 'none were wrong' cannot be told "
            "from 'none happened')") | base
    base["health_note"] = ("benches are rare events: an old newest bench is not a stopped feed, "
                           "and a bench still in force can yet turn out wrong")
    triggers = ", ".join(f"{t} {n}" for t, n in sorted(j.by_trigger.items()))
    lines.append(f"benches by trigger: {triggers}")
    lines.append(f"wrong: {j.wrong_by_unban} cleared by the owner (provider unban), "
                 f"{j.wrong_by_success} succeeded before the reset time (a bench can be both)")
    if j.active:
        lines.append(f"{j.active} bench(es) still in force and not shown wrong -- they can "
                     "still turn out wrong, so the wrong count is a floor")
    if j.benches < MIN_N:
        return _too_few(j.benches, newest_ts=j.newest_ts) | {
            "value": f"{TOO_FEW} (n={j.benches} benches); {j.wrong} shown wrong so far (target 0)"} | base
    # "None shown wrong" is not "none wrong": a bench that is still in force can
    # yet turn out wrong (see the lines above), so this never says the target is met.
    gate = "none shown wrong yet (target 0)" if j.wrong == 0 else "OVER the =0 target"
    return _measured(
        f"{j.wrong / j.benches * 100.0:.1f} wrong benches per 100 ({j.wrong} / {j.benches} benches, "
        f"{days}d; {gate})", j.benches, newest_ts=j.newest_ts,
        rate_per_100=round(j.wrong / j.benches * 100.0, 3), **{k: v for k, v in base.items() if k != "lines"},
        lines=lines,
    )


def _local_shadow_summary(days: int, win: "_Window | None" = None) -> dict:
    """Count of local (shadow) units by task type. Reads ``northstar.local_shadow_units``,
    a stream ``units()`` never yields, so this line cannot move NS, D1 or D2."""
    from llm_router import northstar as ns

    by_task: dict[str, int] = {}
    try:
        for u in ns.local_shadow_units(days=_wall_days(days, win)):
            if win is not None and not win.covers(u.get("ts")):
                continue
            key = u.get("task_type") or "unknown"
            by_task[key] = by_task.get(key, 0) + 1
    except Exception:  # noqa: BLE001 -- informational line must never break the scorecard
        by_task = {}
    return {"n": sum(by_task.values()), "by_task_type": dict(sorted(by_task.items()))}


def _local_shadow_line(summary: dict | None) -> str | None:
    if not summary or not summary.get("n"):
        return None
    by = ", ".join(f"{k} {v}" for k, v in sorted(summary["by_task_type"].items(), key=lambda kv: -kv[1]))
    return (f"local (shadow): n={summary['n']}, by task type: {by} "
            "(provenance=runtime, served by ollama; informational, never in NS, D1 or D2)")


# ── O3: offload share (see offload_share.py and KPIS.md) ───────────────────

def _o3_from_units(units: list[dict], local_no_session: int = 0, n_escalations: int | None = None,
                   assist: list[dict] | None = None, n_detector_flags: int | None = None) -> dict:
    """One O3 result over already-judged units. HEADLINE = human turns (first call of each
    turn, see offload_share); per-call figures are the secondary line. ``assist`` = the local
    MCP units: answers inside Claude turns, so NOT turns (reported apart, M0.3a)."""
    from llm_router import offload_share as osh

    calls = osh.summarize(units)
    s = osh.summarize(osh.turn_units(units))
    n, h, loc, cl = s["n"], s[osh.CLASS_HAIKU], s[osh.CLASS_LOCAL], s[osh.CLASS_CLAUDE]
    counts = f"haiku {h['n']}, local {loc['n']}, claude {cl['n']}"
    out_bd = {"unit": "human_turn", "n": n, "haiku_n": h["n"], "haiku_redone": h["redone"],
              "local_n": loc["n"], "local_redone": loc["redone"], "claude_n": cl["n"],
              "offload_kept": s["offload_kept"], "window_open": s["window_open"],
              "local_no_session": local_no_session,
              "local_assist_n": len(assist or ()),
              "local_assist_redone": sum(1 for u in (assist or ()) if u["redone"]),
              "per_call": {"n": calls["n"], "offload_kept": calls["offload_kept"],
                           "haiku_n": calls[osh.CLASS_HAIKU]["n"], "haiku_redone": calls[osh.CLASS_HAIKU]["redone"],
                           "local_n": calls[osh.CLASS_LOCAL]["n"], "window_open": calls["window_open"]}}
    if n == 0:
        out = _not_measurable("no organic turn in window")
    elif n < MIN_N:
        out = _too_few(n, newest_ts=s["newest_ts"])
        out["lines"] = [f"turns so far: {counts}; no rate is printed below n={MIN_N}"]
    else:
        value = s["offload_kept"] / n
        out = _measured(f"{_pct(value)} (n={n} turns, {s['window_open']} window-open)", n,
                        newest_ts=s["newest_ts"], numerator=s["offload_kept"], denominator=n,
                        meets_target=value >= osh.TARGET)

        def redo(c: dict, name: str) -> str:
            if c["n"] == 0:
                return f"{name} not measurable: none in window"
            return f"{name} {_rate_result(c['redone'], c['n'], label=name + ' unit')['value']}"

        # Local turns are proxy-served local calls and zero-Claude edit turns. The MCP-local
        # answers (inside a Claude turn) are NOT turns: they are reported on their own line below.
        local_share = f"local share {_pct(loc['n'] / n)} ({loc['n']}/{n})"
        out["lines"] = [
            f"Haiku share {_pct(h['n'] / n)} ({h['n']}/{n} turns) | {local_share} | claude {cl['n']}/{n}",
            f"redo rate (per turn): {redo(h, 'Haiku')} | {redo(loc, 'local')}",
        ]
        cn = calls["n"]
        if cn >= MIN_N:
            out["lines"].append(f"per call: {_pct(calls['offload_kept'] / cn)} (n={cn} calls; "
                                f"{cn / n:.1f} calls per turn)")
        else:
            out["lines"].append(f"per call: too few to tell (n={cn} calls)")
    if assist is not None and (assist or local_no_session):
        out.setdefault("lines", []).append(
            f"local MCP answers inside Claude turns (not turns, outside n and the numerator): "
            f"{len(assist)} ({out_bd['local_assist_redone']} redone)"
            + (f"; {local_no_session:,} more with no session id excluded" if local_no_session else ""))
    if n_escalations is not None:
        out_bd["n_escalations"] = n_escalations
        out.setdefault("lines", []).append(
            ("redo signal sparse: " if n_escalations < MIN_N else "redo signal: ")
            + f"n_escalations={n_escalations} in window"
            + (" (a low redo rate is NOT proven low: there is little signal to detect a redo)"
               if n_escalations < MIN_N else ""))
    if n_detector_flags is not None:
        # Source 4 (redo_signal.py): reported even while it is disabled, so its volume is visible before
        # anyone proposes to count it (PLAN M0.9).
        enabled = osh.REDO_SOURCE4_ENABLED
        out_bd["redo_detector_n"] = n_detector_flags
        out_bd["redo_detector_enabled"] = enabled
        out.setdefault("lines", []).append(
            f"redo detector (source 4, transcript): {n_detector_flags} flagged prompt(s) in window, "
            + ("counted in redo" if enabled else "NOT counted in redo (disabled until validated)"))
    out["breakdown"] = out_bd
    return out


def _o3_unit_inputs(days: int, index, now: float, win: "_Window | None" = None
                    ) -> tuple[list[dict], set[str], list[dict], list[dict], set[str]]:
    """(local MCP units, receipt-band redone msg_ids, usage_outcome rows, edit_outcomes rows,
    receipt-band kept msg_ids). Each source that cannot be read yields nothing rather than
    breaking the scorecard."""
    from llm_router import northstar as ns
    from llm_router import usage_outcome as uo
    from llm_router import user_signal

    try:
        local = list(ns.local_shadow_units(days=_wall_days(days, win)))
    except Exception:  # noqa: BLE001
        local = []
    try:
        latest = user_signal.latest_by_key(
            since=now - days * 86400.0, until=None if win is None else win.until)
    except Exception:  # noqa: BLE001
        latest = {}
    band = {k for k, row in latest.items() if row.get("signal") == user_signal.SIGNAL_REDONE}
    kept = {k for k, row in latest.items() if row.get("signal") == user_signal.SIGNAL_KEPT}
    try:
        outcomes = uo.judge_recent(days=_wall_days(days, win))
    except Exception:  # noqa: BLE001
        outcomes = []
    try:
        edits = list(ns._load_edit_outcomes())
    except Exception:  # noqa: BLE001
        edits = []
    return local, band, outcomes, edits, kept


#: Below this G3 session_kind completeness, O3 is printed with a bound (M0.3d): the untagged
#: rows are in neither side of the headline, and may or may not be organic.
O3_BOUND_BELOW = 0.95


#: M0-2, the O3 integrity gate (PLAN M0.3): O3 turns counted from the proxy ledger / typed prompts
#: in the Claude Code transcript, over the pinned window W0, on the only session that qualified
#: (>= 50 prompts, >= 1 proxy row). The bar was 0.90-1.10; the measured ratio is 3.89 (813 / 209), so the
#: gate FAILED. The owner decision of 2026-10-07 is to accept O3 as is, with a warning, so every
#: surface that prints O3 says so. This is the ONE place the figures live: ``o3_caveat()`` builds the
#: text from them. Source of the figures: ``~/.rsi/research/primary-plan/gates/M0.3_o3_integrity_W0.json``
#: (``o3_integrity.py --since 2026-09-29T09:41:51Z --until 2026-10-06T09:41:52Z``, exit 1).
#: Re-run that script and edit THIS dict (and PLAN M0-2) when the turn definition changes.
O3_INTEGRITY_GATE: dict[str, Any] = {
    "gate": "M0-2",
    "status": "owner-accepted with warning",
    "accepted_on": "2026-10-07",
    "turns": 813,
    "typed_prompts": 209,
    "bar": [0.90, 1.10],
    "session": "b9f04425",
    "session_kind": "research",
    "sessions_measurable": 1,
    "window": "2026-09-29T09:41:51Z..2026-10-06T09:41:52Z",
}


def o3_caveat(gate: dict[str, Any] | None = None) -> str:
    """The one-line warning printed on every O3 surface (scorecard, --health, --json, weekly)."""
    g = gate or O3_INTEGRITY_GATE
    ratio = g["turns"] / g["typed_prompts"]
    n = g["sessions_measurable"]
    scope = "on the only measurable session" if n == 1 else f"across {n} measurable sessions"
    return (f"O3 turn count unvalidated: may overcount turns (integrity {ratio:.2f}x {scope}, "
            f"{g['session_kind']}; owner accepted {g['accepted_on']})")


def _o3_with_caveat(o3: dict) -> dict:
    """Attach the M0-2 warning to an O3 result: ``caveat`` (the text) and ``integrity`` (the figures,
    with the ratio computed, so a JSON reader gets the number and not only the sentence)."""
    g = O3_INTEGRITY_GATE
    o3["caveat"] = o3_caveat()
    o3["integrity"] = {**g, "ratio": round(g["turns"] / g["typed_prompts"], 3)}
    return o3


def _o3_offload_share(days: int, allowed: frozenset[str], index, all_rows: list[dict],
                      now: float, since_policy: str | None = None,
                      win: "_Window | None" = None, g3: dict | None = None) -> dict:
    from llm_router import northstar as ns
    from llm_router import offload_share as osh
    from llm_router import redo_signal

    try:
        local, band, outcomes, edits, kept = _o3_unit_inputs(days, index, now, win)
        from llm_router import o3_transcripts

        thread_of = o3_transcripts.thread_lookup()
        detector_flags = redo_signal.session_flags_loader(ns.claude_projects_dir())

        def build(untagged_organic: bool = False) -> dict:
            def kind_of(sid, stamp):
                k = index.resolve(sid, stamp=stamp).kind
                return "organic" if k is None and untagged_organic else k
            return osh.build_units(
                all_rows, local, now=now, days=days, allowed=allowed, kind_of=kind_of,
                band_redone=band, outcome_redos=outcomes, edit_rows=edits, thread_of=thread_of,
                detector_flags=detector_flags, band_kept=kept)

        built = build()
        res = _o3_from_units(built["units"], built["local_no_session"], built["n_escalations"],
                             assist=built["local_assist"], n_detector_flags=built["n_detector_flags"])
        res["local_answers"] = _local_answers_result(
            osh.local_answers(built["units"] + built["local_assist"]), built["local_no_session"],
            local_failed=built["local_failed"], local_untagged=built["local_untagged"],
            local_other_kind=built["local_other_kind"])
        res["excluded"] = {"side_call": built["side_call_excluded"], "untagged": built["untagged"],
                           "other_kind": built["other_kind"], "local_no_session": built["local_no_session"],
                           "subagent_first": built["subagent_first"],
                           "edit_no_session": built["edit_no_session"],
                           "edit_no_turn_id": built["edit_no_turn_id"],
                           "local_failed": built["local_failed"],
                           "local_untagged": built["local_untagged"],
                           "local_other_kind": built["local_other_kind"]}
        # NOT excluded: turn rows the transcript says were not a typed prompt's first answer. They
        # stay in n (PLAN M0.3b, "unjoined rows stay in"); the counts say how big that gap is.
        res["kept_in"] = {"meta_first": built["meta_first"], "unjoined": built["unjoined"],
                          "no_transcript": built["no_transcript"]}
        note = (f"{res['breakdown']['window_open']} turn(s) have fewer than {osh.REDO_TURNS} human turns "
                f"after them (counted as not redone, may still change); excluded: "
                f"{built['side_call_excluded']:,} Claude Code side call(s), "
                f"{built['subagent_first']:,} sub-agent first call(s), "
                f"{built['untagged']:,} untagged, {built['other_kind']:,} other-kind; "
                f"kept in n although the transcript says they were not a typed prompt's first answer: "
                f"{built['meta_first']:,} injected-input first call(s) (slash command, sub-agent "
                f"hand-back, task notification), {built['unjoined']:,} call(s) in no message of their "
                f"session's transcript; {built['no_transcript']:,} turn row(s) with no transcript "
                f"(may include sub-agent first calls)")
        if built["local_failed"]:
            note += f", {built['local_failed']:,} local call(s) the router flagged failed (success=0)"
        res["lines"] = list(res.get("lines", ())) + [note]
        cov = ((g3 or {}).get("fields") or {}).get("session_kind", {}).get("coverage")
        if isinstance(cov, (int, float)) and cov < O3_BOUND_BELOW:
            res["bound"] = _o3_bound(res, build(untagged_organic=True), float(cov))
            res["lines"].append(res["bound"]["line"])
        if since_policy:
            res["since_policy"] = _o3_since_view(built["units"], all_rows, since_policy, now, days,
                                                  built["local_no_session"], built["local_assist"])
    except Exception as exc:  # noqa: BLE001 -- an O3 failure must not take the scorecard down
        res = _not_measurable(f"O3 computation failed ({type(exc).__name__})")
    return res


def _o3_bound(headline: dict, alt_built: dict, coverage: float) -> dict:
    """O3 as a range when ``session_kind`` is < 95% complete (M0.3d). ``untagged_excluded`` is the
    headline (untagged rows treated as non-organic); ``untagged_included`` treats them as organic.
    ``lower`` and ``upper`` are the smaller and larger of the two. Each end needs the headline's
    minimum n, else it is None."""
    alt = _o3_from_units(alt_built["units"], alt_built["local_no_session"], alt_built["n_escalations"],
                         assist=alt_built["local_assist"])

    def val(r: dict) -> float | None:
        return (r["numerator"] / r["denominator"]) if r.get("measurable") else None

    excl, incl = val(headline), val(alt)
    both = [v for v in (excl, incl) if v is not None]
    lower, upper = (min(both), max(both)) if both else (None, None)
    out = {"g3_session_kind_coverage": round(coverage, 4), "untagged_excluded": excl,
           "untagged_included": incl, "lower": lower, "upper": upper,
           "n_untagged_excluded": headline.get("n"), "n_untagged_included": alt.get("n")}
    out["line"] = (f"O3 bound (session_kind {_pct(coverage)} complete, below {_pct(O3_BOUND_BELOW)}): "
                   + (f"{_pct(lower)} to {_pct(upper)} (untagged rows counted as non-organic, then as organic)"
                      if lower is not None else "not measurable at both ends (n below the minimum)"))
    return out


def _o3_since_view(units: list[dict], all_rows: list[dict], version: str, now: float, days: int,
                   local_no_session: int = 0, assist: list[dict] | None = None) -> dict:
    from llm_router import offload_share as osh

    start = osh.policy_start(all_rows, version)
    if start is None:
        return {"version": version, "start_ts": None,
                "since": _not_measurable(f"policy version {version} not seen in the proxy ledger"),
                "before": None}
    a = assist or []
    since = _o3_from_units([u for u in units if u["ts"] >= start], local_no_session,
                           assist=[u for u in a if u["ts"] >= start])
    before = _o3_from_units([u for u in units if u["ts"] < start], local_no_session,
                            assist=[u for u in a if u["ts"] < start])
    return {"version": version, "start_ts": start, "start": _iso(start), "since": since, "before": before}


def _local_answers_result(la: dict, local_no_session: int, *, local_failed: int = 0,
                          local_untagged: int = 0, local_other_kind: int = 0) -> dict:
    """The local answers line: served, accepted and the accept rate (offload_share.local_answers).
    The rate is accepted / decided (accepted + redone); pending, unjudged, failed and
    no-session units are stated beside it and never counted on either side. Below MIN_N
    decided it prints ``too few to tell``; with nothing decided, ``not measurable``: never
    0% for unknown."""
    decided = la["accepted"] + la["redone"]
    rate = _rate_result(la["accepted"], decided, label="decided local answer")
    out = dict(rate)
    out["breakdown"] = {**la, "decided": decided, "no_session": local_no_session, "failed": local_failed,
                        "untagged": local_untagged, "other_kind": local_other_kind}
    out["value"] = f"{la['served']} served, {la['accepted']} accepted, accept rate {rate['value']}"
    detail = (f"{la['redone']} redone, {la['pending']} not decided yet (fewer than "
              f"2 human turns after them, or an edit not applied yet); "
              f"{la['unjudged']} with a usage verdict of unknown (no result, no edits, not "
              f"applied): not judged; {la['kept']} kept on the receipt band; "
              f"{la['joined']} joined to their transcript call by tool_use id")
    if local_failed:
        detail += (f"; {local_failed:,} flagged failed by the router (success=0): not served, "
                   "not in any count")
    if local_no_session:
        detail += (f"; {local_no_session:,} local answer(s) with no session id: unknown, "
                   "not judged and not in any count")
    if local_untagged:
        detail += (f"; {local_untagged:,} local answer(s) from sessions with no kind tag: "
                   "not judged and not in any count")
    if local_other_kind:
        detail += (f"; {local_other_kind:,} from a research/other-kind session: "
                   "not organic, not in any count")
    out["lines"] = [detail]
    return out


def _o3_render_lines(o3: dict) -> list[str]:
    """The O3 block as printed by `kpi`: one headline line, then detail lines, then the
    local answers line (its own line, outside NS and the other KPIs like O3 itself)."""
    lines = [f"  {_LABELS['O3']:<42s} {o3['value']}"]
    if o3.get("caveat"):
        lines.append(f"      WARNING: {o3['caveat']}")
    lines += [f"      {x}" for x in o3.get("lines", ())]
    la = o3.get("local_answers")
    if la is not None:
        lines.append(f"  {_LABELS['O3_local']:<42s} {la['value']}")
        lines += [f"      {x}" for x in la.get("lines", ())]
    sp = o3.get("since_policy")
    if sp:
        def one(r: dict | None) -> str:
            if r is None:
                return "n/a"
            if r.get("measurable"):
                b = r["breakdown"]
                return (f"{r['value']}; haiku {b['haiku_n']} (redone {b['haiku_redone']}), "
                        f"local {b['local_n']} (redone {b['local_redone']})")
            return r["value"]
        head = f"since policy {sp['version']}" + (f" (first row {sp['start']})" if sp.get("start") else "")
        lines.append(f"      {head}: {one(sp['since'])}")
        if sp.get("before") is not None:
            lines.append(f"      before it, same window: {one(sp['before'])}")
    return lines


def _proxy_shadow_summary(days: int, win: "_Window | None" = None) -> dict:
    """``local_shadow`` records the proxy's shadow mode wrote (``proxy/local_shadow``): a
    file of their own that ``units()`` and the proxy ledger never read, so this cannot move
    NS, D1 or D2. Reason codes and numbers only."""
    from llm_router.proxy import local_shadow

    try:
        recs = local_shadow.read_records(days=_wall_days(days, win))
        if win is not None:
            recs = [r for r in recs if win.covers(r.get("ts"))]
    except Exception:  # noqa: BLE001 -- informational line must never break the scorecard
        recs = []
    compared = [r for r in recs if r.get("agree") is not None]
    lat = sorted(r["local_latency_s"] for r in recs if isinstance(r.get("local_latency_s"), (int, float)))
    attempted = [r for r in recs if r.get("fallback_reason") not in ("skipped_busy", "not_eligible",
                                                                      "media_present", "prompt_over_cap",
                                                                      "kill_switch", "backend_unhealthy",
                                                                      "breaker_open")]
    judged = [r for r in attempted if r.get("schema_valid") is not None]
    out = {"n": len(recs), "n_compared": len(compared),
           "agree": sum(1 for r in compared if r["agree"]),
           "args_equal": sum(1 for r in compared if r.get("args_equal")),
           "schema_judged": len(judged), "schema_invalid": sum(1 for r in judged if r["schema_valid"] is False),
           "no_comparison": sum(1 for r in recs if r.get("agree") is None),
           "p90_latency_s": lat[min(len(lat) - 1, int(0.9 * len(lat)))] if lat else None}
    return out


def _proxy_shadow_line(s: dict | None) -> str | None:
    if not s or not s.get("n"):
        return None
    c = s["n_compared"]
    agree = f"{s['agree'] / c * 100:.0f}% ({s['agree']}/{c})" if c else "n/a (0 compared)"
    args = f"{s['args_equal'] / c * 100:.0f}%" if c else "n/a"
    sch = f"{s['schema_invalid'] / s['schema_judged'] * 100:.1f}%" if s["schema_judged"] else "n/a"
    p90 = f"{s['p90_latency_s']:.1f}s" if s["p90_latency_s"] is not None else "n/a"
    return (f"local shadow (proxy): n={s['n']}, tool-name agreement {agree}, args equal {args}, "
            f"schema-invalid {sch}, p90 local latency {p90}, no comparison {s['no_comparison']}/{s['n']} "
            "(reason codes only; informational, never in NS, D1 or D2)")


def _classifier_shadow_summary(days: float, win: "_Window | None" = None,
                               allowed: "frozenset[str] | None" = None, index=None,
                               ledger_rows: "list[dict] | None" = None) -> dict:
    """The local LLM classifier's shadow log (``proxy/llm_shadow``, M1.6) in the window: how
    often it answered, how often it agreed with the rules, how slow and how cheap it is.
    Its own file, which ``units()`` and the proxy ledger never read, so it cannot move NS, D1
    or D2. Hashes, tiers and numbers only.

    Population: a record counts when its session resolves to an ``allowed`` kind (the stamp
    the record was written with, the ledger and the tag file decide; no kind is never
    organic). ``allowed=None`` counts every record. ``n`` is LLM calls (one row per call,
    cache hits are never logged); ``fallback_rate`` is calls with no usable verdict
    (timeout, parse error, cold model) over ``n``. ``agree`` compares the LLM's tier with
    the rules' ``tier_proposed`` on answered calls where the rules proposed one; ``local``
    counts as Haiku (it is Haiku-and-local-eligible). Both tier distributions and
    ``cheap_share_llm`` are over the answered calls. ``p50_ms`` / ``p95_ms`` are over real
    model calls (``source=llm``). ``calls_per_turn`` is calls over distinct
    (session, text) turns. ``cls_applied_true`` counts ledger rows that say a verdict was
    applied: it must be 0 while the classifier is shadow only."""
    from llm_router.proxy import llm_shadow

    try:
        recs = llm_shadow.read_records(days=_wall_days(days, win))
        if win is not None:
            recs = [r for r in recs if win.covers(r.get("ts"))]
    except Exception:  # noqa: BLE001 -- informational line must never break the scorecard
        recs = []
    kept: list[dict] = []
    excluded = 0
    for r in recs:
        if allowed is None:
            kept.append(r)
            continue
        stamp = r.get("session_kind")
        kind = (index.resolve(r.get("session_id"), stamp).kind if index is not None
                else (session_kind.override_of(r.get("session_id")) or stamp))
        if kind in allowed:
            kept.append(r)
        else:
            excluded += 1
    calls = [r for r in kept if r.get("kind") == llm_shadow.KIND]
    drops = sum(1 for r in kept if r.get("kind") == llm_shadow.KIND_DROP)
    answered = [r for r in calls if r.get("source") in ("llm", "cache")]
    model_ms = sorted(float(r["ms"]) for r in calls
                      if r.get("source") == "llm" and isinstance(r.get("ms"), (int, float)))

    def tier_of(r: dict, side: str) -> str | None:
        t = (r.get(side) or {}).get("tier")
        return t if isinstance(t, str) else None

    def merged(t: str | None) -> str | None:
        return "haiku" if t == "local" else t   # local = Haiku-and-local-eligible

    compared = [r for r in answered if tier_of(r, "rules") and tier_of(r, "llm")]
    llm_dist: dict[str, int] = {}
    rules_dist: dict[str, int] = {}
    for r in answered:
        llm_dist[tier_of(r, "llm") or "none"] = llm_dist.get(tier_of(r, "llm") or "none", 0) + 1
        rules_dist[tier_of(r, "rules") or "none"] = rules_dist.get(tier_of(r, "rules") or "none", 0) + 1
    turns = {(r.get("session_id"), r.get("text_sha")) for r in calls}
    n = len(calls)
    agree = sum(1 for r in compared if merged(tier_of(r, "llm")) == tier_of(r, "rules"))
    applied = [r for r in (ledger_rows or []) if "cls_applied" in r]
    return {
        "n": n,
        "n_sessions": len({r.get("session_id") for r in calls}),
        "n_answered": len(answered),
        "agree": agree,
        "n_compared": len(compared),
        "agree_rate": round(agree / len(compared), 4) if compared else None,
        "llm_tier_dist": dict(sorted(llm_dist.items())),
        "rules_tier_dist": dict(sorted(rules_dist.items())),
        "cheap_share_llm": (round(sum(1 for r in answered if tier_of(r, "llm") in ("local", "haiku"))
                                  / len(answered), 4) if answered else None),
        "fallback_rate": round((n - len(answered)) / n, 4) if n else None,
        "p50_ms": round(_percentile(model_ms, 0.50), 1) if model_ms else None,
        "p95_ms": round(_percentile(model_ms, 0.95), 1) if model_ms else None,
        "drops": drops,
        "drop_rate": round(drops / (n + drops), 4) if (n + drops) else None,
        "calls_per_turn": round(n / len(turns), 4) if n else None,
        "excluded_non_organic": excluded,
        "cls_applied_true": sum(1 for r in applied if r["cls_applied"] is True) if ledger_rows is not None else None,
        "ledger_rows": len(applied) if ledger_rows is not None else None,
    }


def _classifier_shadow_line(s: dict | None) -> str | None:
    if not s or not (s.get("n") or s.get("drops")):
        return None

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x * 100:.1f}%"

    def ms(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.0f} ms"

    agree = f"{s['agree']}/{s['n_compared']}" if s["n_compared"] else "n/a"
    cpt = f"{s['calls_per_turn']:.2f}" if s["calls_per_turn"] is not None else "n/a"
    few = f" ({TOO_FEW}: n < {MIN_N})" if s["n"] < MIN_N else ""
    applied = ""
    if s.get("cls_applied_true"):
        applied = f", WARNING cls_applied true on {s['cls_applied_true']} ledger rows"
    return (f"classifier shadow (proxy): n={s['n']} calls in {s['n_sessions']} sessions{few}, agree {agree}, "
            f"cheap share {pct(s['cheap_share_llm'])}, fallback {pct(s['fallback_rate'])}, "
            f"p50 {ms(s['p50_ms'])}, p95 {ms(s['p95_ms'])}, drops {s['drops']}, {cpt} calls/turn{applied} "
            "(hashes and tiers only; informational, never in NS, D1 or D2)")


# ── P0.14-a: proxy ledger liveness ─────────────────────────────────────────

def _proxy_liveness(now: float, proxy_rows: list[dict]) -> dict:
    from llm_router import proxy_liveness

    return proxy_liveness.liveness(now=now, proxy_rows=proxy_rows)


def _proxy_liveness_lines(live: dict | None) -> list[str]:
    if not live:
        return []
    h = f"{live['window_hours']:g}"

    def n(v: Any) -> str:
        return "unreadable" if v is None else str(v)

    lines = [f"proxy_rows_24h: {live['proxy_rows_24h']} (n={live['proxy_rows_24h']} proxy ledger row(s) "
             f"in the {h} h to generated; hook turns {n(live['hook_turns_24h'])}, "
             f"routing decisions {n(live['routing_decisions_24h'])})"]
    if live.get("warn"):
        lines.append(f"WARN {live['message']}")
    return lines


# ── assembly ───────────────────────────────────────────────────────────────

def compute_scorecard(days: int = 7, *, include_research: bool = False,
                      schema_since: float | None = None, now: float | None = None,
                      since_policy: str | None = None,
                      since: float | None = None, until: float | None = None) -> dict:
    """The scorecard over the last ``days`` days, or over the absolute window
    ``[since, until]`` (epoch seconds; both or neither). A window replaces ``days`` and
    ``now``: "now" becomes ``until``, so a historical check does not drift as time passes."""
    from llm_router import session_kind as sk
    from llm_router.proxy import ledger as pl

    win: _Window | None = None
    if since is not None or until is not None:
        if since is None or until is None:
            raise ValueError("an absolute window needs both since and until")
        if not since < until:
            raise ValueError("since must be before until")
        win = _Window(since, until)
        days, now = win.days, until
    now_ts = time.time() if now is None else now
    allowed = _allowed_kinds(include_research)
    all_rows = pl.read_rows()
    index = sk.KindIndex(all_rows)
    until_ts = None if win is None else win.until
    ns_r, d1_r, d2_r, joins = _ns_d1_d2(days, allowed, index, win)
    kpis_diag = joins.pop("diag")
    d3_r = _fold_user_signals(_d3_redo_rate(days, allowed, index, win), days, now_ts)
    pop = _proxy_population(all_rows, days, allowed, now_ts, until=until_ts)
    d4_r = _d4_tier_mix(pop)
    g1_hook_r = _g1_hook(days, now_ts, _killed_hooks(days, now_ts))
    g1_proxy_r = _g1_proxy(pop)
    g3_r = _g3_completeness(all_rows, days, now_ts, schema_since, until=until_ts)
    o1_r = _o1_quota_avoided(days, win)
    bench = _load_benchmark()
    o2_r = _o2_quality_held(bench)
    d5_r = _d5_classifier_accuracy(bench)
    g2_r = _g2_silent_failures(days, now_ts, all_rows)
    g4_r = _g4_wrongly_benched(days, now_ts)
    card = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "generated_ts": now_ts,
        "window_days": days,
        "include_research": include_research,
        "joins": joins,
        "verify_shadow": _verify_shadow(days),
        # Informational only: not in "kpis", so not in _ORDER, --health or NS/D1/D2.
        "local_shadow": _local_shadow_summary(days, win),
        # O3 is likewise outside "kpis": adding it there would change the key set, _ORDER and
        # the --health counts that NS..G4 consumers read. Rendered as its own line.
        "o3": _o3_with_caveat(
            _o3_offload_share(days, allowed, index, all_rows, now_ts, since_policy, win, g3_r)),
        "proxy_local_shadow": _proxy_shadow_summary(days, win),
        # P0.14-a: is the proxy ledger alive? Outside "kpis" (a liveness check, not a KPI).
        "proxy_liveness": _proxy_liveness(now_ts, all_rows),
        "classifier_shadow": _classifier_shadow_summary(days, win, allowed, index, pop["allowed"]),
        "kpis": {
            "NS": ns_r, "O1": o1_r, "O2": o2_r,
            "D1": d1_r, "D2": d2_r, "D3": d3_r, "D4": d4_r, "D5": d5_r,
            "G1_hook": g1_hook_r, "G1_proxy": g1_proxy_r, "G2": g2_r, "G3": g3_r, "G4": g4_r,
        },
        # Outside "kpis" (so outside KPI_CODES, _ORDER and --health): the heuristic NS and D2
        # numerators, kept for comparison only.
        "kpis_diag": kpis_diag,
    }
    if win is not None:  # only a windowed card carries the key: the default output is unchanged
        card["window_days"] = round(win.days, 6)
        card["window"] = {"since": _iso(win.since), "until": _iso(win.until),
                          "since_ts": win.since, "until_ts": win.until}
    return card


# ── --health: measured / blind / stale, per KPI ──────────────────────────────

STATE_MEASURED = "measured"
STATE_BLIND = "blind"
STATE_STALE = "stale"

#: KPIs read from the frozen benchmark: judged against STALE_BENCHMARK_DAYS.
_BENCHMARK_KPIS = frozenset({"O2", "D5"})


def _age(hours: float) -> str:
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


def _health_entry(r: dict, limit_h: float, now_ts: float) -> dict:
    newest = r.get("newest_ts")
    age_h = (now_ts - newest) / 3600 if newest is not None else None
    if not r["measurable"]:
        # "too few" has an n (the one behind the number); "nothing to count" has only
        # the data points that existed and could not be used.
        state, reason, n = STATE_BLIND, r["reason"], (r["n"] if r["n"] is not None else r["seen"])
    elif age_h is None:
        state, n = STATE_BLIND, r["n"]
        reason = "measured, but its data carries no timestamp: cannot tell how old it is"
    elif age_h > limit_h:
        state, n = STATE_STALE, r["n"]
        reason = f"newest data point is {_age(age_h)} old, over the {_age(limit_h)} limit"
    else:
        state, n = STATE_MEASURED, r["n"]
        reason = f"newest data point {_age(max(age_h, 0.0))} old"
    if r.get("health_note") and state != STATE_BLIND:
        reason = f"{reason} ({r['health_note']})"
    return {"state": state, "reason": reason, "n": n, "newest_ts": newest,
            "newest": _iso(newest), "age_hours": round(age_h, 2) if age_h is not None else None,
            "stale_after_hours": limit_h}


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
        limit_h = STALE_BENCHMARK_DAYS * 24 if key in _BENCHMARK_KPIS else stale_hours
        out[key] = _health_entry(data["kpis"][key], limit_h, now_ts)
    counts = {s: sum(1 for v in out.values() if v["state"] == s)
              for s in (STATE_MEASURED, STATE_BLIND, STATE_STALE)}
    res = {"generated_at": data["generated_at"], "window_days": data["window_days"],
           **({"window": data["window"]} if data.get("window") else {}),
           "stale_after_hours": stale_hours, "benchmark_stale_after_days": STALE_BENCHMARK_DAYS,
           "counts": counts, "kpis": out}
    if data.get("o3") is not None:  # outside "kpis" and "counts": see compute_scorecard
        res["o3"] = _health_entry(data["o3"], stale_hours, now_ts)
        if data["o3"].get("caveat"):
            res["o3"]["caveat"] = data["o3"]["caveat"]
    return res


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
    "G2": "G2  silent failures (fail-open / 100 calls)",
    "G3": "G3  ledger completeness (target >=99%)",
    "G4": "G4  wrongly benched providers (target 0)",
    "O3": "O3  offload share (target >=60%)",
    "O3_local": "local answers (accepted; never in NS)",
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
        f"llm-router kpi -- window={_window_label(data)} "
        f"({'organic + research' if data['include_research'] else 'organic only'}) "
        f"generated={data['generated_at']}",
        "",
    ]
    for key in _ORDER:
        r = data["kpis"][key]
        lines.append(f"  {_LABELS[key]:<42s} {r['value']}")
        for extra in r.get("lines", ()):
            lines.append(f"      {extra}")
        if key == "D2" and data.get("verify_shadow"):
            v = data["verify_shadow"]
            lines.append(f"      verify (shadow): {v['verified']} verified, {v['weak']} weak, "
                         f"{v['failed']} failed, {v['unavailable']} unavailable (informational; not in NS/D1/D2)")
    for key, r in (data.get("kpis_diag") or {}).items():
        if not r.get("measurable"):
            continue  # nothing to compare: the card stays as it was
        lines.append(f"  {key:<42s} {r['value']}   [diagnostic, not a target]")
    if data.get("o3") is not None:
        lines += _o3_render_lines(data["o3"])
    lines.append("")
    local_line = _local_shadow_line(data.get("local_shadow"))
    if local_line:
        lines.append(local_line)
    proxy_shadow_line = _proxy_shadow_line(data.get("proxy_local_shadow"))
    if proxy_shadow_line:
        lines.append(proxy_shadow_line)
    classifier_line = _classifier_shadow_line(data.get("classifier_shadow"))
    if classifier_line:
        lines.append(classifier_line)
    lines += _proxy_liveness_lines(data.get("proxy_liveness"))
    lines.append(_join_line(data["joins"]))
    lines.append("O1 is never session-kind filtered (usage.db predates tagging); G3 is not "
                  "session-kind filtered either (see KPIS.md); neither are G1 (hook), G2 and G4, "
                  "whose rows carry no session id. "
                  "O2/D5 need LLM_ROUTER_KPI_BENCHMARK_PATH. See each line and the module docstring.")
    return "\n".join(lines)


def render_health(health: dict, *, strict: bool = False) -> str:
    lines = [
        f"llm-router kpi --health -- window={_window_label(health)} generated={health['generated_at']}",
        f"  stale = newest data point older than {_age(health['stale_after_hours'])} "
        f"(live ledgers) or {health['benchmark_stale_after_days']:.0f}d (frozen benchmark, O2/D5)",
        "",
    ]
    for key in _ORDER:
        h = health["kpis"][key]
        lines.append(f"  {_LABELS[key]:<42s} {h['state']:<9s} n={str(h['n']):<7} {h['reason']}")
    if health.get("o3") is not None:
        h = health["o3"]
        lines.append(f"  {_LABELS['O3']:<42s} {h['state']:<9s} n={str(h['n']):<7} {h['reason']}"
                     + (f" | WARNING: {h['caveat']}" if h.get("caveat") else ""))
    c = health["counts"]
    verdict = ("exit 1: --strict and a KPI is blind" if strict and c[STATE_BLIND]
               else "exit 0; --strict exits 1 if any KPI is blind")
    lines += ["", f"{c[STATE_MEASURED]} measured, {c[STATE_BLIND]} blind, {c[STATE_STALE]} stale ({verdict})"]
    return "\n".join(lines)


def write_weekly(data: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y-%m-%d", time.gmtime())
    path = out_dir / f"kpi-{date}.md"
    body = ["# llm-router KPI scorecard", "",
            f"Generated {data['generated_at']}, window {_window_label(data)}, "
            f"{'organic + research' if data['include_research'] else 'organic only'}.",
            "", "| KPI | Value |", "|---|---|"]
    for key in _ORDER:
        r = data["kpis"][key]
        body.append(f"| {_LABELS[key]} | {r['value']} |")
    if data.get("o3") is not None:
        body.append(f"| {_LABELS['O3']} | {data['o3']['value']}"
                    + (f" (WARNING: {data['o3']['caveat']})" if data["o3"].get("caveat") else "") + " |")
        if data["o3"].get("local_answers") is not None:
            body.append(f"| {_LABELS['O3_local']} | {data['o3']['local_answers']['value']} |")
    details = [(key, r["lines"]) for key in _ORDER if (r := data["kpis"][key]).get("lines")]
    if details:
        body += ["", "## Details", ""]
        for key, extra in details:
            body.append(f"**{_LABELS[key].strip()}**")
            body += [""] + [f"- {ln.strip()}" for ln in extra] + [""]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _parse_when(raw: str) -> float | None:
    """A ``--since`` / ``--until`` value as epoch seconds: an ISO date or time (naive = UTC)
    or a bare epoch number. None when it is neither."""
    ts = _parse_ts(raw)
    if ts is None:
        try:
            ts = _num_ts(float(raw))
        except ValueError:
            return None
    return ts


def cmd_kpi(args: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="llm-router kpi", add_help=True)
    ap.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    ap.add_argument("--since", metavar="WHEN", default=None,
                     help="absolute window start (YYYY-MM-DD, an ISO time, or epoch seconds); "
                          "needs --until, and with it replaces --days")
    ap.add_argument("--until", metavar="WHEN", default=None,
                     help="absolute window end, same formats; needs --since")
    ap.add_argument("--include", choices=("research",), default=None,
                     help="also count research-tagged sessions (harness is never included)")
    ap.add_argument("--write-weekly", metavar="DIR", default=None,
                     help="write a dated markdown scorecard into DIR")
    ap.add_argument("--json", action="store_true", help="print the raw scorecard dict as JSON")
    ap.add_argument("--schema-since", metavar="WHEN", default=None,
                     help="G3: count proxy rows from WHEN (YYYY-MM-DD, an ISO time, or epoch "
                          "seconds) instead of from the first row that carries the fields")
    ap.add_argument("--since-policy", metavar="VERSION", default=None,
                     help="O3: also show offload share since the first proxy row stamped with this "
                          "tier_policy_version (e.g. 7816b7cf5537), against the same window before it")
    ap.add_argument("--health", action="store_true",
                     help="per KPI: measured, blind or stale, with the one reason and the n")
    ap.add_argument("--strict", action="store_true",
                     help="with --health: exit 1 if any KPI is blind (default exit 0)")
    ap.add_argument("--stale-hours", type=float, default=STALE_LIVE_HOURS,
                     help=f"with --health: a live KPI is stale past this age (default {STALE_LIVE_HOURS:.0f})")
    ap.add_argument("--backfill-tags", action="store_true",
                     help="derive a session_kind for every ledger session with no live tag, from "
                          "its Claude Code transcript (same rules as the live tagger), and append it "
                          "to the sidecar session_kind_backfill.jsonl; a live tag always wins and "
                          "deleting the sidecar restores the untagged behaviour exactly")
    ap.add_argument("--quota-burn", action="store_true",
                     help="S3 / GE6: Claude quota burn per session and per human turn from "
                          "quota_samples.jsonl and quota_history.jsonl; needs --since and --until; "
                          "stale samples are labelled estimated and kept out of the measured line")
    ap.add_argument("--dry-run", action="store_true",
                     help="with --backfill-tags: report what would be written; write nothing")
    ap.add_argument("--validate-backfill", action="store_true",
                     help="read-only: run the backfill rules on sessions that already have a live "
                          "kind and report agreement plus a confusion table (counts only)")
    ap.add_argument("--haiku-watch", action="store_true",
                     help="the daily Haiku Option A watch (PLAN v16 P0.11, D-20) over --since/--until: "
                          "Haiku-decided calls, tier_retry, redo, blind audit and shadow triggers, each "
                          "with its n; exit 1 when a D-20 trigger is not evaluable (the day fails)")
    parsed = ap.parse_args(args)
    if parsed.dry_run and not parsed.backfill_tags:
        ap.error("--dry-run needs --backfill-tags")
    if parsed.strict and not parsed.health:
        ap.error("--strict needs --health")
    if parsed.quota_burn:
        if parsed.since is None or parsed.until is None:
            ap.error("--quota-burn needs --since and --until (an absolute window)")
        q_since, q_until = _parse_when(parsed.since), _parse_when(parsed.until)
        if q_since is None or q_until is None or not q_since < q_until:
            ap.error("--quota-burn: --since and --until must be readable and since < until")
        from llm_router import quota_samples

        burn = quota_samples.quota_burn(q_since, q_until,
                                        include_research=(parsed.include == "research"))
        print(json.dumps(burn, indent=2, default=str) if parsed.json
              else quota_samples.render_quota_burn(burn))
        return 0
    if parsed.validate_backfill:
        from llm_router import session_kind_backfill as skb

        verdict = skb.validate_against_live()
        print(json.dumps(verdict, indent=2) if parsed.json else skb.render_validation(verdict))
        return 0
    if parsed.backfill_tags:
        from llm_router import session_kind_backfill as skb

        result = skb.backfill_sessions(dry_run=parsed.dry_run)
        print(json.dumps(result, indent=2, sort_keys=False) if parsed.json
              else skb.render_backfill_report(result))
        return 0
    since = None
    if parsed.schema_since is not None:
        since = _parse_ts(parsed.schema_since)
        if since is None:
            try:
                since = _parse_ts(float(parsed.schema_since))
            except ValueError:
                ap.error(f"--schema-since: cannot read {parsed.schema_since!r} as a date or epoch")

    win_since = win_until = None
    if (parsed.since is None) != (parsed.until is None):
        ap.error("--since and --until must be given together")
    if parsed.since is not None:
        win_since, win_until = _parse_when(parsed.since), _parse_when(parsed.until)
        if win_since is None:
            ap.error(f"--since: cannot read {parsed.since!r} as a date, an ISO time or epoch seconds")
        if win_until is None:
            ap.error(f"--until: cannot read {parsed.until!r} as a date, an ISO time or epoch seconds")
        if not win_since < win_until:
            ap.error("--since must be before --until")
    if parsed.haiku_watch:
        if win_since is None:
            ap.error("--haiku-watch needs --since and --until (an absolute window)")
        from llm_router.proxy import haiku_guard

        watch = haiku_guard.watch(win_since, win_until)
        print(json.dumps(watch, indent=2, default=str) if parsed.json else haiku_guard.render_watch(watch))
        return 0 if watch["day_pass"] else 1

    data = compute_scorecard(days=parsed.days, include_research=(parsed.include == "research"),
                             schema_since=since, since_policy=parsed.since_policy,
                             since=win_since, until=win_until)

    exit_code = 0
    if parsed.health:
        health = compute_health(data, stale_hours=parsed.stale_hours)
        print(json.dumps(health, indent=2, default=str) if parsed.json
              else render_health(health, strict=parsed.strict))
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
