#!/usr/bin/env python3
"""CLS-v16 B1: the scoring harness for learned tier classifiers (counts-only, stdlib only).

Implements docs/prereg/PREREG-CLS-DEV.md sections 3, 4, 7 and 8 (and the TEST scoring that the later
PREREG-CLS-TEST reuses) over the cost functions of ``src/llm_router/shadow_eval.py``. TEST-gated: it scores
nothing until PREREG-CLS-TEST is frozen; DEV output is guard-free and screening only.

    python3 scripts/cls_eval.py score    TABLE [--truth RULE] [--truth-by-source SRC=RULE,...] [--population pooled|real|syn]
                                         [--mode dev|test] [--calibration FILE] [--use-log FILE --single-use]
                                         [--pooled-oos --arm NAME] [--split-rows real,syn] [--out FILE]
    python3 scripts/cls_eval.py fit      TABLE --arm NAME [--out FILE]
    python3 scripts/cls_eval.py selftest TABLE [--make-noised-oracle SEED --out FILE]

TABLE is a JSONL "arms table", one record per item: ``item_id, group, requested, strata, truths, preds, scores?,
kind?, fold?, weight?`` (an optional first line ``{"_header": {...}}``). It never holds prompt text; a record with a
key named text, prompt, digest, reason or content is refused (exit 2, the message names the key, never the value).

Statistics (D-148): the non-inferiority bound is the 97.5th percentile of the paired bootstrap (the upper end of the
reported two-sided 95% CI); the NOT-TRIVIAL guard is the one-sided 95th percentile. ``--mode test`` (PASS-* labels)
needs ``--single-use --use-log``; DEV mode prints SCREEN-PASS / SCREEN-REJECT only.

Exit: 0 done, 2 refused input or usage, 3 shadow_eval sha pin mismatch, 4 single-use log already holds a
"started" line, 5 fit found no admissible operating point (selftest: 1 when a validity check fails).
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import json
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from llm_router import shadow_eval as _se
except ImportError:  # run from a checkout without an install
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from llm_router import shadow_eval as _se
from llm_router.shadow_eval import COST, LAMBDA, RANK, clamp, cost_eff, tier_name, wilson

TIERS = ("haiku", "sonnet", "opus")
JUDGES = ("opus", "gpt", "sonnet")
FORBIDDEN_KEYS = ("text", "prompt", "digest", "reason", "content")
SEED = 20261017
B_DEFAULT = 2000
NI_UNDER = 0.03          # non-inferiority: upper 95% bound of (cand - rules_eff) under-route, share
NI_COST = 0.3            # ... and of C-lambda2-clamp
COST_HIGH_RATIO = 1.35  # PREREG-CLS-DEV criterion 5: informative flag, not gating
GUARD_EPS = 1e-9         # the guard bound must be below -eps: always_opus gives exactly 0 up to float noise
EQ_EPS = 1e-12
N_OPUS_MIN = 40
N_OPUS_LOWPOWER = 80
MISSING_TRUTH_MAX = 0.05
ERR_KILL = 0.05
ERR_INFORMATIVE = 0.10
HAIKU_MIN_ROUTES = 10
HAIKU_MIN_PRECISION = 0.70
ADMISSIBLE_PCT = 0.20
GRID_HI = [round(0.20 + 0.05 * i, 2) for i in range(16)]
GRID_LO = [round(0.50 + 0.05 * i, 2) for i in range(10)] + [0.99, 1.01]
GRID_LO = sorted(set(GRID_LO))
SENSITIVITY = ("opus_only", "gpt_only", "agree_only", "gpt_excluded", "higher2")
ROBUST_RULES = ("opus_only", "gpt_only", "agree_only")
VALIDITY_ARMS = ("always_haiku", "always_sonnet", "always_opus", "random", "nimble", "qwen3coder",
                 "noised_oracle", "oracle")
KNOWN_WORSE = ("always_haiku", "random", "nimble")
STRATA_DIMS = ("length_band", "truth", "task_type", "tool_need", "generator", "contested", "source")
DEV_LABEL = "DEV figures are guard-free and screening only"
OPS = {"group_visits": 0, "bootstraps": 0}


class Refuse(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# --------------------------------------------------------------------------- hashes, input

def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _norm_tier(value, where: str):
    if value is None:
        return None
    t = tier_name(value) if isinstance(value, str) else None
    if t is None:
        raise Refuse(2, f"refusing: unknown tier value at {where}")
    return t


def _find_forbidden(obj, lineno: int) -> None:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS:
                    raise Refuse(2, f"refusing: record {lineno} holds the forbidden key '{k.lower()}'")
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


def _num(v, where: str, lo=None, hi=None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise Refuse(2, f"refusing: {where} is not a number")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise Refuse(2, f"refusing: {where} is out of range")
    return float(v)


def _parse_item(rec: dict, lineno: int) -> dict:
    iid = rec.get("item_id")
    if not isinstance(iid, str) or not iid:
        raise Refuse(2, f"refusing: record {lineno} has no item_id")
    grp = rec.get("group")
    if not isinstance(grp, str) or not grp:
        raise Refuse(2, f"refusing: record {lineno} has no group")
    kind = rec.get("kind", "real")
    if kind not in ("real", "syn"):
        raise Refuse(2, f"refusing: record {lineno} key kind must be real or syn")
    strata = rec.get("strata") or {}
    truths_raw = rec.get("truths") or {}
    preds_raw = rec.get("preds") or {}
    if not all(isinstance(x, dict) for x in (strata, truths_raw, preds_raw)):
        raise Refuse(2, f"refusing: record {lineno} strata, truths and preds must be objects")
    truths = {j: _norm_tier(truths_raw.get(j), f"record {lineno} truths.{j}") for j in JUDGES}
    for k in truths_raw:
        if k not in JUDGES:
            raise Refuse(2, f"refusing: record {lineno} truths has the unknown judge key '{k}'")
    preds = {str(a): _norm_tier(v, f"record {lineno} preds.{a}") for a, v in preds_raw.items()}
    scores: dict[str, tuple] = {}
    for a, s in (rec.get("scores") or {}).items():
        if not isinstance(s, dict):
            raise Refuse(2, f"refusing: record {lineno} scores.{a} must be an object")
        scores[str(a)] = tuple(_num(s.get(f"p_{t}"), f"record {lineno} scores.{a}.p_{t}", 0.0, 1.0)
                               for t in TIERS)
    fold = rec.get("fold")
    if fold is not None and (isinstance(fold, bool) or not isinstance(fold, int) or not 0 <= fold <= 4):
        raise Refuse(2, f"refusing: record {lineno} key fold must be an integer 0-4")
    weight = 1.0 if rec.get("weight") is None else _num(rec.get("weight"), f"record {lineno} weight", 1e-12)
    return {"id": iid, "group": grp, "kind": kind, "w": weight, "fold": fold,
            "requested": _norm_tier(rec.get("requested"), f"record {lineno} requested"),
            "strata": {str(k): v for k, v in strata.items()}, "truths": truths, "preds": preds,
            "scores": scores}


def load_table(path: str | Path):
    """(header, items, raw_records, table_sha). Refuses a forbidden key anywhere in a record."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        raise Refuse(2, "refusing: the table cannot be read") from None
    header: dict = {}
    items: list[dict] = []
    raws: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            raise Refuse(2, f"refusing: line {lineno} is not JSON") from None
        if not isinstance(rec, dict):
            raise Refuse(2, f"refusing: line {lineno} is not an object")
        _find_forbidden(rec, lineno)
        if "_header" in rec:
            if not isinstance(rec["_header"], dict):
                raise Refuse(2, "refusing: _header must be an object")
            header = rec["_header"]
            continue
        it = _parse_item(rec, lineno)
        if it["id"] in seen:
            raise Refuse(2, f"refusing: duplicate item_id at line {lineno}")
        seen.add(it["id"])
        items.append(it)
        raws.append(rec)
    if not items:
        raise Refuse(2, "refusing: the table holds no items")
    return header, items, raws, hashlib.sha256(data).hexdigest()


