#!/usr/bin/env python3
"""M1.8 round 3 (PREREG v2 amendment 2): nimble:9b on /v1/systemone as the tier classifier, TUNE only.

  collect   one pass over the 121 TUNE items with the PR-branch backend (llm_router.decision_classifier via
            local_classifier.classify_local, LLM_ROUTER_CLASSIFIER_BACKEND=systemone), uncontended. Stores only the
            response body (numbers and enum strings), latency and source: no prompt text, no raw text.
  analyze   offline, in two stages. Stage 1 loads E2-cal ONLY, fits tau_abs on it and WRITES the fit file. Stage 2 then
            loads E1-tune (labels first read here) and scores everything with tau_abs fixed. K1 uses m18_select.select().

The harness (eval_router.py, md5 0c755059...) and m18_select.py (md5 6fd18a3e...) are imported unchanged.
Refuses to run without PREREG-v2-amend2.md (mode 0444). Held-out items are never loaded.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
PP = Path(os.environ.get("PP") or sys.exit("set PP to the primary-plan directory (it holds eval/ and scripts/m18_select.py)"))
sys.path.insert(0, str(PP / "eval"))
sys.path.insert(0, str(PP / "scripts"))
import eval_router as ER  # noqa: E402
import m18_select as MS  # noqa: E402

AMEND2 = PP / "eval" / "PREREG-v2-amend2.md"
ERRATA1 = PP / "eval" / "PREREG-v2-amend2-errata1.md"  # re-pins the artifact actually pulled (MLX mxfp8), before any call
TAG = "nimble:9b"
MANIFEST = HOME / ".ollama/models/manifests/registry.ollama.ai/library/nimble/9b"
MANIFEST_SHA256 = "dfed4f707396966698374b7ffe21bd4586271b772551a02f0114e5c79fd24a21"
MODEL_ID = "13ae0a506102"
TAU_GRID = (0.0, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
BUDGET_MS = 2000.0
RAW_TIMEOUT_S = 30.0
FALLBACK_MAX = 0.05
OLLAMA = "http://localhost:11434"


# ------------------------------------------------------------------------ pure logic (unit-tested)
def row_verdict(row, dc, below=0.0):
    """The strict parse of one stored response through the backend's own parser."""
    if row.get("source") != "llm" or row.get("ms") is None:
        return None
    v = dc.parse_answer(row.get("body"), model=TAG, ms=row["ms"], below=below)
    return v if v.source == "llm" else None


def prep(raw_rows, dc):
    """-> [{id,set,ms,tier,conf,base_fb}]: tier/conf None and base_fb True when invalid or slower than the D-4 budget."""
    out = []
    for r in raw_rows:
        v = row_verdict(r, dc)
        slow = r["ms"] is None or r["ms"] > BUDGET_MS
        out.append({"id": r["id"], "set": r["set"], "ms": r["ms"], "tier": v.tier if v else None,
                    "conf": v.confidence if v else None, "invalid": v is None, "slow": slow,
                    "base_fb": v is None or slow})
    return out


def apply_tau(rows, tau, rules_eff):
    """Per-item prediction and fallback flag at abstain threshold `tau`. A fallback, an invalid answer and an abstain all take rules_eff."""
    preds, flags = {}, {}
    for r in rows:
        abst = (not r["base_fb"]) and r["conf"] < tau
        fb = r["base_fb"] or abst
        preds[r["id"]] = rules_eff[r["id"]] if fb else r["tier"]
        flags[r["id"]] = {"fallback": fb, "abstain": abst, "invalid": r["invalid"], "slow": r["slow"]}
    return preds, flags


