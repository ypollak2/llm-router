"""Reports over the decision ledger (PLAN v16 P1.7 tasks 3-5): the daily accuracy line, PSI
drift and the ``kpi`` classifier line. Pure over rows; the CLI reads the state dir.

``python -m llm_router.decision_report --day YYYY-MM-DD`` (UTC) appends the day's accuracy and
drift record to ``decision_daily.jsonl`` once (a re-run for the same day adds nothing). The
SessionEnd background child runs it for yesterday.

Statistics (PLAN section 1.4): every rate prints its n and a Wilson 95% CI; a set below
:data:`MIN_N` is "not informative" and never a pass; a missing day is a line with n = 0, not
an omitted day. Rows are not session-kind filtered (they carry no kind), so every number
here is on the "all sessions" population, not organic-only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llm_router import decision_ledger as DL
from llm_router import outcome_link, paths

DAILY_FILENAME = "decision_daily.jsonl"
PSI_WARN = 0.2
MIN_N = 30          # smallest window a PSI is computed on
MIN_LABELLED = 30   # smallest labelled sample a day's accuracy is called informative
TIER_RANK = {"haiku": 1, "sonnet": 2, "opus": 3}
_CONF_BINS = 10
_NONE_BIN = _CONF_BINS  # abstain / no confidence


def daily_path() -> Path:
    return paths.state_path(DAILY_FILENAME)


def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


# ── drift ────────────────────────────────────────────────────────────────────

def psi(expected: list[float], actual: list[float], eps: float = 1e-4) -> float:
    """Population stability index over matching bins of COUNTS. Each bin share is floored at
    ``eps`` so an empty bin gives a large finite term, not a division by zero."""
    if len(expected) != len(actual) or not expected:
        raise ValueError("psi needs two equal, non-empty bin lists")
    se, sa = sum(expected), sum(actual)
    if se <= 0 or sa <= 0:
        raise ValueError("psi of an empty distribution")
    total = 0.0
    for e, a in zip(expected, actual):
        pe, pa = max(e / se, eps), max(a / sa, eps)
        total += (pa - pe) * math.log(pa / pe)
    return total


def tier_bins(rows: list[dict]) -> list[int]:
    c = Counter(r.get("tier") for r in rows)
    return [c.get(t, 0) for t in DL.TIERS]


def conf_bins(rows: list[dict]) -> list[int]:
    """10 equal-width confidence bins plus one for no confidence (abstain with ``None``)."""
    out = [0] * (_CONF_BINS + 1)
    for r in rows:
        c = r.get("confidence")
        if isinstance(c, (int, float)) and not isinstance(c, bool) and 0.0 <= c <= 1.0:
            out[min(_CONF_BINS - 1, int(c * _CONF_BINS))] += 1
        else:
            out[_NONE_BIN] += 1
    return out


def _day_bounds(day: str) -> tuple[float, float]:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def _in(rows: list[dict], lo: float, hi: float) -> list[dict]:
    return [r for r in rows if isinstance(r.get("ts"), (int, float)) and lo <= r["ts"] < hi]


def drift(decisions: list[dict], day: str) -> dict:
    """PSI of the tier distribution and the confidence histogram for ``day`` against the
    trailing 7 days before it. Either window under :data:`MIN_N` rows is ``informative: false``
    (no PSI, no warning). ``warn`` is true when a computed PSI exceeds :data:`PSI_WARN`."""
    lo, hi = _day_bounds(day)
    cur, ref = _in(decisions, lo, hi), _in(decisions, lo - 7 * 86400, lo)
    out = {"day": day, "n_day": len(cur), "n_ref": len(ref), "informative": False,
           "psi_tier": None, "psi_conf": None, "warn": False}
    if len(cur) < MIN_N or len(ref) < MIN_N:
        return out
    out["psi_tier"] = round(psi(tier_bins(ref), tier_bins(cur)), 4)
    out["psi_conf"] = round(psi(conf_bins(ref), conf_bins(cur)), 4)
    out["informative"] = True
    out["warn"] = max(out["psi_tier"], out["psi_conf"]) > PSI_WARN
    return out


# ── daily accuracy ───────────────────────────────────────────────────────────

def daily_accuracy(decisions: list[dict], labels: list[dict], day: str) -> dict:
    """Shadow agreement for ``day``: sampled decisions of that day that have a reference
    label. ``agree`` = engine tier equals the reference; ``under`` = engine tier is cheaper
    than the reference (the estimated under-route). The shadow rate is sampled / decided."""
    lo, hi = _day_bounds(day)
    cur = _in(decisions, lo, hi)
    sampled = [r for r in cur if r.get("shadow") is True]
    ref = {lb.get("decision_id"): lb for lb in labels if lb.get("ref_tier") in TIER_RANK}
    lab = [(r, ref[r["decision_id"]]) for r in sampled if r.get("decision_id") in ref]
    n = len(lab)
    agree = sum(1 for r, lb in lab if r.get("tier") == lb["ref_tier"])
    under = sum(1 for r, lb in lab if r.get("tier") in TIER_RANK
                and TIER_RANK[r["tier"]] < TIER_RANK[lb["ref_tier"]])
    src = Counter(lb["source"].split(":", 1)[0] for _, lb in lab)
    return {"day": day, "n_decisions": len(cur), "n_sampled": len(sampled),
            "shadow_rate": (len(sampled) / len(cur)) if cur else None, "n_labelled": n,
            "agree": agree, "agree_wilson95": wilson(agree, n), "under": under,
            "under_wilson95": wilson(under, n), "sources": dict(src),
            "informative": n >= MIN_LABELLED}


def accuracy_text(a: dict) -> str:
    if not a["n_labelled"]:
        return ("%s: shadow agreement n=0 labelled (%d decisions, %d sampled): not informative"
                % (a["day"], a["n_decisions"], a["n_sampled"]))
    note = "" if a["informative"] else " (not informative: n < %d)" % MIN_LABELLED
    return ("%s: shadow agree %d/%d [%.2f-%.2f], est. under-route %d/%d [%.2f-%.2f], sampled %d of %d%s"
            % (a["day"], a["agree"], a["n_labelled"], *a["agree_wilson95"], a["under"], a["n_labelled"],
               *a["under_wilson95"], a["n_sampled"], a["n_decisions"], note))


def write_daily(day: str, *, state: Path | None = None, now: float | None = None) -> dict | None:
    """Append the day's record once. Returns the record, or None when the day is already there."""
    base = state or paths.llm_router_home()
    path = base / DAILY_FILENAME
    if any(r.get("day") == day for r in DL.read_jsonl(path)):
        return None
    dec = DL.read_jsonl(base / DL.LEDGER_FILENAME)
    rec = {"day": day, "written_at": round(time.time() if now is None else now, 3),
           "accuracy": daily_accuracy(dec, DL.read_jsonl(base / DL.LABELS_FILENAME), day),
           "drift": drift(dec, day)}
    rec["line"] = accuracy_text(rec["accuracy"])
    DL._append(path, rec)
    return rec