def arms_of(items: list[dict]) -> list[str]:
    names = {a for it in items for a in it["preds"]}
    names.add("rules_eff")
    if not any("rules_eff" in it["preds"] and it["preds"]["rules_eff"] for it in items):
        raise Refuse(2, "refusing: no item carries a rules_eff prediction")
    return ["rules_eff"] + sorted(names - {"rules_eff"})


# --------------------------------------------------------------------------- truth rules

def rule_majority3(j: dict):
    if any(j.get(k) is None for k in JUDGES):
        return None
    tier, n = Counter(j[k] for k in JUDGES).most_common(1)[0]
    return tier if n >= 2 else "sonnet"


def rule_higher2(j: dict):
    a, b = j.get("opus"), j.get("gpt")
    if a is None or b is None:
        return None
    return a if RANK[a] >= RANK[b] else b


def rule_other_two_higher(j: dict, voided: str):
    rest = [j.get(k) for k in JUDGES if k != voided]
    if len(rest) != 2 or any(v is None for v in rest):
        return None
    return rest[0] if RANK[rest[0]] >= RANK[rest[1]] else rest[1]


def rule_agree_only(j: dict):
    vals = [v for v in (j.get(k) for k in JUDGES) if v is not None]
    return vals[0] if len(vals) >= 2 and len(set(vals)) == 1 else None


def truth_for(item: dict, rule: str, voided: str | None = None):
    """The truth tier of an item under a rule, or None (the item is excluded and counted)."""
    j = item["truths"]
    if rule.startswith("other_two_higher:"):
        rule, voided = "other_two_higher", rule.split(":", 1)[1]
    if rule == "majority3":
        return rule_majority3(j)
    if rule in ("opus_only", "gpt_only", "sonnet_only"):
        return j.get(rule.split("_", 1)[0])
    if rule == "agree_only":
        return rule_agree_only(j)
    if rule == "higher2":
        return rule_higher2(j)
    if rule == "gpt_excluded":
        return rule_other_two_higher(j, "gpt")
    if rule == "other_two_higher":
        if voided not in JUDGES:
            raise Refuse(2, "refusing: other_two_higher needs --voided opus|gpt|sonnet")
        return rule_other_two_higher(j, voided)
    raise Refuse(2, f"refusing: unknown truth rule '{rule}'")


def rule_for_source(source, default: str, by_source: dict) -> str:
    return by_source.get(source, default)


def parse_by_source(spec: str | None) -> dict:
    out: dict[str, str] = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        src, sep, rule = part.partition("=")
        if not sep or not src or not rule:
            raise Refuse(2, "refusing: --truth-by-source wants SRC=RULE[,SRC=RULE...]")
        out[src.strip()] = rule.strip()
    return out


def source_of(item: dict) -> str:
    s = item["strata"].get("source")
    return s if isinstance(s, str) and s else "unknown"


# --------------------------------------------------------------------------- rows

def resolve_pred(pred, fallback, truth):
    """A refusal or error (None) is scored as the rules_eff tier; it is never correct by itself."""
    return fallback if pred is None else pred


def select_population(rows: list[dict], population: str) -> list[dict]:
    if population == "pooled":
        return list(rows)
    return [r for r in rows if r["kind"] == population]


def split_by_kind(rows: list[dict], kinds) -> dict:
    return {k: [r for r in rows if r["kind"] == k] for k in kinds}


def cluster_keys(rows: list[dict]) -> list[str]:
    return [r["group"] for r in rows]


def train_rows(rows: list[dict], fold) -> list[dict]:
    return [r for r in rows if r["fold"] != fold]


def _imputed_requested(items: list[dict]) -> dict:
    """shadow_eval's imputation: the group's most common requested tier, else sonnet."""
    by: dict[str, Counter] = defaultdict(Counter)
    for it in items:
        if it["requested"]:
            by[it["group"]][it["requested"]] += 1
    out = {}
    for g, c in by.items():
        out[g] = sorted(c.items(), key=lambda kv: (-kv[1], RANK[kv[0]]))[0][0]
    return out


def prepare(items: list[dict], arms: list[str], rule: str, by_source: dict | None = None,
            voided: str | None = None) -> tuple[list[dict], dict]:
    by_source = by_source or {}
    imputed = _imputed_requested(items)
    rows: list[dict] = []
    n_no_truth = 0
    no_truth_src: Counter = Counter()
    n_no_rules = 0
    for it in items:
        src = source_of(it)
        t = truth_for(it, rule_for_source(src, rule, by_source), voided)
        if t is None:
            n_no_truth += 1
            no_truth_src[src] += 1
            continue
        base = it["preds"].get("rules_eff")
        if base is None:
            n_no_rules += 1
            continue
        preds, fb = {}, {}
        for a in arms:
            p = it["preds"].get(a)
            preds[a] = base if a == "rules_eff" else resolve_pred(p, base, t)
            fb[a] = a != "rules_eff" and p is None
        rows.append({"id": it["id"], "group": it["group"], "kind": it["kind"], "w": it["w"],
                     "fold": it["fold"], "truth": t, "req": it["requested"] or "opus",
                     "req_imp": it["requested"] or imputed.get(it["group"], "sonnet"),
                     "strata": it["strata"], "preds": preds, "fb": fb, "scores": it["scores"],
                     "joined": it["requested"] is not None})
    info = {"n_items": len(items), "n_no_truth": n_no_truth, "n_no_truth_by_source": dict(sorted(no_truth_src.items())),
            "n_no_rules_eff": n_no_rules}
    return rows, info


# --------------------------------------------------------------------------- metrics

def item_cost(pred: str, truth: str, req: str) -> float:
    return cost_eff(clamp(pred, req), truth, LAMBDA)


def _wmean(rows: list[dict], f) -> float | None:
    w = sum(r["w"] for r in rows)
    return sum(r["w"] * f(r) for r in rows) / w if w > 0 else None


def under_rate(rows: list[dict], arm: str):
    return _wmean(rows, lambda r: 1.0 if RANK[r["preds"][arm]] < RANK[r["truth"]] else 0.0)


def cost_mean(rows: list[dict], arm: str, imputed: bool = False):
    key = "req_imp" if imputed else "req"
    return _wmean(rows, lambda r: item_cost(r["preds"][arm], r["truth"], r[key]))


def raw_mean(rows: list[dict], arm: str):
    return _wmean(rows, lambda r: COST[r["preds"][arm]])


def r6(x):
    return None if x is None else round(x, 6)


def rate_block(k: int, n: int, weighted: float | None = None) -> dict:
    return {"k": k, "n": n, "rate": r6(weighted if weighted is not None else (k / n if n else None)),
            "ci95": wilson(k, n)}