def fit_tau(e2_rows, e2_truth, rules_eff, grid=TAU_GRID):
    """tau_abs by amendment 2 section 3.2: among taus with fallback rate <= 5% on E2-cal, the lowest C-lambda2; ties: fewer
    under-routes, then the lower tau. No feasible tau -> 0.0. Takes E2-cal rows and E2 truth ONLY."""
    if any(r["set"] != "e2" for r in e2_rows):
        raise ValueError("tau_abs is fitted on E2-cal rows only")
    table = []
    for tau in grid:
        preds, flags = apply_tau(e2_rows, tau, rules_eff)
        n = len(e2_rows)
        c2 = sum(ER.cost_eff(preds[r["id"]], e2_truth[r["id"]], 2.0) for r in e2_rows) / n
        under = sum(ER.RANK[ER.mapped(preds[r["id"]], "primary")] < ER.RANK[e2_truth[r["id"]]] for r in e2_rows)
        fb = sum(f["fallback"] for f in flags.values())
        table.append({"tau": tau, "fallback": fb, "fallback_rate": fb / n, "abstain": sum(f["abstain"] for f in flags.values()),
                      "c2": c2, "under": under, "feasible": fb / n <= FALLBACK_MAX})
    feas = [t for t in table if t["feasible"]]
    best = min(feas, key=lambda t: (t["c2"], t["under"], t["tau"])) if feas else None
    return (best["tau"] if best else 0.0), table


def pbin(p):
    """Decile bin of p_max: [0.3,0.4) ... [0.9,1.0]."""
    b = min(int(p * 10), 9)
    return f"{b / 10:.1f}"


def k1(fallback_rate, c2, c2_rules):
    return fallback_rate <= FALLBACK_MAX and c2 < c2_rules


# ------------------------------------------------------------------------ collect
def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()


def preflight(repo):
    for f in (AMEND2, ERRATA1):
        st = f.stat() if f.exists() else None
        if st is None or (st.st_mode & 0o777) != 0o444:
            sys.exit(f"REFUSED: {f} must exist with mode 0444 before any decision-model call")
    ver = json.loads(urllib.request.urlopen(OLLAMA + "/api/version", timeout=5).read())["version"]
    if tuple(int(x) for x in ver.split(".")[:2]) < (0, 35):
        sys.exit(f"REFUSED: Ollama {ver} < 0.35")
    msha = hashlib.sha256(MANIFEST.read_bytes()).hexdigest() if MANIFEST.exists() else None
    ids = [ln.split()[1] for ln in sh("ollama", "list").splitlines() if ln.split()[:1] == [TAG]]
    req = urllib.request.Request(OLLAMA + "/api/show", data=json.dumps({"model": TAG}).encode(),
                                 headers={"Content-Type": "application/json"})
    det = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("details", {})
    if msha != MANIFEST_SHA256 or ids != [MODEL_ID] or (det.get("format"), det.get("quantization_level"), det.get("runner")) != ("safetensors", "mxfp8", "mlx"):
        sys.exit(f"REFUSED: artifact check failed for {TAG}: manifest {msha}, id {ids}, details {det}")
    if sh("git", "-C", str(repo), "status", "--porcelain"):
        sys.exit("REFUSED: the backend worktree is not clean; the run must use a recorded commit")
    return ver, msha


