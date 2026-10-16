#!/usr/bin/env python3
"""CLS-v16 resemblance tool (build B-L): CLS-SYN-GENERATOR-SPEC section 8.1 text-only checks.

  cls_resemble.py run --set {dev_syn,test_syn} --real-items F --syn-items F --cls-root C --spec-version N --out F

The ONLY tool that reads TEST-SYN text before release (inside the TEST-SYN agent's own step, or with a
release token). OUTPUT IS pass/fail and AUROC numbers ONLY: no features, weights, top terms, error cases,
item ids or text, in stdout, stderr or the output file. There is no flag that dumps features.
Exit codes: 0 pass, 14 fail. Heavy libraries (numpy, scikit-learn, sentence_transformers) are imported
lazily inside the default scorers; tests inject scorers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cls_pins as P  # noqa: E402
import cls_send_lib as S  # noqa: E402

VERIFY_PINS = P.verify_at_start

BOOTSTRAP_B = 2000
GATE_POINT = 0.75
GATE_CI_UPPER = 0.80
SHARE_TOL = 10.0
MEDIAN_TOL = 0.30
BAND_MISS_MAX = 0.15
FOLDS = 5
FOLD_PREFIX = "clsdev:fold:20261017:"
BANDS = ["L1", "L2", "L3", "L4"]


# ---------------------------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------------------------

def auroc(scores, labels) -> float:
    """Rank-based AUROC with tied scores averaged. labels: 1 = synthetic, 0 = real."""
    pairs = sorted(zip(scores, labels))
    n1 = sum(1 for _s, y in pairs if y == 1)
    n0 = len(pairs) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + j + 1) / 2.0
        rank_sum += avg * sum(1 for k in range(i, j) if pairs[k][1] == 1)
        i = j
    return (rank_sum - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def fold_assignment(groups, k: int = FOLDS) -> dict:
    """Groups sorted by sha256("clsdev:fold:20261017:" + group), dealt round-robin."""
    uniq = sorted(set(groups), key=lambda g: hashlib.sha256((FOLD_PREFIX + g).encode()).hexdigest())
    return {g: i % k for i, g in enumerate(uniq)}


def group_bootstrap_ci(scores, labels, groups, *, B: int | None = None, seed: int = S.SEED) -> tuple[float, float]:
    """95 percent CI of the pooled AUROC, resampling GROUPS with replacement."""
    B = B or BOOTSTRAP_B
    by = {}
    for s, y, g in zip(scores, labels, groups):
        by.setdefault(g, []).append((s, y))
    keys = sorted(by)
    rng = random.Random(seed)
    vals = []
    for _ in range(B):
        sc, yy = [], []
        for _k in range(len(keys)):
            for s, y in by[keys[rng.randrange(len(keys))]]:
                sc.append(s)
                yy.append(y)
        v = auroc(sc, yy)
        if not math.isnan(v):
            vals.append(v)
    if not vals:
        return float("nan"), float("nan")
    vals.sort()
    return vals[int(0.025 * (len(vals) - 1))], vals[int(math.ceil(0.975 * (len(vals) - 1)))]


def pure_python_lr(X, y, w, *, iters: int = 150, lr: float = 0.5, l2: float = 0.01):
    """A tiny pure-Python weighted logistic regression, used only through injection (tests)."""
    d = len(X[0])
    wt, b = [0.0] * d, 0.0
    tot = sum(w) or 1.0
    for _ in range(iters):
        gw, gb = [0.0] * d, 0.0
        for xi, yi, wi in zip(X, y, w):
            z = b + sum(c * v for c, v in zip(wt, xi))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            e = (p - yi) * wi / tot
            gb += e
            for j in range(d):
                gw[j] += e * xi[j]
        b -= lr * gb
        wt = [c - lr * (g + l2 * c) for c, g in zip(wt, gw)]

    def predict(Xte):
        out = []
        for xi in Xte:
            z = b + sum(c * v for c, v in zip(wt, xi))
            out.append(1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))))
        return out
    return predict


def make_scorer(featurize, trainer):
    """scorer(train_texts, y, w, test_texts) -> scores. featurize(train, test) -> (Xtr, Xte)."""
    def scorer(tr_texts, y, w, te_texts):
        xtr, xte = featurize(tr_texts, te_texts)
        return list(trainer(xtr, y, w)(xte))
    return scorer


def _sk_lr_trainer(X, y, w):
    from sklearn.linear_model import LogisticRegression
    clf = LogisticRegression(C=1.0, max_iter=1000).fit(X, y, sample_weight=w)
    return lambda Xte: clf.predict_proba(Xte)[:, 1]


def _sk_tfidf_featurize(tr, te):
    from sklearn.feature_extraction.text import TfidfVectorizer
    vec = TfidfVectorizer(analyzer="char", ngram_range=(2, 5), sublinear_tf=True)
    return vec.fit_transform(tr), vec.transform(te)


def make_bge_embedder(snapshot_dir):
    """bge-small-en-v1.5 from the pinned local snapshot, offline, L2-normalised. Never downloads."""
    def embed(texts):
        os.environ["HF_HUB_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(str(snapshot_dir), device="cpu")
        return model.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
    return embed


def default_scorers(bge_snapshot_dir=None) -> dict:
    a1 = make_scorer(_sk_tfidf_featurize, _sk_lr_trainer)
    embed = make_bge_embedder(bge_snapshot_dir) if bge_snapshot_dir else None

    def a2(tr, y, w, te):
        if embed is None:
            raise S.CLSExit(S.EXIT_REFUSE, "a2 needs the pinned bge-small snapshot directory")
        return make_scorer(lambda a, b: (embed(a), embed(b)), _sk_lr_trainer)(tr, y, w, te)
    return {"a1": a1, "a2": a2}


# ---------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------

def gate_pass(point: float, ci_upper: float) -> bool:
    return point <= GATE_POINT and ci_upper <= GATE_CI_UPPER


def balanced_weights(y):
    n1 = sum(y)
    n0 = len(y) - n1
    n = len(y)
    return [n / (2.0 * n1) if v == 1 else n / (2.0 * n0) for v in y]


def oof_scores(texts, y, groups, scorer, k: int = FOLDS) -> list[float]:
    fold = fold_assignment(groups, k)
    f_of = [fold[g] for g in groups]
    out = [0.0] * len(texts)
    for f in range(k):
        tr = [i for i, v in enumerate(f_of) if v != f]
        te = [i for i, v in enumerate(f_of) if v == f]
        if not te or len({y[i] for i in tr}) < 2:
            continue
        ytr = [y[i] for i in tr]
        sc = scorer([texts[i] for i in tr], ytr, balanced_weights(ytr), [texts[i] for i in te])
        for i, s in zip(te, sc):
            out[i] = float(s)
    return out


def band_of(words: int) -> str:
    return "L1" if words <= 12 else "L2" if words <= 50 else "L3" if words <= 150 else "L4"


def shares(texts) -> dict:
    cnt = {b: 0 for b in BANDS}
    for t in texts:
        cnt[band_of(len(t.split()))] += 1
    n = max(1, len(texts))
    return {b: round(100.0 * cnt[b] / n, 1) for b in BANDS}


def median_words(texts) -> float:
    v = sorted(len(t.split()) for t in texts)
    if not v:
        return 0.0
    m = len(v) // 2
    return float(v[m]) if len(v) % 2 else (v[m - 1] + v[m]) / 2.0


def compute(real_rows, syn_rows, *, scorers: dict, gen_report: dict | None, B: int | None = None) -> dict:
    """real_rows/syn_rows: lists of (text, group). Returns the numbers-only result dict."""
    texts = [t for t, _g in real_rows] + [t for t, _g in syn_rows]
    y = [0] * len(real_rows) + [1] * len(syn_rows)
    groups = ["real:" + g for _t, g in real_rows] + ["syn:" + g for _t, g in syn_rows]
    res = {}
    for name in ("a1", "a2"):
        sc = oof_scores(texts, y, groups, scorers[name])
        point = auroc(sc, y)
        lo, hi = group_bootstrap_ci(sc, y, groups, B=B)
        res[name] = {"auroc": round(point, 4), "ci": [round(lo, 4), round(hi, 4)],
                     "point_ok": point <= GATE_POINT, "ci_upper_ok": gate_pass(0.0, hi)}
        res[name]["pass"] = gate_pass(point, hi)
    res["gate"] = {"point_le_0.75": res["a1"]["point_ok"] and res["a2"]["point_ok"],
                   "ci_upper_le_0.80": res["a1"]["ci_upper_ok"] and res["a2"]["ci_upper_ok"],
                   "pass": res["a1"]["pass"] and res["a2"]["pass"]}
    rs, ss = shares([t for t, _ in real_rows]), shares([t for t, _ in syn_rows])
    res["b"] = {"real_shares": rs, "syn_shares": ss, "pass": all(abs(rs[b] - ss[b]) <= SHARE_TOL for b in BANDS)}
    rm, sm = median_words([t for t, _ in real_rows]), median_words([t for t, _ in syn_rows])
    res["c"] = {"real_median_words": rm, "syn_median_words": sm,
                "within_30_percent": bool(rm) and abs(sm - rm) <= MEDIAN_TOL * rm, "gating": False}
    if gen_report is None:
        res["d"] = {"pass": False, "note": "no generator report supplied"}
    else:
        miss = float(gen_report.get("band_miss_rate", 1.0))
        res["d"] = {"quota_ok": bool(gen_report.get("quota_ok")), "band_miss_rate": miss,
                    "pass": bool(gen_report.get("quota_ok")) and miss <= BAND_MISS_MAX}
    res["pass"] = bool(res["gate"]["pass"] and res["b"]["pass"] and res["d"]["pass"])
    return res


def amendment_view(result: dict) -> dict:
    """What a human or agent amending the spec may see: pass/fail and the DEV-SYN AUROC numbers only.
    TEST-SYN results never appear in it."""
    if result.get("set") != "dev_syn":
        raise S.CLSExit(S.EXIT_CUSTODY, "only DEV-SYN numbers may enter the amendment view")
    return {"set": "dev_syn", "spec_version": result.get("spec_version"), "pass": result["pass"],
            "a1": {"auroc": result["a1"]["auroc"], "ci": result["a1"]["ci"]},
            "a2": {"auroc": result["a2"]["auroc"], "ci": result["a2"]["ci"]}}


def _rows_from_items(text: str):
    rows = []
    for line in text.splitlines():
        if line.strip():
            rec = json.loads(line)
            rows.append((rec["text"], str(rec.get("group") or rec["item_id"])))
    return rows


def resemble_in_step(a, items: list[dict], report: dict) -> dict:
    """The TEST-SYN agent's own step: the generated items are in memory (the file is locked right after);
    the real DEV text is read through the custody guard. Returns the numbers-only result."""
    real = _rows_from_items(S.read_guarded_text(a.real_items, a.cls_root))
    syn = [(i["text"], i["group"]) for i in items]
    bge = None
    doc = json.loads(Path(a.pins_static).read_text(encoding="utf-8"))
    if doc.get("params", {}).get("bge_snapshot_dir"):
        bge = doc["params"]["bge_snapshot_dir"]
    res = compute(real, syn, scorers=default_scorers(bge), gen_report=report)
    res.update({"set": "test_syn", "spec_version": report["spec_version"]})
    return res


def build_parser():
    ap = argparse.ArgumentParser(prog="cls_resemble")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--set", choices=("dev_syn", "test_syn"), required=True)
    p.add_argument("--real-items", required=True)
    p.add_argument("--syn-items", required=True)
    p.add_argument("--cls-root", required=True)
    p.add_argument("--spec-version", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--gen-report")
    p.add_argument("--release-token")
    p.add_argument("--amendment-view", action="store_true")
    p.add_argument("--pins-static")
    p.add_argument("--pins-run")
    return ap


def main(argv=None, *, scorers: dict | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        VERIFY_PINS(a.pins_static, a.pins_run, check_ollama=False)
        real = _rows_from_items(S.read_guarded_text(a.real_items, a.cls_root))
        if a.set == "test_syn":
            syn_text = S.read_guarded_text(a.syn_items, a.cls_root, release_token=a.release_token, allow_test_syn=True)
        else:
            syn_text = S.read_guarded_text(a.syn_items, a.cls_root)
        syn = _rows_from_items(syn_text)
        gen_report = json.loads(Path(a.gen_report).read_text(encoding="utf-8")) if a.gen_report else None
        if scorers is None:
            bge = None
            if a.pins_static and Path(a.pins_static).exists():
                bge = json.loads(Path(a.pins_static).read_text(encoding="utf-8")).get("params", {}).get("bge_snapshot_dir")
            scorers = default_scorers(bge)
        res = compute(real, syn, scorers=scorers, gen_report=gen_report)
        res.update({"set": a.set, "spec_version": a.spec_version})
        out = amendment_view(res) if a.amendment_view else res
        S.write_private(a.out, json.dumps(out, sort_keys=True, indent=1) + "\n")
        print(json.dumps(out, sort_keys=True))
        return S.EXIT_OK if res["pass"] else S.EXIT_RESEMBLE_FAIL
    except S.CLSExit as e:
        print("refused: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