def arm_metrics(rows: list[dict], arm: str) -> dict:
    n = len(rows)
    under_k = sum(RANK[r["preds"][arm]] < RANK[r["truth"]] for r in rows)
    tot_w = sum(r["w"] for r in rows)
    share = {t: r6(sum(r["w"] for r in rows if r["preds"][arm] == t) / tot_w) if tot_w else None
             for t in TIERS}
    routed = [r for r in rows if r["preds"][arm] == "haiku"]
    hit = sum(r["truth"] == "haiku" for r in routed)
    n_fb = sum(r["fb"][arm] for r in rows)
    return {
        "n": n, "under": rate_block(under_k, n, under_rate(rows, arm)),
        "c_lambda2_clamp": r6(cost_mean(rows, arm)), "c_lambda2_clamp_imputed_req": r6(cost_mean(rows, arm, True)),
        "raw_cost": r6(raw_mean(rows, arm)), "share": share,
        "haiku_precision": rate_block(hit, len(routed)),
        "haiku_recall": rate_block(hit, sum(r["truth"] == "haiku" for r in rows)),
        "n_fallback": n_fb, "fallback_share": r6(n_fb / n) if n else None,
        "errors_gt_5pct": bool(n and n_fb / n > ERR_KILL), "errors_gt_10pct": bool(n and n_fb / n > ERR_INFORMATIVE),
    }


# --------------------------------------------------------------------------- group sums and bootstrap

def group_sums(rows: list[dict], arms: list[str]) -> list[dict]:
    """Per-group sufficient sums, in sorted group order. A resample is a sum over these, O(groups)."""
    by: dict[str, dict] = {}
    keys = cluster_keys(rows)
    for r, g in zip(rows, keys):
        s = by.setdefault(g, defaultdict(float))
        w, rt = r["w"], RANK[r["truth"]]
        s["W"] += w
        s["Uh"] += w * (1 if 1 < rt else 0)
        s["Us"] += w * (1 if 2 < rt else 0)
        s["Ot"] += w * (1 if r["truth"] == "opus" else 0)
        for a in arms:
            p = r["preds"][a]
            s[f"u:{a}"] += w * (1 if RANK[p] < rt else 0)
            s[f"c:{a}"] += w * item_cost(p, r["truth"], r["req"])
            s[f"r:{a}"] += w * COST[p]
            s[f"o:{a}"] += w * (1 if p == "opus" else 0)
    return [dict(by[g]) for g in sorted(by)]


@functools.lru_cache(maxsize=16)
def resample_indices(n: int, B: int, seed: int) -> tuple:
    rng = random.Random(seed)
    return tuple(tuple(rng.randrange(n) for _ in range(n)) for _ in range(B))


def bootstrap(gs: list[dict], keys: list[str], stat, B: int = B_DEFAULT, seed: int = SEED) -> list:
    """stat(sums) over B group resamples (with replacement). Values that are None are dropped."""
    if not gs:
        return []
    cols = {k: [g.get(k, 0.0) for g in gs] for k in keys}
    out = []
    OPS["bootstraps"] += 1
    for idx in resample_indices(len(gs), B, seed):
        OPS["group_visits"] += len(idx)
        v = stat({k: sum(map(col.__getitem__, idx)) for k, col in cols.items()})
        if v is not None:
            out.append(v)
    return out


def totals(gs: list[dict], keys: list[str]) -> dict:
    return {k: sum(g.get(k, 0.0) for g in gs) for k in keys}


def ci95(sorted_vals: list[float]) -> list:
    """shadow_eval.boot_diff's index convention: lo = 2.5th, hi = 97.5th."""
    n = len(sorted_vals)
    if not n:
        return [None, None]
    return [sorted_vals[int(0.025 * n)], sorted_vals[max(int(0.975 * n) - 1, 0)]]


def upper95(sorted_vals: list[float]):
    """One-sided 95% upper bound: the 95th percentile."""
    n = len(sorted_vals)
    return sorted_vals[max(int(0.95 * n) - 1, 0)] if n else None


def ni_upper(sorted_vals: list[float]):
    """Non-inferiority bound: the upper end of the two-sided 95% CI (97.5th percentile), the same bound the
    reported CI prints (D-148). The guard stays one-sided (upper95)."""
    return ci95(sorted_vals)[1]


def lower_pct(sorted_vals: list[float], p: float):
    n = len(sorted_vals)
    return sorted_vals[min(int(p * n), n - 1)] if n else None


# --------------------------------------------------------------------------- guard, bars, non-inferiority

def mix_q(raw: float) -> float:
    """Opus share of the uninformed sonnet/opus mixture (raw >= sonnet cost), else the sonnet share of the
    haiku/sonnet mixture."""
    if raw >= COST["sonnet"]:
        q = (raw - COST["sonnet"]) / (COST["opus"] - COST["sonnet"])
    else:
        q = (raw - COST["haiku"]) / (COST["sonnet"] - COST["haiku"])
    return min(1.0, max(0.0, q))


def mix_under(raw: float, uh: float, us: float) -> float:
    """Expected under-route share of the mixture at raw cost ``raw``; uh, us = share of items whose truth is
    above haiku / above sonnet. Opus never under-routes."""
    q = mix_q(raw)
    if raw >= COST["sonnet"]:
        return (1 - q) * us
    return (1 - q) * uh + q * us


def guard(upper, raw: float) -> bool:
    """NOT-TRIVIAL guard: the one-sided 95% upper bound of under(cand) - E[under(mix)] is below 0."""
    return upper is not None and upper < -GUARD_EPS


def bars_hold(under_c, under_b, cost_c, cost_b) -> bool:
    return under_c <= under_b + EQ_EPS and cost_c <= cost_b + EQ_EPS


def non_inferior(under_upper, cost_upper) -> bool:
    return under_upper <= NI_UNDER + EQ_EPS and cost_upper <= NI_COST + EQ_EPS


def significantly_worse(ci) -> bool:
    return ci[0] is not None and ci[0] > 0


def label_core(bars: bool, ni: bool, g: bool, ci_under, ci_cost) -> str:
    if (not bars or not ni or not g or significantly_worse(ci_under) or significantly_worse(ci_cost)):
        return "FAIL"
    if ci_under[1] <= 0 and ci_cost[1] <= 0:
        return "PASS-STRONG"
    return "PASS-WEAK"


def display_status(core: str, mode: str) -> str:
    """DEV mode never prints a PASS label (PREREG-CLS-DEV section 4)."""
    if mode == "test":
        return core
    return "SCREEN-PASS" if core.startswith("PASS") else "SCREEN-REJECT"


def analyze_arm(gs: list[dict], arm: str, base: str = "rules_eff", B: int = B_DEFAULT, seed: int = SEED) -> dict:
    """Paired comparison of ``arm`` against ``base`` on one bootstrap pass: diffs with CIs, bars,
    non-inferiority, the NOT-TRIVIAL guard. All figures are shares / cost units."""
    keys = ["W", f"u:{arm}", f"u:{base}", f"c:{arm}", f"c:{base}", f"r:{arm}", f"r:{base}", "Uh", "Us"]

    def stat(s):
        W = s["W"]
        if W <= 0:
            return None
        uc, ub = s[f"u:{arm}"] / W, s[f"u:{base}"] / W
        cc, cb = s[f"c:{arm}"] / W, s[f"c:{base}"] / W
        raw = s[f"r:{arm}"] / W
        return (uc - ub, cc - cb, uc - mix_under(raw, s["Uh"] / W, s["Us"] / W), raw)

    t = totals(gs, keys)
    point = stat(t)
    if point is None:
        return {"evaluable": False}
    vals = bootstrap(gs, keys, stat, B, seed)
    du = sorted(v[0] for v in vals)
    dc = sorted(v[1] for v in vals)
    dg = sorted(v[2] for v in vals)
    W = t["W"]
    uc, ub, cc, cb, raw = (t[f"u:{arm}"] / W, t[f"u:{base}"] / W, t[f"c:{arm}"] / W, t[f"c:{base}"] / W, point[3])
    q = mix_q(raw)
    e_mix = mix_under(raw, t["Uh"] / W, t["Us"] / W)
    g_up = upper95(dg)
    ci_u, ci_c = ci95(du), ci95(dc)
    bars = bars_hold(uc, ub, cc, cb)
    ni_u, ni_c = ni_upper(du), ni_upper(dc)
    ni = non_inferior(ni_u, ni_c)
    g_ok = guard(g_up, raw)
    rb = t[f"r:{base}"] / W
    raw_ci = ci95(sorted(v[3] for v in vals))
    ratio = raw / rb if rb else None
    return {
        "evaluable": True,
        "under": {"cand": r6(uc), "base": r6(ub), "diff": r6(uc - ub), "ci95": [r6(x) for x in ci_u],
                  "upper95": r6(upper95(du)), "ni_upper": r6(ni_u), "se": r6(statistics.stdev(du)) if len(du) > 1 else None},
        "cost": {"cand": r6(cc), "base": r6(cb), "diff": r6(cc - cb), "ci95": [r6(x) for x in ci_c],
                 "upper95": r6(upper95(dc)), "ni_upper": r6(ni_c), "se": r6(statistics.stdev(dc)) if len(dc) > 1 else None},
        "raw_cost": r6(raw), "raw_cost_ci95": [r6(x) for x in raw_ci], "raw_ratio_to_rules_eff": r6(ratio),
        "COST_HIGH": bool(ratio is not None and ratio > COST_HIGH_RATIO), "bars": bars,
        "non_inferior": {"ok": ni, "under_upper": r6(ni_u), "cost_upper": r6(ni_c),
                         "under_bound": NI_UNDER, "cost_bound": NI_COST},
        "guard": {"raw_cost": r6(raw), "q": r6(q), "e_under_mix": r6(e_mix), "diff": r6(uc - e_mix),
                  "upper95": r6(g_up), "passes": g_ok},
        "_core": label_core(bars, ni, g_ok, ci_u, ci_c), "_worse": significantly_worse(ci_u) or significantly_worse(ci_c),
    }