def collect(args):
    repo = Path(args.repo).resolve()
    ver, msha = preflight(repo)
    os.environ["LLM_ROUTER_CLASSIFIER_BACKEND"] = "systemone"
    os.environ.pop("LLM_ROUTER_DECISION_ABSTAIN_BELOW", None)  # raw: the threshold is applied offline
    os.environ.pop("LLM_ROUTER_DECISION_MODEL", None)
    sys.path.insert(0, str(repo / "src"))
    import llm_router.local_classifier as lc
    ER.install_network_guard()
    src = Path(lc.__file__).resolve()
    if not src.is_relative_to(repo) or lc.backend() != "systemone" or lc._model() != TAG:
        sys.exit(f"import path / backend check failed: {src} backend={lc.backend()} model={lc._model()}")
    e2, e1 = MS.tune_items()
    items = e2 + e1
    captured, orig_post = {}, lc._post

    def cap(model, assembled, timeout):
        txt = orig_post(model, assembled, timeout)
        captured[hashlib.md5(str(assembled).encode()).hexdigest()] = txt
        return txt

    lc._post = cap
    resident = [m["name"] for m in ER.resident_models(OLLAMA)]
    foreign = [n for n in resident if n != TAG]
    if foreign or ER.free_pct() < 20:
        sys.exit(f"REFUSED: foreign model resident {foreign} or free {ER.free_pct()}% < 20%")
    t0w = time.time()
    wu = lc.classify_local(lc.Assembled("The developer is working in a small Python repository.", "run the tests again"),
                           timeout_s=120.0)
    cold_ms = round((time.time() - t0w) * 1000.0, 1)
    if not wu.ok:
        sys.exit(f"ABORT: warm-up call failed: source={wu.source}")
    captured.clear()
    rows, t0 = [], time.time()
    min_free, max_load = 100, 0.0
    for it in items:
        free = ER.free_pct()
        min_free = min(min_free, free)
        if free < 20:
            sys.exit(f"ABORT: free memory {free}% < 20% at item {it['id']}; partial run discarded")
        foreign = [m["name"] for m in ER.resident_models(OLLAMA) if m["name"] != TAG]
        if foreign:
            sys.exit(f"ABORT: foreign model resident {foreign} at item {it['id']} (single slot); partial run discarded")
        ctx, pr = ER.assemble(it)
        a = lc.Assembled(ctx, pr)
        max_load = max(max_load, os.getloadavg()[0])
        v = lc.classify_local(a, timeout_s=RAW_TIMEOUT_S)
        txt = captured.pop(hashlib.md5(str(a).encode()).hexdigest(), None)
        body = None
        if txt:
            try:
                body = json.loads(txt)
            except ValueError:
                body = None
        rows.append({"id": it["id"], "set": it["set"], "ms": v.ms, "source": v.source, "body": body})
        if v.source == "timeout":
            time.sleep(0.5)
    lc._post = orig_post
    ps = [{k: m.get(k) for k in ("name", "size_vram", "context_length")} for m in ER.resident_models(OLLAMA)]
    import llm_router.decision_classifier as dc
    doc = {"meta": {"model": TAG, "ts": time.strftime("%FT%TZ", time.gmtime()), "ollama_version": ver, "manifest_sha256": msha, "errata1_sha256": hashlib.sha256(ERRATA1.read_bytes()).hexdigest(),
                    "amend2_sha256": hashlib.sha256(AMEND2.read_bytes()).hexdigest(),
                    "repo_head": sh("git", "-C", str(repo), "rev-parse", "HEAD"),
                    "classifier_md5": hashlib.md5(src.read_bytes()).hexdigest(),
                    "decision_md5": hashlib.md5(Path(dc.__file__).read_bytes()).hexdigest(),
                    "harness_md5": hashlib.md5(Path(ER.__file__).read_bytes()).hexdigest(),
                    "select_md5": hashlib.md5(Path(MS.__file__).read_bytes()).hexdigest(),
                    "cold_load_ms": cold_ms, "ollama_ps_after": ps, "n": len(rows), "min_free_pct": min_free,
                    "max_loadavg1": round(max_load, 2), "wall_s": round(time.time() - t0, 1),
                    "network_attempts": ER.NETWORK_ATTEMPTS, "heldout_items_loaded": 0},
           "rows": rows}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1))
    print(f"{TAG}: n={len(rows)} invalid={sum(r['source'] != 'llm' for r in rows)} cold={cold_ms} ms "
          f"min_free={min_free}% max_load1={max_load:.1f} wrote {out}")


