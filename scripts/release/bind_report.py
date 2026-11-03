#!/usr/bin/env python3
"""The BIND success bar (owner decision D-358; BIND-PLAN-20261103 section 3), measured.

    python3 scripts/release/bind_report.py --since 2026-11-04T12:00:00Z
    python3 scripts/release/bind_report.py --since 2026-11-04T12:00:00Z --until 2026-11-09T00:00:00Z --json

One block per criterion, each with its n, the UTC window, the source and a
status. Statuses are PASS, FAIL, INSUFFICIENT (the window has not yet produced the
n the bar needs; a pass on 4 units is not a pass), PENDING (the bar asks for a
hand check that has not been entered) and REPORTED (shown, never a verdict). The
exit code is 0 only when every gated line is PASS.

    1  Codex binding rate >= 90% on >= 30 delegated units, over >= 3 days: the Agent
       tool_result carries the Codex text AND no Claude sub-agent was launched for
       that tool_use_id.
    2  Blocked-answer share among BOUND units < 10%, by a hand check of 20 bound
       units. The automated screen is printed beside it and never replaces it:
       ``--hand-checked FILE`` takes {"unit_id": ..., "blocked": true|false} rows for
       the ids this report prints.
    3  Toll probes: router holds ("Routing directive BLOCKED") per 100 organic prompts
       < 1, and 0 fixed-reply probes ("Reply OK"). Holds and prompts come from BIND
       W2's hold ledger (``<state dir>/hold_events.jsonl``) when it has rows in the
       window, otherwise from the transcripts; probes are counted from the transcripts.
    4  The outcome rate under the FIXED audit over the 30 days ending at --until:
       REPORTED, not gated (owner decision D-359). The outcome gate was rebaselined at
       the audit v2 number of 16.0.0 and is not compared with 15.4.0's 0.229% (audit
       v1, a different method, transcripts purged); the binding rate carries the bar.
       The line prints the v2 rate and, when the gate's history holds one, the
       recorded v2 baseline.
    -  Reported, not gated: Claude output tokens and dollars avoided, as an ESTIMATE.

The audit's rules are in ``outcome_audit.py``; this script only slices its units.
Read-only: it prints, it writes no file, and it never writes into ~/.llm-router or
~/.claude. Like the audit it imports nothing from ``src/``.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY = ROOT / "docs" / "measurements" / "outcome_audit_history.json"

# D-358 thresholds (pre-registered 2026-11-03)
BIND_RATE_MIN = 0.90
BIND_MIN_UNITS = 30
BIND_MIN_DAYS = 3.0
BLOCKED_SHARE_MAX = 0.10
HAND_CHECK_N = 20
HOLDS_PER_100_MAX = 1.0
#: This script's floor, not the pre-registration's (which has none): "0 holds in 5 prompts" is not a pass.
#: Organic traffic is about 14 prompts a day (418 in the 16.0.0 window with the overrides applied), so a
#: 3 to 5 day window holds 40 to 70; ``--min-prompts`` moves it.
MIN_PROMPTS = 50
MIN_REFERENCE_UNITS = 30   # a live reference for the savings estimate needs this many void units
GATE_DAYS = 30

#: Output price per million tokens, copied from src/llm_router/pricing.py (the pricing
#: page, re-checked 2026-09-28). tests/test_bind_report.py compares this table with that
#: file so they cannot drift apart.
OUTPUT_PRICE_PER_MTOK = {
    "claude-opus-5-5": 20.0, "claude-opus-5": 25.0, "claude-sonnet-5-5": 10.0, "claude-sonnet-5": 10.0,
    "claude-haiku-5-5": 0.50, "claude-fable-5-1": 50.0, "claude-fable-5": 50.0,
}
#: What a void Claude sub-agent cost, measured on the 16.0.0 window: the 317 delegated
#: units whose Claude sub-agent ran anyway (north_star_units.jsonl joined to the
#: sub-agent transcripts' usage fields, max output_tokens per message id).
#: ``--reference live`` recomputes it from the transcripts while they exist (30-day retention).
MEASURED_REFERENCE = {
    "source": "317 void units, 2026-10-04T09:50:20Z..2026-11-03T09:50:20Z, north_star_units.jsonl + "
              "sub-agent transcripts (scratch measurement 2026-11-03)",
    "n": 317, "output_tokens_total": 3_795_414, "output_tokens_mean": 11_973, "output_tokens_median": 2_829,
    "by_model": {"claude-opus-5-5": {"n": 265, "output_tokens": 3_638_817},
                 "claude-sonnet-5-5": {"n": 47, "output_tokens": 116_602},
                 "claude-sonnet-5": {"n": 2, "output_tokens": 1_916},
                 "<synthetic>": {"n": 3, "output_tokens": 38_079}},
}


def _audit_modules():
    """The gate (which path-loads the audit) and that same audit module."""
    name = "_br_outcome_gate"
    if name in sys.modules:
        gate = sys.modules[name]
    else:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("outcome_gate.py"))
        gate = importlib.util.module_from_spec(spec)
        sys.modules[name] = gate
        spec.loader.exec_module(gate)  # type: ignore[union-attr]
    return gate, gate._A


def parse_utc(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pct(x: float | None) -> str:
    # 3 decimals under 1% so a small rate is not blurred ("0.2%" for 0.229%)
    return "n/a" if x is None else (f"{100 * x:.3f}%" if x < 0.01 else f"{100 * x:.1f}%")


def line(status: str, name: str, result: str, target: str, n: str, window: str, source: str, note: str = "") -> dict:
    return {"status": status, "criterion": name, "result": result, "target": target, "n": n,
            "window": window, "source": source, "note": note}


def hand_check_sample(unit_ids: list[str], k: int = HAND_CHECK_N) -> list[str]:
    """A fixed sample of up to k bound units for the hand check: ordered by a hash of the
    id, so it is stable, not chosen by anyone, and the same on every run."""
    return sorted(unit_ids, key=lambda u: hashlib.sha256(u.encode()).hexdigest())[:k]


def read_hand_checked(path: Path | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    if path is None:
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("unit_id"), str) and isinstance(r.get("blocked"), bool):
            out[r["unit_id"]] = r["blocked"]
    return out


HOLD_LEDGER = "hold_events.jsonl"


def read_hold_events(path: Path | None, since: float, until: float, sessions: set[str]) -> dict | None:
    """Counts from BIND W2's hold ledger (``hold_events.jsonl``: one text-free row per
    prompt, hold, blocked tool call, probe and release, each with a ``ts``, an ``event`` and
    a ``session_id``) for the organic sessions in the window. None when the file is not there
    or holds no prompt row in the window: an empty ledger is unknown, never zero."""
    if path is None or not path.is_file():
        return None
    counts: dict[str, int] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if (isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) and isinstance(r.get("event"), str)
                and since <= r["ts"] <= until and r.get("session_id") in sessions):
            counts[r["event"]] = counts.get(r["event"], 0) + 1
    return counts if counts.get("prompt") else None


def void_output_tokens(projects_dir: Path, session_id: str, agent_id: str) -> tuple[int, str | None] | None:
    """Output tokens (max per message id) and model of one launched Claude sub-agent."""
    files = glob.glob(str(projects_dir / "*" / session_id / "subagents" / f"agent-{agent_id}.jsonl"))
    if not files:
        return None
    per_msg: dict[str, int] = {}
    model = None
    with open(files[0], encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if '"usage"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if rec.get("type") != "assistant" or not isinstance(msg, dict):
                continue
            usage = msg.get("usage") or {}
            mid = msg.get("id") or raw[:40]
            per_msg[mid] = max(per_msg.get(mid, 0), int(usage.get("output_tokens") or 0))
            model = msg.get("model") or model
    return sum(per_msg.values()), model


def live_reference(units, since: float, projects_dir: Path) -> dict | None:
    """What a void Claude sub-agent cost in the transcripts before `since`."""
    toks: list[int] = []
    by_model: dict[str, dict] = {}
    for u in units:
        e = u.extra
        if u.kind != "agent_route_codex" or e.get("outcome") != "delegated" or not e.get("launched") or u.ts >= since:
            continue
        got = void_output_tokens(projects_dir, u.session_id, e["agent_id"]) if e.get("agent_id") else None
        if got is None:
            continue
        toks.append(got[0])
        m = by_model.setdefault((got[1] or "unknown").split("[")[0], {"n": 0, "output_tokens": 0})
        m["n"] += 1
        m["output_tokens"] += got[0]
    if not toks:
        return None
    toks.sort()
    return {"source": f"{len(toks)} void units before {iso(since)}, sub-agent transcripts (live)",
            "n": len(toks), "output_tokens_total": sum(toks), "output_tokens_mean": sum(toks) / len(toks),
            "output_tokens_median": toks[len(toks) // 2], "by_model": by_model}


def savings_block(n_bound: int, window_days: float, ref: dict, ref_kind: str) -> dict:
    """Output tokens and dollars a bound unit AVOIDS, as an estimate: the mean cost of a void
    Claude sub-agent from the reference, times the bound units. Output tokens only, so the
    dollars are a lower bound; gross, not net of what the parent then does with the result."""
    cost_per_unit, unpriced = 0.0, 0
    for model, m in ref["by_model"].items():
        price = OUTPUT_PRICE_PER_MTOK.get(model)
        if price is None:
            unpriced += m["n"]
            continue
        cost_per_unit += m["output_tokens"] * price / 1e6
    cost_per_unit /= max(ref["n"], 1)
    weeks = max(window_days, 1e-9) / 7
    return {"label": "ESTIMATE (not measured): gross output tokens and dollars of the Claude sub-agents the bound "
                     "units did not need; output tokens only, so dollars are a lower bound",
            "reference": ref_kind, "reference_source": ref["source"], "reference_n": ref["n"],
            "unpriced_reference_units": unpriced,
            "output_tokens_per_bound_unit_mean": round(ref["output_tokens_mean"]),
            "output_tokens_per_bound_unit_median": ref["output_tokens_median"],
            "bound_units": n_bound,
            "output_tokens_avoided": round(n_bound * ref["output_tokens_mean"]),
            "output_tokens_avoided_per_week": round(n_bound * ref["output_tokens_mean"] / weeks),
            "usd_avoided": round(n_bound * cost_per_unit, 2),
            "usd_avoided_per_week": round(n_bound * cost_per_unit / weeks, 2)}


def report(units, ctx: dict, gate, audit, since: float, until: float, args, projects_dir: Path,
           state_dir: Path, history: dict | None) -> dict:
    window = f"{iso(since)}..{iso(until)}"
    days = (until - since) / 86400
    ledger = str(state_dir / "north_star_units.jsonl")
    src_tr = f"{projects_dir} transcripts ({ctx['n_files_read']} files, {ctx['n_sessions']} organic sessions; " \
             f"{len(ctx['overrides']['excluded_sessions'])} sessions excluded by session_kind_overrides.json)"
    lines: list[dict] = []

    # 1. binding
    d = [u for u in units if u.kind == "agent_route_codex" and u.extra.get("outcome") == "delegated"
         and since <= u.ts <= until]
    bound = [u for u in d if u.extra.get("bound")]
    ci = audit.wilson(len(bound), len(d))
    rate = len(bound) / len(d) if d else None
    res = f"{len(bound)}/{len(d)} = {pct(rate)}" + (f" [Wilson 95% {pct(ci[0])}..{pct(ci[1])}]" if ci else "")
    void, unpaired = (sum(bool(u.extra.get("launched")) for u in d), sum(not u.extra.get("call_found") for u in d))
    if len(d) < BIND_MIN_UNITS or days < BIND_MIN_DAYS:
        status = "INSUFFICIENT"
        why = f"needs >= {BIND_MIN_UNITS} delegated units over >= {BIND_MIN_DAYS:g} days; has {len(d)} over {days:.1f}"
    else:
        status, why = ("PASS" if rate >= BIND_RATE_MIN else "FAIL"), ""
    lines.append(line(status, "Codex binding rate", res, f">= {BIND_RATE_MIN:.0%} on >= {BIND_MIN_UNITS} units, >= {BIND_MIN_DAYS:g} days",
                      f"{len(d)} delegated units ({void} void: a Claude sub-agent ran; {unpaired} with no Agent call found)",
                      window, f"{ledger} joined to {src_tr}",
                      (why + "; " if why else "") + f"respawned after a bound result: "
                      f"{sum(bool(u.extra.get('respawned')) for u in d)}; blocked by the gate before binding (codex_blocked): "
                      f"{sum(u.kind == 'agent_route_codex' and u.extra.get('outcome') == 'codex_blocked' and since <= u.ts <= until for u in units)}"))

    # 2. blocked-answer share among bound units
    screen = sum(bool(u.extra.get("blocked_screen")) for u in bound)
    sample = hand_check_sample([u.unit_id for u in bound])
    hand = {k: v for k, v in read_hand_checked(args.hand_checked).items() if k in {u.unit_id for u in bound}}
    if len(hand) >= HAND_CHECK_N or (bound and len(hand) >= min(HAND_CHECK_N, len(bound))):
        share = sum(hand.values()) / len(hand)
        status = "PASS" if share < BLOCKED_SHARE_MAX else "FAIL"
        res = f"{sum(hand.values())}/{len(hand)} = {pct(share)} by hand check"
    elif not bound:
        status, res = "INSUFFICIENT", "no bound units yet"
    else:
        status, res = "PENDING", f"hand check of {HAND_CHECK_N} not entered ({len(hand)} given)"
    lines.append(line(status, "Blocked-answer share among bound units", res, f"< {BLOCKED_SHARE_MAX:.0%} (hand check of {HAND_CHECK_N})",
                      f"{len(bound)} bound units", window, f"the bound units' delivered text in {src_tr}",
                      f"automated screen (read-only/sandbox limit AND 'could not'): {screen}/{len(bound)}"
                      + (f" = {pct(screen / len(bound))}" if bound else "")
                      + "; the screen only picks candidates. Hand-check sample (unit ids, stable order): "
                      + (", ".join(sample) if sample else "none")))

    # 3. toll probes
    sessions = ctx["sessions"]
    prompts_t = sum(1 for s in sessions.values() for i in s.prompt_idx if since <= (audit._ts(s.records[i]) or 0) <= until)
    derived = sum(1 for s in sessions.values() for k, _t in s.hold_idx if since <= (audit._ts(s.records[k]) or 0) <= until)
    hold_rows = read_hold_events(args.holds_file or state_dir / HOLD_LEDGER, since, until, set(sessions))
    if hold_rows is not None:
        holds, prompts, how = hold_rows.get("blocked", 0), hold_rows["prompt"], \
            f"the router's hold ledger {args.holds_file or state_dir / HOLD_LEDGER} (cross-check from the transcripts: {derived} holds in {prompts_t} prompts)"
    else:
        holds, prompts, how = derived, prompts_t, "holds derived from the transcripts (no hold ledger rows in the window)"
    probes = [u for u in units if u.kind == "routed_mcp" and u.extra.get("probe") and since <= u.ts <= until]
    per100 = 100 * holds / prompts if prompts else None
    hci = audit.wilson(holds, prompts)
    if prompts < args.min_prompts:
        status = "INSUFFICIENT"
    else:
        status = "PASS" if (per100 < HOLDS_PER_100_MAX and not probes) else "FAIL"
    lines.append(line(status, "Toll probes", f"{holds} holds in {prompts} prompts = "
                      f"{'n/a' if per100 is None else f'{per100:.2f}'} per 100"
                      + (f" [Wilson 95% {100 * hci[0]:.2f}..{100 * hci[1]:.2f}]" if hci else "")
                      + f"; {len(probes)} fixed-reply probes",
                      f"holds < {HOLDS_PER_100_MAX:g} per 100 prompts and 0 'Reply OK' probes",
                      f"{prompts} organic prompts (floor {args.min_prompts}), {len(probes)} probes", window,
                      f"{how}; probes from the transcripts' MCP calls in {src_tr}"
                      + (f"; probe units (first 5, to check for a false positive): "
                         f"{', '.join(u.unit_id for u in probes[:5])}" if probes else ""),
                      "" if prompts >= args.min_prompts else
                      f"needs >= {args.min_prompts} organic prompts before a rate under 1 per 100 means anything"))

    # 4. the outcome rate under the fixed audit: REPORTED, not gated (owner decision D-359)
    rows, summary = audit._finish(list(units), ctx, "bind-report")
    leaks = gate.check_leaks(rows, summary)
    h = summary["headline"]
    cur_v = audit.AUDIT_VERSION
    releases = (history or {}).get("releases") or []
    base = next((e for e in reversed(releases) if gate.audit_version(e) == cur_v), None)
    oci = h["ci95"]
    res = f"{h['used']}/{h['attempted']} = {pct(h['rate'])}" + (f" [Wilson 95% {pct(oci[0])}..{pct(oci[1])}]" if oci else "")
    if h["attempted"] < audit.MIN_N:
        res += f" (too few to tell, n < {audit.MIN_N})"
    note = ("D-359: the outcome gate was rebaselined at the audit v2 number, so this line is reported and the binding "
            "rate (line 1) carries the bar. It is not compared with 15.4.0 (audit v1, transcripts purged). ")
    note += (f"Recorded v{cur_v} baseline: {base['release']} {base['used']}/{base['attempted']} = {pct(base['used_rate'])} "
             f"(same audit version, like for like). " if base else
             f"No v{cur_v} baseline is recorded yet: the gate's first run records it "
             f"(pre-release-verify.sh --outcome-cross-version rebaseline). ")
    note += f"With the not_applicable units counted as attempted the rate is {pct(summary['not_applicable']['used_rate_if_included'])}."
    if leaks:
        note += f" WARNING: the gate's integrity checks flag {len(leaks)} problem(s), first: {leaks[0]}"
    lines.append(line("REPORTED", "Outcome rate under the fixed audit (reported, not gated; D-359)", res,
                      "none: reported only (D-359)",
                      f"{h['attempted']} attempted units ({h['not_applicable']} not_applicable outside the denominator)",
                      f"{iso(ctx['lo'])}..{iso(ctx['hi'])} (30-day audit ending at --until)", f"{src_tr}; audit v{cur_v}", note))

    # reported, not gated: savings
    ref_kind, ref = "measured 2026-11-03, all sessions", MEASURED_REFERENCE
    if args.reference == "live":
        live = live_reference(units, since, projects_dir)
        if live is not None and live["n"] >= MIN_REFERENCE_UNITS:
            ref_kind, ref = "live, organic sessions only", live
    sav = savings_block(len(bound), days, ref, ref_kind)
    if ref is not MEASURED_REFERENCE:  # the two populations differ by a factor of several: show both
        sav["alternative"] = savings_block(len(bound), days, MEASURED_REFERENCE, "measured 2026-11-03, all sessions")

    verdict = "MET" if all(x["status"] == "PASS" for x in lines if x["status"] != "REPORTED") else "NOT MET"
    return {"decision": "D-358", "audit_version": cur_v, "window": window, "window_days": round(days, 2),
            "generated_at": iso(time.time()), "verdict": verdict, "lines": lines, "savings_estimate": sav,
            "bound_unit_ids": [u.unit_id for u in bound]}


def render(rep: dict) -> str:
    out = [f"D-358 success bar (BIND-PLAN-20261103 section 3) - audit v{rep['audit_version']}",
           f"window {rep['window']} UTC ({rep['window_days']} days); generated {rep['generated_at']}", ""]
    for i, x in enumerate(rep["lines"], 1):
        out += [f"{i}. [{x['status']}] {x['criterion']}: {x['result']}",
                f"     target: {x['target']}",
                f"     n: {x['n']}",
                f"     window: {x['window']}",
                f"     source: {x['source']}"]
        if x["note"]:
            out.append(f"     note: {x['note']}")
    s = rep["savings_estimate"]
    out += ["", f"Reported, not gated - {s['label']}:",
            f"     {s['bound_units']} bound units x {s['output_tokens_per_bound_unit_mean']} output tokens (mean; median "
            f"{s['output_tokens_per_bound_unit_median']}) = {s['output_tokens_avoided']} tokens "
            f"({s['output_tokens_avoided_per_week']}/week), ${s['usd_avoided']} (${s['usd_avoided_per_week']}/week)",
            f"     reference ({s['reference']}): {s['reference_source']}"]
    if "alternative" in s:
        a = s["alternative"]
        out.append(f"     with the 16.0.0 all-sessions reference ({a['reference_source']}): "
                   f"{a['output_tokens_per_bound_unit_mean']} tokens/unit mean -> {a['output_tokens_avoided']} tokens, ${a['usd_avoided']}")
    out += ["", f"D-358 bar: {rep['verdict']}"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, metavar="ISO_UTC", help="when the fix went live")
    ap.add_argument("--until", default=None, metavar="ISO_UTC", help="end of the window (default: now)")
    ap.add_argument("--projects-dir", type=Path, default=None)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY,
                    help="the outcome gate's history; line 4 quotes the recorded baseline of this audit version")
    ap.add_argument("--holds-file", type=Path, default=None,
                    help=f"the router's hold ledger (default: <state dir>/{HOLD_LEDGER}); transcripts are used when it has no rows")
    ap.add_argument("--hand-checked", type=Path, default=None, help="jsonl of {unit_id, blocked} for the printed sample")
    ap.add_argument("--min-prompts", type=int, default=MIN_PROMPTS,
                    help="organic prompts the toll line needs before it can pass (this script's floor, not D-358's)")
    ap.add_argument("--reference", choices=("measured", "live"), default="live",
                    help="what a void Claude sub-agent cost: live (recompute while transcripts exist) or measured")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    gate, audit = _audit_modules()
    since = parse_utc(args.since)
    until = parse_utc(args.until) if args.until else time.time()
    if until <= since:
        print("--until must be after --since", file=sys.stderr)
        return 2
    projects_dir = args.projects_dir or audit.default_projects_dir()
    state_dir = args.state_dir or audit.default_state_dir()
    lo = min(since, until - GATE_DAYS * 86400)
    units, ctx = audit._collect(GATE_DAYS, projects_dir, state_dir, until, lo=lo)
    history = json.loads(args.history.read_text(encoding="utf-8")) if args.history.is_file() else None
    rep = report(units, ctx, gate, audit, since, until, args, projects_dir, state_dir, history)
    print(json.dumps(rep, indent=2) if args.json else render(rep))
    return 0 if rep["verdict"] == "MET" else 1


if __name__ == "__main__":
    sys.exit(main())