# --------------------------------------------------------------------------- AUROC and kappa

def _auc(blocks: list[list[int]], pos: list[bool], wt: list[float]):
    cum_neg = num = P = N = 0.0
    for blk in blocks:
        pw = sum(wt[i] for i in blk if pos[i])
        nw = sum(wt[i] for i in blk if not pos[i])
        num += pw * (cum_neg + 0.5 * nw)
        cum_neg += nw
        P += pw
        N += nw
    return num / (P * N) if P > 0 and N > 0 else None


def auroc_block(rows: list[dict], arm: str, B: int, seed: int) -> dict:
    pts = []
    for r in rows:
        s = r["scores"].get(arm)
        pts.append((s[2], r) if s else (None, r))
    basis = "p_opus"
    if all(p is None for p, _ in pts):
        basis = "verdict_rank"
        pts = [(float(RANK[r["preds"][arm]]), r) for r in rows]
    pts = [(p, r) for p, r in pts if p is not None]
    if not pts:
        return {"auroc": None, "basis": basis, "n": 0}
    pts.sort(key=lambda pr: pr[0])
    blocks: list[list[int]] = []
    for i, (p, _) in enumerate(pts):
        if blocks and pts[blocks[-1][0]][0] == p:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    pos = [r["truth"] == "opus" for _, r in pts]
    wt = [r["w"] for _, r in pts]
    point = _auc(blocks, pos, wt)
    gkeys = sorted({g for g in cluster_keys([r for _, r in pts])})
    gix = {g: i for i, g in enumerate(gkeys)}
    item_g = [gix[g] for g in cluster_keys([r for _, r in pts])]
    vals = []
    OPS["bootstraps"] += 1
    for idx in resample_indices(len(gkeys), B, seed):
        OPS["group_visits"] += len(idx)
        mult = Counter(idx)
        v = _auc(blocks, pos, [wt[i] * mult.get(item_g[i], 0) for i in range(len(pts))])
        if v is not None:
            vals.append(v)
    vals.sort()
    lo, hi = ci95(vals)
    return {"auroc": r6(point), "ci95": [r6(lo), r6(hi)], "basis": basis, "n": len(pts),
            "n_pos": int(sum(pos))}


def qwk(m: list[list[float]]):
    N = sum(map(sum, m))
    if N <= 0:
        return None
    rs = [sum(m[i]) for i in range(3)]
    cs = [sum(m[i][j] for i in range(3)) for j in range(3)]
    num = sum(((i - j) ** 2 / 4) * m[i][j] for i in range(3) for j in range(3))
    den = sum(((i - j) ** 2 / 4) * rs[i] * cs[j] / N for i in range(3) for j in range(3))
    return None if den == 0 else 1 - num / den


def _kappa_point(pairs: list) -> dict:
    m = [[0.0] * 3 for _ in range(3)]
    for _, i, j in pairs:
        m[i][j] += 1
    if len(pairs) < 5:
        return {"n": len(pairs)}
    return {"n": len(pairs), "kappa": r6(qwk(m)), "exact_agreement": r6(sum(i == j for _, i, j in pairs) / len(pairs))}


def kappa_block(items: list[dict], B: int, seed: int) -> dict:
    out: dict = {}
    pairs_of = (("opus", "gpt"), ("opus", "sonnet"), ("gpt", "sonnet"))
    for kind in ("real", "syn"):
        for a, b in pairs_of:
            pairs = [(it["group"], RANK[it["truths"][a]] - 1, RANK[it["truths"][b]] - 1)
                     for it in items if it["kind"] == kind and it["truths"][a] and it["truths"][b]]
            name = f"{kind}:{a}-{b}"
            if not pairs:
                out[name] = {"n": 0, "kappa": None}
                continue
            by: dict[str, dict] = {}
            for g, i, j in pairs:
                s = by.setdefault(g, defaultdict(float))
                s[f"m{i}{j}"] += 1
            gs = [dict(by[g]) for g in sorted(by)]
            keys = [f"m{i}{j}" for i in range(3) for j in range(3)]

            def stat(s):
                return qwk([[s[f"m{i}{j}"] for j in range(3)] for i in range(3)])

            point = stat(totals(gs, keys))
            vals = sorted(bootstrap(gs, keys, stat, B, seed))
            out[name] = {"n": len(pairs), "kappa": r6(point), "ci95": [r6(x) for x in ci95(vals)],
                         "exact_agreement": r6(sum(i == j for _, i, j in pairs) / len(pairs))}
    # per length band and per source (PREREG-CLS-DEV section 3.4): point values, a table with n < 5 prints n only
    for dim, label in (("length_band", "by_length_band"), ("source", "by_source")):
        tab: dict = {}
        for it in items:
            v = it["strata"].get(dim)
            tab.setdefault(v if isinstance(v, str) and v else "unknown", []).append(it)
        out[label] = {val: {f"{a}-{b}": _kappa_point([(it["group"], RANK[it["truths"][a]] - 1, RANK[it["truths"][b]] - 1)
                                                         for it in its if it["truths"][a] and it["truths"][b]])
                            for a, b in pairs_of} for val, its in sorted(tab.items())}
    return out


# --------------------------------------------------------------------------- validity, robustness

def floor_sign(diff, se):
    """Sign of a paired difference, or None (indeterminate) when it is less than 1 bootstrap SE from 0."""
    if diff is None or se is None or diff == 0 or abs(diff) < se:
        return None
    return 1 if diff > 0 else -1