# ------------------------------------------------------------------------ analyze
def analyze(args):
    repo = Path(args.repo).resolve()
    raw = json.load(open(args.raw))
    rules = ER.Rules(str(repo / "src"), "stored")  # also puts the repo's src first on sys.path
    import llm_router.decision_classifier as dc
    assert Path(dc.__file__).resolve().is_relative_to(repo)
    rows_all = prep(raw["rows"], dc)
    # ---- stage 1: E2-cal only
    e2 = ER.select_split(ER.load_e2(), "tune")
    assert len(e2) == 91 and all(i["set"] == "e2" and i["split"] == "tune" for i in e2)
    rout2 = {i["id"]: rules.classify(i) for i in e2}
    b2 = ER.make_baselines(e2, rout2)
    truth2 = {i["id"]: i["truth"] for i in e2}
    rows2 = [r for r in rows_all if r["set"] == "e2"]
    assert len(rows2) == 91 and {r["id"] for r in rows2} == set(truth2)
    rs2 = ER.score(e2, b2["rules_eff"], b2)
    if abs(rs2["c_lambda2"] - 9.58) > 0.005 or rs2["primary"]["under"]["k"] != 47:
        sys.exit(f"STOP: rules_eff does not reproduce (C2 {rs2['c_lambda2']:.3f}, under {rs2['primary']['under']['k']}/91)")
    tau, table = fit_tau(rows2, truth2, b2["rules_eff"])
    fit_path = Path(args.fit_out)
    fit_path.write_text(json.dumps({"ts": time.strftime("%FT%TZ", time.gmtime()), "tau_abs": tau, "grid": table,
                                    "n_items": len(rows2), "e1_labels_loaded": False,
                                    "raw_file": str(args.raw), "amend2_sha256": hashlib.sha256(AMEND2.read_bytes()).hexdigest()}, indent=1))
    print(f"stage 1: tau_abs={tau} written to {fit_path} (E1-tune labels not yet loaded)")
    # ---- stage 2: E1-tune labels are first read below
    e1 = ER.select_split(ER.load_e1(ER.DEFAULT_KPI), "tune")
    assert len(e1) == 30 and all(i["split"] == "tune" for i in e1)
    rout1 = {i["id"]: rules.classify(i) for i in e1}
    b1 = ER.make_baselines(e1, rout1)
    truth = {**truth2, **{i["id"]: i["truth"] for i in e1}}
    reff = {**b2["rules_eff"], **b1["rules_eff"]}
    rows1 = [r for r in rows_all if r["set"] == "e1"]
    assert len(rows1) == 30

    def block(budget):
        rr = [dict(r, base_fb=(r["invalid"] or (budget is not None and r["slow"]))) for r in rows2]
        preds, flags = apply_tau(rr, tau, reff)
        sc = ER.score(e2, preds, b2)
        lat = [r["ms"] for r in rows2 if r["ms"] is not None]
        k = lambda key: sum(f[key] for f in flags.values())  # noqa: E731
        return preds, flags, {
            "n": 91, "tau_abs": tau, "budget_ms": budget,
            "fallback": ER.rate(k("fallback"), 91), "fallback_rate": k("fallback") / 91,
            "fallback_split": {"slow": sum(f["slow"] for f in flags.values()) if budget else None,
                               "invalid": k("invalid"), "abstain": k("abstain")},
            "c2": sc["c_lambda2"], "c1": sc["c_lambda1"], "craw": sc["craw"], "under": sc["primary"]["under"],
            "exact": sc["primary"]["exact"], "HP": sc["HP"], "HR": sc["HR"], "pareto": sc["pareto"],
            "c2_rules": rs2["c_lambda2"], "under_rules": rs2["primary"]["under"],
            "boot_vs_rules": sc["boot_c_lambda2_vs"].get("rules_eff"),
            "p50": ER.pct(lat, 0.5), "p95": ER.pct(lat, 0.95)}

    preds, flags, main = block(BUDGET_MS)
    _, _, diag = block(None)  # budget-free quality: DIAGNOSTIC ONLY, never eligible
    p1, f1 = apply_tau(rows1, tau, reff)  # E1-tune predictions use the same, already fixed, tau
    sc1 = ER.score(e1, p1, b1)
    lat1 = [r["ms"] for r in rows1 if r["ms"] is not None]
    e1_tune = {"n": 30, "under": sc1["primary"]["under"], "exact": sc1["primary"]["exact"], "c2": sc1["c_lambda2"],
               "under_rules": ER.score(e1, b1["rules_eff"], b1)["primary"]["under"], "p95": ER.pct(lat1, 0.95),
               "fallback": ER.rate(sum(f["fallback"] for f in f1.values()), 30)}
    # warm latency over all 121 calls
    lat_all = [r["ms"] for r in rows_all if r["ms"] is not None]
    # haiku fit on p_max (tau_h): rows need fallback/task_type/margin keys
    hf_rows = [{"id": r["id"], "fallback": flags[r["id"]]["fallback"], "task_type": None, "margin": r["conf"]} for r in rows2]
    haiku = MS.haiku_fit(hf_rows, truth, preds, key_margin=lambda r: r["margin"])
    valid2 = [r for r in rows2 if not r["invalid"]]
    pairs = sorted({(r["tier"], pbin(r["conf"])) for r in valid2})
    calib = {}
    for r in valid2:
        c = calib.setdefault(pbin(r["conf"]), {"n": 0, "correct": 0})
        c["n"] += 1
        c["correct"] += r["tier"] == truth[r["id"]]
    dist = {"pred_valid": {t: sum(r["tier"] == t for r in valid2) for t in ("haiku", "sonnet", "opus")},
            "truth_e2": {t: sum(truth2[i] == t for i in truth2) for t in ("haiku", "sonnet", "opus")}}
    chosen, elig = MS.select([{"name": TAG, "fallback_rate": main["fallback_rate"], "c2": main["c2"],
                               "c2_rules": main["c2_rules"], "e1_under": e1_tune["under"]["k"], "p95": main["p95"]}])
    r2 = None
    p2 = PP / "eval/results/tune_round2_20261007T151100.json"
    if p2.exists():
        c2d = json.load(open(p2))["candidates"][0]
        r2 = {"name": c2d["name"], **{k: c2d["bases"]["all91"][k] for k in ("c2", "under", "exact", "p50", "p95", "fallback_rate", "craw")}}
    doc = {"meta": {"ts": time.strftime("%FT%TZ", time.gmtime()), "round": 3, "raw_file": str(args.raw),
                    "raw_meta": {k: v for k, v in raw["meta"].items()}, "tau_fit_file": str(fit_path),
                    "script_md5": hashlib.md5(Path(__file__).read_bytes()).hexdigest(),
                    "harness_md5": hashlib.md5(Path(ER.__file__).read_bytes()).hexdigest(),
                    "select_md5": hashlib.md5(Path(MS.__file__).read_bytes()).hexdigest(),
                    "prereg_sha256": ER.prereg_sha(), "amend2_sha256": hashlib.sha256(AMEND2.read_bytes()).hexdigest(),
                    "heldout_items_classified": 0, "heldout_items_scored": 0},
           "tau_abs": tau, "tau_fit_table": table,
           "K1": {"eligible": bool(elig), "chosen": chosen, "fallback_rate_e2cal": main["fallback_rate"], "c2": main["c2"],
                  "rules_eff_c2": main["c2_rules"], "rule": "fallback<=5% at 2000 ms AND C-lambda2 < rules_eff on E2-cal"},
           "e2cal": main, "diagnostic_budget_free": diag, "e1_tune": e1_tune,
           "latency_all121": {"n": len(lat_all), "p50": ER.pct(lat_all, 0.5), "p95": ER.pct(lat_all, 0.95),
                              "cold_load_ms": raw["meta"]["cold_load_ms"],
                              "over_2000ms": sum(x > BUDGET_MS for x in lat_all)},
           "haiku_fit": haiku, "distinct_choice_pmax_bin_pairs": [list(p) for p in pairs], "calibration_by_pmax_bin": calib,
           "tier_distribution": dist, "round2_v7": r2,
           "rows": [{"id": r["id"], "set": r["set"], "truth": truth[r["id"]], "tier": r["tier"], "conf": r["conf"],
                     "pred": (preds if r["set"] == "e2" else p1)[r["id"]], "ms": r["ms"], **(flags if r["set"] == "e2" else f1)[r["id"]]}
                    for r in rows_all]}
    Path(args.out).write_text(json.dumps(doc, indent=1, default=str))
    m = main
    print(f"K1 eligible={bool(elig)} | tau_abs={tau} | fallback {m['fallback']['k']}/91 {m['fallback_split']} | "
          f"C2 {m['c2']:.3f} vs rules {m['c2_rules']:.3f} | under {m['under']['k']}/91 vs {m['under_rules']['k']} | exact {m['exact']['k']}/91 | "
          f"craw {m['craw']:.2f} | p50 {m['p50']} p95 {m['p95']} | HP {m['HP']['k']}/{m['HP']['n']} | pairs {len(pairs)}")
    print(f"wrote {args.out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--repo", required=True)
    c.add_argument("--out", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("--raw", required=True)
    a.add_argument("--repo", required=True)
    a.add_argument("--fit-out", required=True)
    a.add_argument("--out", required=True)
    args = ap.parse_args()
    {"collect": collect, "analyze": analyze}[args.cmd](args)


if __name__ == "__main__":
    main()