def days_covered(daily: list[dict], days: list[str]) -> dict:
    """P1.7-a "daily accuracy line on 7/7 days": a day counts only when its record exists AND
    was informative (n_labelled >= MIN_LABELLED). Both numbers are returned."""
    by = {r.get("day"): r for r in daily}
    have = [d for d in days if d in by]
    good = [d for d in have if (by[d].get("accuracy") or {}).get("informative")]
    return {"window_days": len(days), "with_record": len(have), "informative": len(good)}


# ── kpi line ─────────────────────────────────────────────────────────────────

def classifier_line(decisions: list[dict], labels: list[dict], outcomes: list[dict], *, now: float,
                    engine_version: str, heldout: dict | None = None) -> str | None:
    """``kpi classifier``: stored GT held-out for ``engine_version`` + live 7-day abstain rate,
    layer mix, shadow agreement, outcome-linked share, with n. None when the ledger is empty."""
    win = [r for r in decisions if isinstance(r.get("ts"), (int, float)) and now - 7 * 86400 <= r["ts"] <= now + 1.0]  # ts is rounded to ms: a row just written may read as 'future'
    if not win:
        return None
    n = len(win)
    ab = sum(1 for r in win if r.get("abstain") is True)
    mix = Counter(r.get("layer") for r in win)
    mix_s = " ".join("%s %.0f%%" % (k, 100 * v / n) for k, v in sorted(mix.items(), key=lambda kv: -kv[1]))
    ref = {lb.get("decision_id"): lb for lb in labels if lb.get("ref_tier") in TIER_RANK}
    lab = [(r, ref[r["decision_id"]]) for r in win if r.get("shadow") is True and r.get("decision_id") in ref]
    agree = sum(1 for r, lb in lab if r.get("tier") == lb["ref_tier"])
    ol = outcome_link.linked_share(win, outcomes, now=now)
    if heldout and heldout.get("engine_version") == engine_version:
        held = "GT held-out %s (n=%s, stored)" % (heldout.get("summary", "?"), heldout.get("n", "?"))
    else:
        held = "GT held-out: none stored for %s" % engine_version
    share = "n/a (0 due)" if ol["share"] is None else "%.0f%% of %d due" % (100 * ol["share"], ol["n_due"])
    return ("classifier [%s]: %s; live 7d n=%d: abstain %.1f%%, layers %s, shadow agree %s, "
            "outcome-linked %s (all sessions, not organic-only; informational)"
            % (engine_version, held, n, 100 * ab / n, mix_s,
               "%d/%d" % (agree, len(lab)) if lab else "n/a (0 labelled)", share))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llm_router.decision_report")
    ap.add_argument("--day", help="UTC day YYYY-MM-DD (default: yesterday)")
    a = ap.parse_args(argv)
    day = a.day or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    rec = write_daily(day)
    print(json.dumps(rec if rec else {"day": day, "status": "already written"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