def _robust_leg(items: list[dict], arms: list[str], B: int, seed: int, population: str, pair: tuple,
                extra: tuple = ()) -> dict:
    judges = tuple(pair) + tuple(extra)
    per_rule = {}
    for j in judges:
        rows, _ = prepare(items, arms, f"{j}_only")
        rows = select_population(rows, population)
        gs = group_sums(rows, arms)
        per_rule[j] = {a: analyze_arm(gs, a, "rules_eff", B, seed) for a in arms if a != "rules_eff"} if rows else {}
    out = {"population": population, "pair": list(pair), "arms": {}}
    for a in arms:
        if a == "rules_eff":
            continue
        entry = {}
        ok = all(per_rule[j].get(a, {}).get("evaluable") for j in judges)
        for metric in ("under", "cost"):
            if not ok:
                entry[metric] = {"evaluable": False}
                continue
            m = {}
            for j in judges:
                d, se_ = (per_rule[j][a][metric][k] for k in ("diff", "se"))
                m[f"diff_{j}"], m[f"se_{j}"], m[f"sign_{j}"] = d, se_, floor_sign(d, se_)
            s1, s2 = m[f"sign_{pair[0]}"], m[f"sign_{pair[1]}"]
            m["indeterminate"] = s1 is None or s2 is None
            m["discordant"] = s1 is not None and s2 is not None and s1 != s2
            entry[metric] = m
        out["arms"][a] = entry
    return out


def judge_robustness(items: list[dict], arms: list[str], B: int, seed: int, voided) -> dict:
    """Criterion 8 (PREREG-CLS-DEV section 8): the paired diff vs rules_eff under each single-judge truth, with
    the 1-bootstrap-SE floor. Real leg: T-opus vs T-gpt (T-sonnet reported beside them). DEV-SYN leg: T-gpt vs
    T-sonnet on the syn rows."""
    out = _robust_leg(items, arms, B, seed, "real", ("opus", "gpt"), ("sonnet",))
    out["syn_leg"] = _robust_leg(items, arms, B, seed, "syn", ("gpt", "sonnet"))
    return out


def validity_checks(gs: list[dict], arms: list[str], B: int, seed: int, mode: str) -> dict:
    """Section 6: always_opus must fail the guard, noised_oracle must PASS-STRONG, known-better must
    separate from known-worse. Computed by arm NAME; a check whose arms are absent is 'not_run'."""
    have = set(arms)
    out: dict = {"checks": {}, "reasons": [], "not_run": []}
    if "always_opus" in have:
        an = analyze_arm(gs, "always_opus", "rules_eff", B, seed)
        broken = an.get("evaluable") and an["guard"]["passes"]
        out["checks"]["always_opus_fails_guard"] = not broken
        if broken:
            out["reasons"].append("guard_broken")
    else:
        out["not_run"].append("always_opus_fails_guard")
    if "noised_oracle" in have:
        an = analyze_arm(gs, "noised_oracle", "rules_eff", B, seed)
        strong = bool(an.get("evaluable")) and an["_core"] == "PASS-STRONG"
        out["checks"]["noised_oracle_strong"] = strong
        if not strong:
            out["reasons"].append("cannot_separate")
    else:
        out["not_run"].append("noised_oracle_strong")
    if "qwen3coder" in have and "nimble" in have:
        sep = _paired_diff(gs, "qwen3coder", "nimble", B, seed)
        improving = sep["under"]["ci95"][1] < 0 or sep["cost"]["ci95"][1] < 0
        reversed_ = sep["under"]["ci95"][0] > 0 or sep["cost"]["ci95"][0] > 0
        out["checks"]["known_better_separates"] = bool(improving and not reversed_)
        out["spread_qwen3coder_minus_nimble"] = {"under": sep["under"], "cost": sep["cost"]}
        if not (improving and not reversed_):
            out["reasons"].append("not_separated")
    else:
        out["not_run"].append("known_better_separates")
    worse = {}
    for a in KNOWN_WORSE:
        if a in have:
            an = analyze_arm(gs, a, "rules_eff", B, seed)
            worse[a] = {"fails_bars": not an["bars"]} if an.get("evaluable") else {"fails_bars": None}
    out["known_worse"] = worse
    out["ok"] = not out["reasons"]
    return out


def _paired_diff(gs: list[dict], a: str, b: str, B: int, seed: int) -> dict:
    keys = ["W", f"u:{a}", f"u:{b}", f"c:{a}", f"c:{b}"]

    def stat(s):
        W = s["W"]
        return None if W <= 0 else ((s[f"u:{a}"] - s[f"u:{b}"]) / W, (s[f"c:{a}"] - s[f"c:{b}"]) / W)

    vals = bootstrap(gs, keys, stat, B, seed)
    point = stat(totals(gs, keys))
    out = {}
    for j, name in enumerate(("under", "cost")):
        col = sorted(v[j] for v in vals)
        out[name] = {"diff": r6(point[j]), "ci95": [r6(x) for x in ci95(col)]}
    return out


# --------------------------------------------------------------------------- strata

def _stratum_value(r: dict, dim: str) -> str:
    if dim == "truth":
        return r["truth"]
    v = r["strata"].get(dim)
    if dim == "contested":
        return "contested" if v is True else ("agreed" if v is False else "unknown")
    return v if isinstance(v, str) and v else "unknown"


def strata_tables(rows: list[dict], arms: list[str]) -> dict:
    out: dict = {}
    for a in arms:
        per: dict = {}
        for dim in STRATA_DIMS:
            groups: dict[str, list[dict]] = defaultdict(list)
            for r in rows:
                groups[_stratum_value(r, dim)].append(r)
            tab = {}
            for val, rs in sorted(groups.items()):
                k = sum(RANK[r["preds"][a]] < RANK[r["truth"]] for r in rs)
                if len(rs) < 5:
                    tab[val] = {"n": len(rs), "under_k": k}
                else:
                    tab[val] = {"n": len(rs), "under_k": k, "under": r6(under_rate(rs, a)),
                                "c_lambda2_clamp": r6(cost_mean(rs, a)), "raw_cost": r6(raw_mean(rs, a))}
            per[dim] = tab
        out[a] = per
    return out


# --------------------------------------------------------------------------- threshold fit

def decide(sc: tuple, t_hi: float, t_lo: float) -> str:
    ph, _, po = sc
    if po >= t_hi:
        return "opus"
    return "haiku" if ph >= t_lo else "sonnet"


def opus_share_ok(share: float, truth_share: float) -> bool:
    return share <= truth_share + EQ_EPS


def haiku_ok(n_routes: int, precision, t_lo: float) -> bool:
    if t_lo >= 1.01:
        return True
    if n_routes < HAIKU_MIN_ROUTES:
        return False
    return precision is not None and precision >= HAIKU_MIN_PRECISION


def _point_stats(rows: list[dict], preds: tuple) -> dict:
    W = sum(r["w"] for r in rows)
    cost = sum(r["w"] * item_cost(p, r["truth"], r["req"]) for r, p in zip(rows, preds)) / W
    raw = sum(r["w"] * COST[p] for r, p in zip(rows, preds)) / W
    opus = sum(r["w"] for r, p in zip(rows, preds) if p == "opus") / W
    hroutes = [(r, p) for r, p in zip(rows, preds) if p == "haiku"]
    hw = sum(r["w"] for r, _ in hroutes)
    prec = (sum(r["w"] for r, _ in hroutes if r["truth"] == "haiku") / hw) if hw > 0 else None
    return {"cost": cost, "raw": raw, "opus": opus, "n_h": len(hroutes), "prec": prec}


def tie_key(st: dict, hi: float, lo: float) -> tuple:
    """Objective, then ties: lower opus share, lower raw cost, larger t_hi, larger t_lo."""
    return (round(st["cost"], 12), round(st["opus"], 12), round(st["raw"], 12), -hi, -lo)


