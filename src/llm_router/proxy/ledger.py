"""Per-call ledger for the proxy, and the metrics computed from it.

One row per ``POST /v1/messages`` that reached the proxy, written to
``<state>/proxy_calls.jsonl``. A row never holds a header value, a prompt, a
tool result or a reply; only shape, decision, timing and token counts. The
optional ``detail`` string (a fallback reason) is passed through
``secret_scrubber.scrub_text`` and cut to 200 chars.

Row fields:
  ts, session_id, msg_id, stream, requested_model, step_class
  prev_tools      names of the tool calls the newest tool results answer
  decision        served | forwarded | fallback
  reason          why not served: routing_off | not_eligible | policy_kept |
                  budget_exceeded | backend_error | validation   (None if served)
  detail          short scrubbed error text for a fallback
  task_type, complexity, model      the policy's view and the serving model
  route_latency_s     time spent on the non-Claude attempt (served or not)
  upstream_latency_s  Anthropic time for a forwarded / fallback call
  added_latency_s     time the proxy added before Anthropic saw the call
                      (classification + a failed attempt); 0 when not tried
  upstream_status, usage (Anthropic usage, cache split), backend_usage
  served_blocks   e.g. ["tool_use:Read"] for a served reply
  mixed_history   history contains a proxy-served turn
  thinking_retry  Anthropic rejected the mixed history over thinking and the
                  proxy retried once with thinking off
  auth            oauth | api_key | none   (kind only; never the value)

``northstar`` joins ``msg_id`` of served rows to transcript assistant records
(``message.id``) to count those turns as routed.

The metrics are kept separate on purpose (spike, 2026-09-28: half the calls
routed saved about a fifth of the Anthropic cost, because cached re-reads are
already cheap). A routed share is never reported as a saving.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from llm_router import paths
from llm_router.proxy.loop_guard import REASON_LOOP_GUARD

LEDGER_NAME = "proxy_calls.jsonl"

DECISION_SERVED = "served"
DECISION_FORWARDED = "forwarded"
DECISION_FALLBACK = "fallback"


def ledger_path() -> Path:
    return paths.state_path(LEDGER_NAME)


def auth_kind(headers) -> str:
    auth = headers.get("authorization") or ""
    if auth:
        value = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
        return "oauth" if value.startswith("sk-ant-oat") else "bearer"
    if headers.get("x-api-key"):
        return "api_key"
    return "none"


def normalize_usage(usage: dict | None) -> dict:
    """Anthropic usage -> flat ints, with the 5m/1h cache-write split."""
    u = usage or {}
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    out = {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
        "cache_creation_1h": int(cc.get("ephemeral_1h_input_tokens") or 0),
    }
    out["cache_creation_5m"] = max(0, out["cache_creation_input_tokens"] - out["cache_creation_1h"])
    return out


def scrub_detail(text: str | None) -> str | None:
    if not text:
        return None
    from llm_router.secret_scrubber import scrub_text

    return scrub_text(str(text))[:200]


def write_row(row: dict, path: Path | None = None) -> None:
    target = path or ledger_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as exc:
        from llm_router import failopen

        failopen.record("LR-FO-PROXY-LEDGER-WRITE", exc)


def read_rows(path: Path | None = None, days: float | None = None) -> list[dict]:
    target = path or ledger_path()
    cutoff = time.time() - days * 86400 if days else None
    rows: list[dict] = []
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ts = row.get("ts")
        # A row without a timestamp cannot be placed in a window: it is kept
        # only when no window was asked for, never read as "time zero".
        if cutoff is None or (isinstance(ts, (int, float)) and ts >= cutoff):
            rows.append(row)
    return rows


# ── metrics ──────────────────────────────────────────────────────────────────


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def anthropic_cost(row: dict) -> float | None:
    """Estimated USD of the Anthropic side of one row, or None when unpriced."""
    from llm_router import pricing

    model = row.get("requested_model") or ""
    u = normalize_usage(row.get("usage"))
    rates = pricing.rates_per_m(model)
    rate_1h = pricing.cache_write_1h_rate(model)
    if rates is None or rate_1h is None:
        return None
    return (u["input_tokens"] * rates["input"] + u["output_tokens"] * rates["output"]
            + u["cache_read_input_tokens"] * rates["cache_read"]
            + u["cache_creation_5m"] * rates["cache_write"]
            + u["cache_creation_1h"] * rate_1h) / 1_000_000


def stats(rows: list[dict]) -> dict:
    """The four proxy metrics, each with its own n. See the module docstring.

    A call the loop guard flags (``reason == "loop_guard"``, see
    ``proxy.loop_guard``) is a repeat or a forced-fallback of one, never a
    saving: it is excluded from ``served`` defensively (a served row should
    never carry that reason, but the exclusion is explicit rather than
    assumed) and reported separately so a routed share or an avoided-cost
    total cannot be inflated by a runaway session's repeats.
    """
    calls = len(rows)
    loop_flagged = [r for r in rows if r.get("reason") == REASON_LOOP_GUARD]
    served = [r for r in rows if r.get("decision") == DECISION_SERVED and r.get("reason") != REASON_LOOP_GUARD]
    fallback = [r for r in rows if r.get("decision") == DECISION_FALLBACK]
    to_anthropic = [r for r in rows if r.get("decision") in (DECISION_FORWARDED, DECISION_FALLBACK)]
    attempted = served + fallback

    reasons: dict[str, int] = {}
    for r in rows:
        if r.get("decision") != DECISION_SERVED:
            reasons[r.get("reason") or "unknown"] = reasons.get(r.get("reason") or "unknown", 0) + 1

    calls_excl_repeats = calls - len(loop_flagged)
    sessions: dict[str, int] = {}
    for r in rows:
        sid = r.get("session_id") or "unknown"
        sessions[sid] = sessions.get(sid, 0) + 1
    session_counts = sorted(sessions.values())

    tokens = {k: 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                             "cache_creation_5m", "cache_creation_1h")}
    cost = 0.0
    unpriced = 0
    for r in to_anthropic:
        u = normalize_usage(r.get("usage"))
        for k in tokens:
            tokens[k] += u[k]
        c = anthropic_cost(r)
        if c is None:
            unpriced += 1
        else:
            cost += c

    # Estimated avoided cost: each served call priced as the median forwarded
    # CONTINUATION call of the same session (else of all sessions). An estimate
    # with its n, never a measured saving.
    by_session: dict[str, list[float]] = {}
    all_cont: list[float] = []
    for r in to_anthropic:
        c = anthropic_cost(r)
        if c is not None and r.get("step_class"):
            by_session.setdefault(r.get("session_id") or "", []).append(c)
            all_cont.append(c)
    avoided = 0.0
    avoided_n = 0
    for r in served:
        pool = by_session.get(r.get("session_id") or "") or all_cont
        if pool:
            avoided += statistics.median(pool)
            avoided_n += 1

    def _cc(rs: list[dict]) -> list[float]:
        return [normalize_usage(r.get("usage"))["cache_creation_input_tokens"] for r in rs]

    cont_fwd = [r for r in to_anthropic if r.get("step_class")]
    mixed = [r for r in cont_fwd if r.get("mixed_history")]
    clean = [r for r in cont_fwd if not r.get("mixed_history")]

    return {
        "calls": calls,
        "sessions": {"n": len(sessions), "calls_per_session": dict(sorted(sessions.items(),
                                                                            key=lambda kv: -kv[1])),
                     "median_calls_per_session": _median([float(c) for c in session_counts]),
                     "max_calls_per_session": max(session_counts) if session_counts else None},
        "routed_share": {"served": len(served), "calls": calls,
                         "share": round(len(served) / calls, 4) if calls else None},
        "routed_share_excl_repeats": {
            "served": len(served), "calls": calls_excl_repeats, "repeats_excluded": len(loop_flagged),
            "share": round(len(served) / calls_excl_repeats, 4) if calls_excl_repeats else None,
        },
        "fallbacks": {"n": len(fallback), "attempted": len(attempted),
                      "rate": round(len(fallback) / len(attempted), 4) if attempted else None,
                      "not_served_by_reason": dict(sorted(reasons.items()))},
        "latency": {
            "served_median_s": _median([r["route_latency_s"] for r in served if r.get("route_latency_s") is not None]),
            "served_n": len(served),
            "anthropic_median_s": _median([r["upstream_latency_s"] for r in to_anthropic
                                           if r.get("upstream_latency_s") is not None]),
            "anthropic_n": len(to_anthropic),
            "added_total_s": round(sum(r.get("added_latency_s") or 0 for r in rows), 2),
            "added_on_fallback_median_s": _median([r.get("added_latency_s") or 0 for r in fallback]),
            "thinking_retries": sum(1 for r in rows if r.get("thinking_retry")),
        },
        "anthropic": {
            "calls": len(to_anthropic), "tokens": tokens,
            "est_cost_usd": round(cost, 4), "unpriced_calls": unpriced,
            "est_avoided_usd": round(avoided, 4), "est_avoided_n": avoided_n,
            "cache_creation_median_after_served_turn": _median(_cc(mixed)), "after_served_n": len(mixed),
            "cache_creation_median_clean_history": _median(_cc(clean)), "clean_n": len(clean),
        },
    }


def format_stats(s: dict) -> str:
    rs, rsx, fb, lat, an = (s["routed_share"], s["routed_share_excl_repeats"], s["fallbacks"],
                            s["latency"], s["anthropic"])
    sess = s["sessions"]
    share = f"{rs['share'] * 100:.1f}%" if rs["share"] is not None else "n/a"
    share_x = f"{rsx['share'] * 100:.1f}%" if rsx["share"] is not None else "n/a"
    lines = [
        f"calls through proxy: {s['calls']}  across {sess['n']} session(s) "
        f"(median {sess['median_calls_per_session']}/session, max {sess['max_calls_per_session']})",
        f"routed share: {share}  ({rs['served']} served by non-Claude / {rs['calls']} calls)",
        f"routed share excl. loop-guard repeats: {share_x}  "
        f"({rsx['served']} served / {rsx['calls']} calls, {rsx['repeats_excluded']} repeat(s) excluded)",
        f"fallbacks: {fb['n']} of {fb['attempted']} attempts; not served by reason: {fb['not_served_by_reason']}",
        f"latency: served median {lat['served_median_s']}s (n={lat['served_n']}), "
        f"Anthropic median {lat['anthropic_median_s']}s (n={lat['anthropic_n']}), "
        f"added total {lat['added_total_s']}s, thinking retries {lat['thinking_retries']}",
        f"Anthropic tokens: {an['tokens']}",
        f"Anthropic est. cost: ${an['est_cost_usd']} over {an['calls']} calls "
        f"({an['unpriced_calls']} unpriced); est. avoided: ${an['est_avoided_usd']} (n={an['est_avoided_n']})",
        f"cache_creation median: after a served turn {an['cache_creation_median_after_served_turn']} "
        f"(n={an['after_served_n']}) vs clean history {an['cache_creation_median_clean_history']} (n={an['clean_n']})",
    ]
    return "\n".join(lines)
