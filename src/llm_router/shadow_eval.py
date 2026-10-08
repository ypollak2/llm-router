"""Live-shadow scoring of the LLM classifier against the rules (v16 P1.7, owner decision 2026-10-08).

The proxy's classifier shadow (``proxy/llm_shadow``) writes one record per turn-first call:
the rules' tier, the LLM's verdict and the tier the client REALLY requested on that call.
This module scores those records with the PREREG v2 metrics, so the adoption rule
"adopt only if M1-12 passes on >= 100 real turns" can be read off live data:

* **Arms.** ``rules_eff``: the rules' proposed tier, with Haiku lifted to Sonnet unless
  Haiku was really served on that call (today's served behaviour, as in the eval
  harness). ``llm``: the verdict's tier (``local`` counts as Haiku), or ``rules_eff``
  when the verdict is unusable (timeout, parse error, cold model, abstain): the M1.9
  "fallback rules_eff" configuration. ``n_fallback`` counts those.
* **Requested tier.** The record's own ``requested`` tier (from the proxy row's
  ``requested_model``), so every scored turn is joined; a record without one (an
  unknown model id) takes v2's imputation: the session's most common requested tier,
  else Sonnet. ``n_joined`` counts the real ones.
* **Truth** comes from a labels file (JSONL ``{"text_sha", "session_id"?, "truth"}``),
  because a live turn has no ground truth of its own. Records without a label are not
  scored; ``n_labeled`` says how many were. With no labels every truth-based metric is
  ``None`` and M1-12 is ``not informative``.
* **Metrics** (PREREGISTRATION.v2 sections 1.2-1.4): cost c(haiku) 1.0, c(sonnet) 2.7,
  c(opus) 6.15; ``C-lambda2`` = mean of c(pred) + 2 c(truth) when pred < truth;
  ``C-lambda2-clamp`` (M1-12) scores min(pred, requested) instead of pred; under-route;
  Haiku precision HP = #{pred haiku and truth haiku} / #{pred haiku} and recall HR;
  a session-clustered bootstrap (B 2,000, seed 20261003) of llm minus rules_eff
  C-lambda2-clamp.
* **M1-12 verdict.** ``pass`` when C-lambda2-clamp(llm) <= C-lambda2-clamp(rules_eff)
  on >= :data:`MIN_N` labeled turns and the largest session holds <= 60% of them;
  ``fail`` when the inequality does not hold on such a sample; else ``not informative``.
  The verdict is cost only: R1 adoption also needs under-route(llm) <= under-route(rules_eff),
  which the caller reads from ``arms``. ``agree`` counts fallback turns (llm = rules_eff) as
  agreement; ``n_fallback`` says how many there are.

Pure functions over records; nothing here reads prompt text (records hold none).
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

COST = {"haiku": 1.0, "sonnet": 2.7, "opus": 6.15}
RANK = {"haiku": 1, "sonnet": 2, "opus": 3}
LAMBDA = 2.0
MIN_N = 100            # owner, 2026-10-08: M1-12 on >= 100 real turns
MAX_SESSION_SHARE = 0.60  # statistics rule 4
BOOT_B = 2000
BOOT_SEED = 20261003
OK_SOURCES = ("llm", "cache")


def tier_name(value: object) -> str | None:
    """``haiku`` / ``sonnet`` / ``opus`` from a tier name or a model id (``local`` is Haiku);
    ``None`` for anything else. The eval harness's join reads model ids the same way."""
    if not isinstance(value, str):
        return None
    v = value.lower()
    if v == "local":
        return "haiku"
    for t in ("haiku", "sonnet", "opus"):
        if t in v:
            return t
    return None


def rules_eff(rec: dict) -> str | None:
    proposed = tier_name((rec.get("rules") or {}).get("tier"))
    if proposed == "haiku" and tier_name(rec.get("tier_live")) != "haiku":
        return "sonnet"
    return proposed


def llm_pick(rec: dict) -> tuple[str | None, bool]:
    """(tier, fell_back)."""
    if rec.get("source") in OK_SOURCES:
        t = tier_name((rec.get("llm") or {}).get("tier"))
        if t is not None:
            return t, False
    return rules_eff(rec), True


def requested_of(rec: dict) -> str | None:
    return tier_name(rec.get("requested")) or tier_name(rec.get("requested_tier"))


def load_labels(path: str | Path | None) -> dict[tuple[str, str], str]:
    """``{(session_id or "", text_sha): truth}``. A missing or unreadable file is no labels;
    a line that is not an object with a text_sha and a known truth is skipped."""
    out: dict[tuple[str, str], str] = {}
    if not path:
        return out
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or not isinstance(r.get("text_sha"), str):
            continue
        truth = tier_name(r.get("truth"))
        if truth:
            out[(r.get("session_id") or "", r["text_sha"])] = truth
    return out


def _truth(labels: dict, rec: dict) -> str | None:
    sha = rec.get("text_sha")
    return labels.get((rec.get("session_id") or "", sha)) or labels.get(("", sha))


def cost_eff(pred: str, truth: str, lam: float = LAMBDA) -> float:
    return COST[pred] + (lam * COST[truth] if RANK[pred] < RANK[truth] else 0.0)


def clamp(pred: str, requested: str) -> str:
    return pred if RANK[pred] <= RANK[requested] else requested


def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": wilson(k, n)}