def fit(rows: list[dict], arm: str, B: int = B_DEFAULT, seed: int = SEED) -> dict:
    """Deterministic grid fit on ``rows`` (the training folds): minimise weighted C-lambda2-clamp over the
    grid points that meet constraints 1-4 of PREREG-CLS-DEV section 7.2."""
    W = sum(r["w"] for r in rows)
    truth_opus = sum(r["w"] for r in rows if r["truth"] == "opus") / W if W else 0.0
    sc = [r["scores"][arm] for r in rows]
    cache: dict[tuple, dict] = {}
    cands = []
    for hi in GRID_HI:
        for lo in GRID_LO:
            preds = tuple(decide(s, hi, lo) for s in sc)
            if preds not in cache:
                cache[preds] = _point_stats(rows, preds)
            st = cache[preds]
            cands.append((tie_key(st, hi, lo), hi, lo, preds))
    cands.sort(key=lambda c: c[0])
    boot_cache: dict[tuple, dict] = {}
    checked = 0
    for _, hi, lo, preds in cands:
        st = cache[preds]
        if not opus_share_ok(st["opus"], truth_opus) or not haiku_ok(st["n_h"], st["prec"], lo):
            continue
        if preds not in boot_cache:
            trial = [dict(r, preds=dict(r["preds"], _cand=p)) for r, p in zip(rows, preds)]
            gs = group_sums(trial, ["rules_eff", "_cand"])
            checked += 1
            keys = ["W", "u:_cand", "u:rules_eff", "r:_cand", "Uh", "Us"]

            def stat(s):
                Wb = s["W"]
                if Wb <= 0:
                    return None
                uc = s["u:_cand"] / Wb
                return (s["u:rules_eff"] / Wb - uc,
                        uc - mix_under(s["r:_cand"] / Wb, s["Uh"] / Wb, s["Us"] / Wb))

            vals = bootstrap(gs, keys, stat, B, seed)
            gain = sorted(v[0] for v in vals)
            gd = sorted(v[1] for v in vals)
            boot_cache[preds] = {"p20_gain": lower_pct(gain, ADMISSIBLE_PCT), "g_upper": upper95(gd),
                                 "raw": st["raw"]}
        bc = boot_cache[preds]
        if bc["p20_gain"] is None or bc["p20_gain"] < -EQ_EPS:
            continue
        if not guard(bc["g_upper"], bc["raw"]):
            continue
        return {"admissible": True, "t_hi": hi, "t_lo": lo, "n_haiku_routes": st["n_h"],
                "opus_share": r6(st["opus"]), "truth_opus_share": r6(truth_opus), "cost": r6(st["cost"]),
                "raw_cost": r6(st["raw"]), "boot_points_checked": checked, "n_train": len(rows)}
    return {"admissible": False, "boot_points_checked": checked, "n_train": len(rows),
            "truth_opus_share": r6(truth_opus)}


def oos_decisions(rows: list[dict], arm: str, B: int, seed: int) -> dict:
    folds = sorted({r["fold"] for r in rows})
    if len(folds) < 2:
        raise Refuse(2, "refusing: the fit needs at least 2 folds")
    dec: dict[str, str] = {}
    per_fold = []
    for f in folds:
        res = fit(train_rows(rows, f), arm, B, seed)
        per_fold.append({"fold": f, **{k: res.get(k) for k in ("admissible", "t_hi", "t_lo")}})
        if not res["admissible"]:
            return {"admissible": False, "failed_fold": f, "fold_thresholds": per_fold}
        for r in rows:
            if r["fold"] == f:
                dec[r["id"]] = decide(r["scores"][arm], res["t_hi"], res["t_lo"])
    return {"admissible": True, "decisions": dec, "fold_thresholds": per_fold}


def check_folds(items: list[dict]) -> None:
    seen: dict[str, int] = {}
    for it in items:
        if it["fold"] is None:
            raise Refuse(2, "refusing: an item has no fold")
        if seen.setdefault(it["group"], it["fold"]) != it["fold"]:
            raise Refuse(2, "refusing: a group spans folds")


def check_scores(items: list[dict], arm: str) -> None:
    if arm is None or any(arm not in it["scores"] for it in items):
        raise Refuse(2, "refusing: --arm must name an arm with scores on every item")


def with_decisions(rows: list[dict], name: str, dec: dict) -> list[dict]:
    return [dict(r, preds=dict(r["preds"], **{name: dec[r["id"]]}), fb=dict(r["fb"], **{name: False}))
            for r in rows]


# --------------------------------------------------------------------------- score

def _arm_report(rows: list[dict], arms: list[str], B: int, seed: int):
    gs = group_sums(rows, arms)
    return gs, {a: analyze_arm(gs, a, "rules_eff", B, seed) for a in arms if a != "rules_eff"}


def _rule_block(items, arms, rule, by_source, voided, population, B, seed, screen_reasons, mode, primary=False):
    rows_all, info = prepare(items, arms, rule, by_source if primary else None, voided if primary else None)
    rows = select_population(rows_all, population)
    n = len(rows)
    n_items = len(items)
    reasons = list(screen_reasons)
    block: dict = {"rule": rule, "population": population, "n": n, **info}
    block["no_truth_share"] = r6(info["n_no_truth"] / n_items) if n_items else None
    n_opus = sum(r["truth"] == "opus" for r in rows)
    block["n_truth_opus"] = n_opus
    block["low_power"] = n_opus < N_OPUS_LOWPOWER
    if n_items and info["n_no_truth"] / n_items > MISSING_TRUTH_MAX:
        reasons.append("truth_missing")
    if n_opus < N_OPUS_MIN:
        reasons.append("n_truth_opus_lt_40")
    block["evaluable"] = n > 0
    if not rows:
        block.update({"comparisons": {}, "reasons": reasons})
        return block, rows, None
    gs, comp = _arm_report(rows, arms, B, seed)
    out = {}
    for a, an in comp.items():
        arm_reasons = list(reasons)
        m_n = sum(r["fb"][a] for r in rows)
        if m_n / n > ERR_INFORMATIVE:
            arm_reasons.append("errors")
        status = "NOT INFORMATIVE" if arm_reasons else display_status(an.get("_core", "FAIL"), mode)
        entry = {k: v for k, v in an.items() if not k.startswith("_")}
        entry.update({"status": status, "reasons": sorted(set(arm_reasons)),
                      "significantly_worse": an.get("_worse", False), "_core": an.get("_core")})
        out[a] = entry
    block["comparisons"] = out
    block["reasons"] = reasons
    return block, rows, gs


def dev_labels(mode: str, header: dict) -> dict:
    """R2-6: every DEV-mode result is guard-free and screening only, and says so."""
    if mode == "dev":
        return {"guard_free": True, "screening_only": True, "label": DEV_LABEL}
    return {"guard_free": not bool(header.get("guards_applied")), "screening_only": False}


def _fallback_judge(rule: str, voided):
    if rule.startswith("other_two_higher:"):
        return rule.split(":", 1)[1]
    if rule == "other_two_higher":
        return voided
    return "gpt" if rule == "gpt_excluded" else None


def single_use_validity_reasons(validity: dict, single_use: bool) -> list[str]:
    """A single-use run whose validity arms are absent proves nothing: NOT INFORMATIVE."""
    return ["validity_unavailable"] if single_use and validity["not_run"] else []


def run_score(args, header, items, table_sha, harness_sha, se_sha, cal) -> dict:
    mode = args.mode
    seed, B = args.seed, args.boot_b
    arms = arms_of(items)
    by_source = parse_by_source(args.truth_by_source)
    voided = args.voided
    population = args.population
    warnings: list[str] = []

    screen_reasons: list[str] = []
    cal_block = {"provided": cal is not None}
    if cal is None:
        if args.single_use:
            screen_reasons.append("calibration_unknown")
        else:
            warnings.append("calibration_unknown")
    else:
        bad = sorted(j for j, v in cal.items() if v.get("planted_ok") != v.get("planted_n"))
        cal_block["failed_judges"] = bad
        if bad:
            screen_reasons.append("calibration")

    rows_all, info = prepare(items, arms, args.truth, by_source, voided)
    prim_rows = select_population(rows_all, population)
    gs_v = group_sums(prim_rows, arms) if prim_rows else []
    validity = validity_checks(gs_v, arms, B, seed, mode) if prim_rows else {"checks": {}, "reasons": [], "not_run": [], "ok": True}
    screen_reasons += validity["reasons"]
    screen_reasons += single_use_validity_reasons(validity, args.single_use)

    primary, rows, gs = _rule_block(items, arms, args.truth, by_source, voided, population, B, seed,
                                    screen_reasons, mode, primary=True)
    sens = {}
    for rule in SENSITIVITY:
        sblock, _, _ = _rule_block(items, arms, rule, by_source, voided, population, B, seed, screen_reasons, mode)
        sens[rule] = sblock
    robust = {}
    for a in arms:
        if a == "rules_eff":
            continue
        ev = [sens.get(r, {}).get("comparisons", {}).get(a) for r in ROBUST_RULES]
        if any(e is None or not e.get("evaluable", True) for e in ev):
            robust[a] = None
        else:
            robust[a] = not any(e["significantly_worse"] for e in ev)

    result: dict = {
        "command": "score", "mode": mode, "population": population,
        "harness_sha": harness_sha, "shadow_eval_sha": se_sha, "table_sha": table_sha,
        "seed": seed, "B": B, "n_items": len(items), "n": len(rows),
        "n_groups": len({r["group"] for r in rows}),
        "largest_group_share": r6(max(Counter(it["group"] for it in items).values()) / len(items)),
        "truth": {"primary": args.truth, "by_source": by_source, "truth_fallback": _fallback_judge(args.truth, voided)},
        "primary": primary, "sensitivity": sens, "robust_under_single_judge_truths": robust,
        "calibration": cal_block, "validity": validity, "warnings": warnings,
        "arms": {a: arm_metrics(rows, a) for a in arms} if rows else {},
        "strata": strata_tables(rows, arms) if rows else {},
        "kappa": kappa_block(items, B, seed),
        "auroc": {a: auroc_block(select_population(rows_all, "real"), a, B, seed) for a in arms if a != "rules_eff"}
        if rows_all else {},
        "auroc_population": "real",
        "judge_robustness": judge_robustness(items, arms, B, seed, voided),
    }
    result.update(dev_labels(mode, header))
    result["reasons"] = sorted(set(primary.get("reasons", [])))
    result["status"] = "NOT INFORMATIVE" if result["reasons"] else "INFORMATIVE"
    if args.list_fallback_ids:
        result["fallback_ids"] = {a: sorted(r["id"] for r in rows if r["fb"][a]) for a in arms if a != "rules_eff"}
    for blk in [primary, *sens.values()]:
        for e in blk.get("comparisons", {}).values():
            e.pop("_core", None)
    if args.pooled_oos:
        result["pooled_oos"] = pooled_oos(items, arms, args, by_source, voided, B, seed, mode)
    if args.split_rows:
        result["split_rows"] = split_rows_report(items, arms, args, by_source, voided, B, seed, mode)
    return result


def _oos_rows(items, arms, args, by_source, voided, B, seed):
    arm = args.arm
    check_folds(items)
    check_scores(items, arm)
    rows_all, _ = prepare(items, arms, args.truth, by_source, voided)
    fit_rows = select_population(rows_all, "pooled")
    oos = oos_decisions(fit_rows, arm, B, seed)
    return rows_all, oos


def pooled_oos(items, arms, args, by_source, voided, B, seed, mode) -> dict:
    """Criterion 4 helper: the second-level CV decisions scored against the bars, guard and bounds."""
    rows_all, oos = _oos_rows(items, arms, args, by_source, voided, B, seed)
    name = f"{args.arm}@oos"
    if not oos["admissible"]:
        return {"admissible": False, "meets_bars": False, "guard": None, "non_inf": None,
                "failed_fold": oos["failed_fold"], "fold_thresholds": oos["fold_thresholds"],
                "population": args.population}
    rows = select_population(with_decisions(rows_all, name, oos["decisions"]), args.population)
    gs = group_sums(rows, ["rules_eff", name])
    an = analyze_arm(gs, name, "rules_eff", B, seed)
    return {"admissible": True, "population": args.population, "n": len(rows), "meets_bars": an["bars"],
            "guard": an["guard"], "non_inf": an["non_inferior"], "under": an["under"], "cost": an["cost"],
            "raw_cost": an["raw_cost"], "fold_thresholds": oos["fold_thresholds"]}


def split_rows_report(items, arms, args, by_source, voided, B, seed, mode) -> dict:
    """Criterion 6 helper: the same pooled out-of-sample decisions scored on each kind separately."""
    kinds = [k.strip() for k in args.split_rows.split(",") if k.strip()]
    if not kinds or any(k not in ("real", "syn") for k in kinds):
        raise Refuse(2, "refusing: --split-rows wants real,syn")
    rows_all, oos = _oos_rows(items, arms, args, by_source, voided, B, seed)
    if not oos["admissible"]:
        return {"admissible": False, "failed_fold": oos["failed_fold"], "fold_thresholds": oos["fold_thresholds"]}
    name = f"{args.arm}@oos"
    rows = with_decisions(rows_all, name, oos["decisions"])
    out: dict = {"admissible": True, "kinds": {}}
    for kind, part in split_by_kind(rows, kinds).items():
        if not part:
            out["kinds"][kind] = {"n": 0, "evaluable": False}
            continue
        gs = group_sums(part, ["rules_eff", name])
        an = analyze_arm(gs, name, "rules_eff", B, seed)
        conds = bool(an["bars"] and an["non_inferior"]["ok"] and an["guard"]["passes"])
        out["kinds"][kind] = {"n": len(part), "n_groups": len(gs), "bars": an["bars"],
                              "guard": an["guard"], "non_inf": an["non_inferior"], "under": an["under"],
                              "cost": an["cost"], "significantly_worse": an["_worse"],
                              "meets_criterion4_conditions": conds}
    return out


def run_fit(args, header, items, table_sha, harness_sha, se_sha) -> tuple[dict, int]:
    seed, B = args.seed, args.boot_b
    arms = arms_of(items)
    by_source = parse_by_source(args.truth_by_source)
    check_folds(items)
    check_scores(items, args.arm)
    rows_all, info = prepare(items, arms, args.truth, by_source, args.voided)
    rows = select_population(rows_all, args.population)
    if not rows:
        raise Refuse(2, "refusing: no scorable rows")
    ops0 = dict(OPS)
    final = fit(rows, args.arm, B, seed)
    oos = oos_decisions(rows, args.arm, B, seed)
    out: dict = {"command": "fit", "arm": args.arm, "population": args.population, "truth": args.truth,
                 "weighted": any(r["w"] != 1.0 for r in rows), "grid_size": len(GRID_HI) * len(GRID_LO),
                 "harness_sha": harness_sha, "shadow_eval_sha": se_sha, "table_sha": table_sha,
                 "seed": seed, "B": B, "n": len(rows), "n_groups": len({r["group"] for r in rows}),
                 "n_no_truth": info["n_no_truth"], "frozen": final, "fold_thresholds": oos["fold_thresholds"],
                 "bootstrap_group_visits": OPS["group_visits"] - ops0["group_visits"],
                 **dev_labels(args.mode, header)}
    if not final["admissible"] or not oos["admissible"]:
        out["admissible"] = False
        if not oos["admissible"]:
            out["failed_fold"] = oos["failed_fold"]
        return out, 5
    out["admissible"] = True
    name = f"{args.arm}@oos"
    rows_o = with_decisions(rows, name, oos["decisions"])
    gs = group_sums(rows_o, ["rules_eff", name])
    an = analyze_arm(gs, name, "rules_eff", B, seed)
    out["oos"] = {"metrics": arm_metrics(rows_o, name),
                  **{k: an[k] for k in ("under", "cost", "raw_cost", "bars", "non_inferior", "guard")}}
    if args.decisions_out:
        write_private(args.decisions_out, "".join(
            json.dumps({"item_id": r["id"], "decision": oos["decisions"][r["id"]]}, sort_keys=True) + "\n"
            for r in rows))
    return out, 0