def boot_diff(items: list[dict], fa, fb, B: int = BOOT_B, seed: int = BOOT_SEED) -> dict:
    """Session-clustered bootstrap of mean(fa - fb) (the eval harness's ``boot_diff``)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by[it["session_id"]].append(it)
    keys = sorted(by)
    if not keys:
        return {"diff": None, "ci95": [None, None], "clusters": 0}
    rng = random.Random(seed)

    def d(sample: list[str]) -> float:
        tot = [it for s in sample for it in by[s]]
        return sum(fa(it) - fb(it) for it in tot) / len(tot)

    point = d(keys)
    bs = sorted(d([rng.choice(keys) for _ in keys]) for _ in range(B))
    return {"diff": round(point, 4), "ci95": [round(bs[int(0.025 * B)], 4), round(bs[int(0.975 * B) - 1], 4)],
            "clusters": len(keys)}


def score(records: list[dict], labels: dict[tuple[str, str], str] | None = None) -> dict:
    """Score answered-or-failed shadow verdict records (``kind == classifier_shadow``) of ONE
    classifier model. Records without a rules tier cannot be compared and are skipped."""
    labels = labels or {}
    turns: list[dict] = []
    by_sess_req: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        req = requested_of(r)
        if req:
            by_sess_req[r.get("session_id") or ""][req] += 1
    seen: set[tuple] = set()
    for r in records:
        key = (r.get("session_id") or "", r.get("text_sha"))
        rules = rules_eff(r)
        if rules is None or key in seen:   # one score per turn: the first record of a (session, text)
            continue
        seen.add(key)
        llm, fell_back = llm_pick(r)
        real = requested_of(r)
        sess = r.get("session_id") or ""
        imputed = by_sess_req[sess].most_common(1)[0][0] if by_sess_req.get(sess) else "sonnet"
        turns.append({"session_id": sess, "llm": llm, "rules_eff": rules, "fallback": fell_back,
                      "requested": real or imputed, "joined": real is not None,
                      "capped": r.get("assemble_capped") is True, "truth": _truth(labels, r)})
    n = len(turns)
    out: dict = {
        "n_turns": n,
        "n_sessions": len({t["session_id"] for t in turns}),
        "n_joined": sum(t["joined"] for t in turns),
        "n_fallback": sum(t["fallback"] for t in turns),
        "n_capped": sum(t["capped"] for t in turns),
        "agree": sum(t["llm"] == t["rules_eff"] for t in turns),
        "clamp_binds_llm": sum(RANK[t["llm"]] > RANK[t["requested"]] for t in turns),
        "clamp_binds_rules": sum(RANK[t["rules_eff"]] > RANK[t["requested"]] for t in turns),
    }
    for arm in ("llm", "rules_eff"):
        out[f"craw_{arm}"] = round(sum(COST[t[arm]] for t in turns) / n, 4) if n else None
        out[f"craw_clamp_{arm}"] = (round(sum(COST[clamp(t[arm], t["requested"])] for t in turns) / n, 4)
                                    if n else None)
    lab = [t for t in turns if t["truth"]]
    m = len(lab)
    sess = Counter(t["session_id"] for t in lab)
    share = round(sess.most_common(1)[0][1] / m, 4) if m else None
    out.update({"n_labeled": m, "n_labeled_sessions": len(sess), "largest_session_share": share,
                "top3_session_counts": [c for _, c in sess.most_common(3)]})   # statistics rule 4
    arms: dict = {}
    for arm in ("llm", "rules_eff"):
        if not m:
            arms[arm] = None
            continue
        picks_h = [t for t in lab if t[arm] == "haiku"]
        hit = sum(t["truth"] == "haiku" for t in picks_h)
        arms[arm] = {
            "c_lambda2": round(sum(cost_eff(t[arm], t["truth"]) for t in lab) / m, 4),
            "c_lambda2_clamp": round(sum(cost_eff(clamp(t[arm], t["requested"]), t["truth"]) for t in lab) / m, 4),
            "under": _rate(sum(RANK[t[arm]] < RANK[t["truth"]] for t in lab), m),
            "exact": _rate(sum(t[arm] == t["truth"] for t in lab), m),
            "HP": _rate(hit, len(picks_h)),
            "HR": _rate(hit, sum(t["truth"] == "haiku" for t in lab)),
        }
    out["arms"] = arms
    if m:
        out["boot_c_lambda2_clamp_llm_minus_rules"] = boot_diff(
            lab, lambda t: cost_eff(clamp(t["llm"], t["requested"]), t["truth"]),
            lambda t: cost_eff(clamp(t["rules_eff"], t["requested"]), t["truth"]))
    else:
        out["boot_c_lambda2_clamp_llm_minus_rules"] = None
    if m < MIN_N:
        verdict, why = "not informative", f"{m} labeled turns < {MIN_N}"
    elif share is not None and share > MAX_SESSION_SHARE:
        verdict, why = "not informative", f"largest session holds {share:.0%} > {MAX_SESSION_SHARE:.0%}"
    elif arms["llm"]["c_lambda2_clamp"] <= arms["rules_eff"]["c_lambda2_clamp"]:
        verdict, why = "pass", "C-lambda2-clamp(llm) <= C-lambda2-clamp(rules_eff)"
    else:
        verdict, why = "fail", "C-lambda2-clamp(llm) > C-lambda2-clamp(rules_eff)"
    out["M1_12"] = {"verdict": verdict, "why": why, "min_n": MIN_N}
    return out


def score_by_model(records: list[dict], labels: dict | None = None) -> dict[str, dict]:
    """One :func:`score` block per classifier model in the records (a backend switch inside the
    window must never mix two classifiers into one number)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[str(r.get("model") or "unknown")].append(r)
    return {model: score(recs, labels) for model, recs in sorted(groups.items())}