# --------------------------------------------------------------------------- noised oracle, selftest

def make_noised_oracle(items: list[dict], raws: list[dict], rule: str, by_source: dict, voided, seed: int) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for it, raw in zip(items, raws):
        t = truth_for(it, rule_for_source(source_of(it), rule, by_source), voided)
        pred = None
        if t is not None:
            r = RANK[t]
            if rng.random() < 0.25:
                opts = [x for x in (r - 1, r + 1) if 1 <= x <= 3]
                r = rng.choice(opts)
            pred = TIERS[r - 1]
        rec = dict(raw)
        rec["preds"] = dict(raw.get("preds") or {}, noised_oracle=pred)
        out.append(rec)
    return out


def run_selftest(args, header, items, table_sha, harness_sha, se_sha, cal) -> tuple[dict, int]:
    arms = arms_of(items)
    rows, info = prepare(items, arms, args.truth, parse_by_source(args.truth_by_source), args.voided)
    rows = select_population(rows, args.population)
    gs = group_sums(rows, arms) if rows else []
    validity = validity_checks(gs, arms, args.boot_b, args.seed, args.mode) if rows else {"checks": {}, "reasons": ["no_rows"], "not_run": [], "ok": False}
    out = {"command": "selftest", "mode": args.mode, "harness_sha": harness_sha, "shadow_eval_sha": se_sha,
           "table_sha": table_sha, "seed": args.seed, "B": args.boot_b, "n": len(rows),
           "validity": validity, "status": "INFORMATIVE" if validity["ok"] else "NOT INFORMATIVE",
           "reasons": validity["reasons"]}
    out.update(dev_labels(args.mode, header))
    return out, 0 if validity["ok"] else 1


# --------------------------------------------------------------------------- io, cli

def write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def start_use_log(path: str, harness_sha: str, table_sha: str) -> None:
    """Append the "started" line BEFORE scoring; refuse (exit 4) when one exists. Never rewritten."""
    p = Path(path)
    if p.exists():
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            raise Refuse(4, "refusing: the use log cannot be read") from None
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("event") == "started":
                raise Refuse(4, "refusing: the use log already holds a started line (single use)")
    rec = {"event": "started", "utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "harness_sha": harness_sha, "table_sha": table_sha}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def load_calibration(path: str | None):
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refuse(2, "refusing: the calibration file cannot be read") from None
    if isinstance(data, dict) and isinstance(data.get("calibration"), dict):
        data = data["calibration"]
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise Refuse(2, "refusing: the calibration file must map judge -> {planted_ok, planted_n}")
    return data


def _header_calibration(header: dict):
    cal = header.get("calibration")
    if cal is None:
        return None
    if not isinstance(cal, dict) or not all(isinstance(v, dict) for v in cal.values()):
        raise Refuse(2, "refusing: the header calibration must map judge -> {planted_ok, planted_n}")
    return cal


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("table", help="JSONL arms table")
    common.add_argument("--truth", default="majority3", help="primary truth rule (default majority3)")
    common.add_argument("--voided", choices=JUDGES, help="voided judge for --truth other_two_higher")
    common.add_argument("--truth-by-source", help="SRC=RULE[,SRC=RULE...] (default: --truth for every source)")
    common.add_argument("--population", choices=("pooled", "real", "syn"), default="pooled")
    common.add_argument("--mode", choices=("dev", "test"), default="dev")
    common.add_argument("--seed", type=int, default=SEED)
    common.add_argument("--boot-b", type=int, default=B_DEFAULT)
    common.add_argument("--out", help="also write the JSON here (mode 0600)")
    common.add_argument("--pin-shadow-eval-sha", help="exit 3 when shadow_eval.py differs from this sha256")
    common.add_argument("--make-noised-oracle", type=int, metavar="SEED",
                        help="write TABLE plus a noised_oracle arm to --out and exit")
    ap = argparse.ArgumentParser(prog="cls_eval.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score", parents=[common], help="score arms against a truth rule")
    sc.add_argument("--calibration", help="JSON {judge: {planted_ok, planted_n}}")
    sc.add_argument("--use-log", help="append-only use log")
    sc.add_argument("--single-use", action="store_true", help="exit 4 when the use log holds a started line")
    sc.add_argument("--list-fallback-ids", action="store_true")
    sc.add_argument("--pooled-oos", action="store_true", help="score the second-level CV decisions of --arm")
    sc.add_argument("--split-rows", help="real,syn: score the pooled-OOS decisions per kind")
    sc.add_argument("--arm", help="candidate arm with scores (for --pooled-oos / --split-rows)")
    ft = sub.add_parser("fit", parents=[common], help="session-grouped CV threshold fit")
    ft.add_argument("--arm", required=True)
    ft.add_argument("--decisions-out", help="write the pooled out-of-sample decisions here (mode 0600)")
    sub.add_parser("selftest", parents=[common], help="gate-validity checks on the table")
    return ap


def emit(result: dict, out_path: str | None) -> None:
    text = json.dumps(result, sort_keys=True, indent=1) + "\n"
    if out_path:
        write_private(out_path, text)
    sys.stdout.write(text)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        harness_sha = sha256_file(__file__)
        se_sha = sha256_file(_se.__file__)
        if args.pin_shadow_eval_sha and args.pin_shadow_eval_sha != se_sha:
            raise Refuse(3, "refusing: shadow_eval.py sha256 differs from the pin")
        if args.boot_b < 100:
            raise Refuse(2, "refusing: --boot-b must be at least 100")
        header, items, raws, table_sha = load_table(args.table)
        if args.make_noised_oracle is not None:
            if not args.out:
                raise Refuse(2, "refusing: --make-noised-oracle needs --out")
            recs = make_noised_oracle(items, raws, args.truth, parse_by_source(args.truth_by_source),
                                      args.voided, args.make_noised_oracle)
            head = [json.dumps({"_header": header}, sort_keys=True) + "\n"] if header else []
            write_private(args.out, "".join(head + [json.dumps(r, sort_keys=True) + "\n" for r in recs]))
            return 0
        code = 0
        if args.cmd == "score":
            if args.mode == "test" and not (args.single_use and args.use_log):
                raise Refuse(2, "refusing: --mode test needs --single-use and --use-log")
            if args.single_use and not args.use_log:
                raise Refuse(2, "refusing: --single-use needs --use-log")
            if (args.pooled_oos or args.split_rows) and not args.arm:
                raise Refuse(2, "refusing: --pooled-oos and --split-rows need --arm")
            cal = load_calibration(args.calibration) if args.calibration else _header_calibration(header)
            if args.use_log and args.single_use:
                start_use_log(args.use_log, harness_sha, table_sha)
            result = run_score(args, header, items, table_sha, harness_sha, se_sha, cal)
        elif args.cmd == "fit":
            result, code = run_fit(args, header, items, table_sha, harness_sha, se_sha)
        else:
            result, code = run_selftest(args, header, items, table_sha, harness_sha, se_sha, None)
        emit(result, args.out)
        return code
    except Refuse as e:
        sys.stderr.write(e.msg + "\n")
        return e.code


if __name__ == "__main__":
    sys.exit(main())
