#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build and evaluate the complexity_knn artifact (Phase 2.1, "Compass" analogue).

    # evaluate (numpy needed for the similarity matrices; not a runtime dependency)
    uv run --with numpy python scripts/build_complexity_knn.py eval \\
        --data-dir ~/Projects/llm-router/data --cheap <cheap-id> --strong <strong-id> \\
        --report /tmp/complexity_knn_eval.json

    # build the artifact into the state dir (~/.llm-router/complexity_knn.json);
    # --threshold is required: use the "threshold" the eval report fitted
    python scripts/build_complexity_knn.py build \\
        --data-dir ~/Projects/llm-router/data --cheap <cheap-id> --strong <strong-id> \\
        --threshold <eval.json threshold>

LABELS, in order of trust
-------------------------
(b) the owner's own labelled turns: a ``scripts/groundtruth`` dataset directory
    (``--groundtruth-dir``, repeatable). A task is labelled when its label row
    has ``status``/``label_status`` = ``labelled``; needs_frontier = its
    ``cheapest_acceptable_model`` is the top tier (``premium``). Only
    ``tune.jsonl`` is read: the held-out ``test.jsonl`` is never opened.
    The release outcome audit (``scripts/release/outcome_audit.py``) is NOT a
    label source: its labels say whether routed output was *adopted*, not
    whether a cheaper model was *capable*, and it writes no prompt text.
(a) public priors: the audited external corpus (``<data-dir>/corpus/*.jsonl``,
    27 public train splits hash-audited against RouterArena at overlap 0) and
    the measured per-model outcomes (``<data-dir>/outcomes/*.jsonl``).
    needs_frontier = 1 iff ``--cheap`` answered wrong AND ``--strong`` answered
    right: the only case where escalating buys anything (the same label as
    ``scripts/routerarena/escalation.py``). Model ids are arguments, never
    literals here. No RouterArena question, answer or outcome is read.

Private prompt text never enters the repo: the artifact holds embeddings and
labels only, is written to the state directory, and is never committed.

EVALUATION (``eval``), fixed before it was run
----------------------------------------------
* Stratified (source × label) 80/20 dev/held-out split, seed 20260930.
* 5-fold stratified CV on dev: kNN vs the regex/length heuristic
  (``classify._complexity`` via ``classify_signals``, HOOK_POLICY and
  GATEWAY_POLICY), AUC for predicting needs_frontier.
* Held-out: index = all of dev, queries = held-out. ΔAUC with a paired
  bootstrap 95% CI (1,000 resamples). Threshold fitted on dev out-of-fold
  scores (max balanced accuracy), then balanced accuracy on held-out.
* Leave-one-source-out: every source is scored against an index built from
  the other 26, a transfer test to a distribution the index has not seen.
* Owner distribution: labelled owner turns are scored too when any exist;
  otherwise the report says n=0 and the unlabelled organic prompts are used
  only for a distribution-shift check (nearest-neighbour similarity) and for
  latency.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_router import complexity_knn as ck  # noqa: E402
from llm_router.classify import GATEWAY_POLICY, HOOK_POLICY, classify_signals  # noqa: E402

SEED = 20260930
EMBED_MODEL = "nomic-embed-text"  # the model semantic_classify embeds with
FRONTIER_TIER = "premium"  # scripts/groundtruth/run_matrix.py TIERS, top tier
_RANK = {"simple": 0, "moderate": 1, "complex": 2, "deep_reasoning": 3}


def text_key(text: str) -> str:
    """The key the external outcome files and embed caches use."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── labels ────────────────────────────────────────────────────────────────────


def public_items(data_dir: Path, cheap: str, strong: str) -> list[dict]:
    """``[{text, y, source, origin}]`` from the external corpus + outcomes."""
    prompts: dict[str, dict] = {}
    for path in sorted((data_dir / "corpus").glob("*.jsonl")):
        if path.name.startswith("_"):
            continue
        for line in path.open(encoding="utf-8"):
            it = json.loads(line)
            prompts[text_key(it["prompt"])] = it
    cells: dict[str, dict[str, float]] = defaultdict(dict)
    for path in sorted((data_dir / "outcomes").glob("*.jsonl")):
        for line in path.open(encoding="utf-8"):
            r = json.loads(line)
            if r.get("error") or r.get("model") not in (cheap, strong) or "prompt_hash" not in r:
                continue
            cells[r["prompt_hash"]][r["model"]] = float(r["correct"])
    out = []
    for h, c in sorted(cells.items()):
        if cheap in c and strong in c and h in prompts:
            y = 1 if (c[cheap] < 0.5 and c[strong] >= 0.5) else 0
            out.append({"text": prompts[h]["prompt"], "y": y, "source": prompts[h].get("source", "?"),
                        "origin": "public"})
    return out


def groundtruth_items(gt_dir: Path) -> list[dict]:
    """Labelled owner turns from a groundtruth dataset dir (tune split only)."""
    tasks = {}
    tune = gt_dir / "tune.jsonl"
    if tune.is_file():
        for line in tune.open(encoding="utf-8"):
            t = json.loads(line)
            if t.get("task_id") and t.get("prompt"):
                tasks[t["task_id"]] = t["prompt"]
    out = []
    for path in sorted(gt_dir.glob("labels*.jsonl")):
        for line in path.open(encoding="utf-8"):
            r = json.loads(line)
            status = r.get("status") or r.get("label_status")
            cam = r.get("cheapest_acceptable_model")
            if status != "labelled" or not cam or r.get("task_id") not in tasks:
                continue
            out.append({"text": tasks[r["task_id"]], "y": int(cam == FRONTIER_TIER),
                        "source": f"owner:{gt_dir.name}", "origin": "owner"})
    return out


# ── embeddings ────────────────────────────────────────────────────────────────


def _ollama_embed(text: str, url: str) -> list[float] | None:
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode()
    req = urllib.request.Request(f"{url}/api/embeddings", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            v = json.loads(resp.read()).get("embedding")
            return v if isinstance(v, list) and v else None
    except Exception:  # noqa: BLE001 — a failed embed drops the item, reported below
        return None


def embed_all(texts: list[str], *, url: str, cache_path: Path,
              seed_caches: list[Path], workers: int = 4) -> dict[str, list[float]]:
    """Embeddings keyed by ``text_key``. Seed caches are read-only (and only
    trusted for texts <= 4000 chars, the length their writer truncated at);
    new vectors are appended to ``cache_path``."""
    cache: dict[str, list[float]] = {}
    for p in [*seed_caches, cache_path]:
        if p.is_file():
            own = p == cache_path
            for line in p.open(encoding="utf-8"):
                r = json.loads(line)
                cache[r["k"]] = (r["v"], own)
    wanted = {text_key(t): t for t in texts}
    todo = [t for k, t in wanted.items()
            if k not in cache or (not cache[k][1] and len(t) > 4000)]
    if todo:
        print(f"  embedding {len(todo)} texts ({len(wanted) - len(todo)} cached)", file=sys.stderr)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(workers) as ex:
            for i, (t, v) in enumerate(ex.map(lambda t: (t, _ollama_embed(t, url)), todo), 1):
                if v:
                    cache[text_key(t)] = (v, True)
                    fh.write(json.dumps({"k": text_key(t), "v": v}) + "\n")
                if i % 1000 == 0:
                    print(f"    {i}/{len(todo)}", file=sys.stderr)
    return {k: cache[k][0] for k in wanted if k in cache}


# ── scoring many queries (numpy when present; pure python for small inputs) ──


def knn_scores(q_vecs, x_vecs, x_labels, *, k=ck.DEFAULT_K, kappa=ck.DEFAULT_KAPPA,
               temperature=ck.DEFAULT_TEMPERATURE, prior=None):
    """(scores, evidences, max_sims) for each query against an index."""
    prior = sum(x_labels) / len(x_labels) if prior is None else prior
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is None:
        art = ck.Artifact.from_parts(x_vecs, x_labels, embedding_model=EMBED_MODEL, k=k,
                                     kappa=kappa, temperature=temperature, prior=prior)
        out_s, out_e, out_m = [], [], []
        for q in q_vecs:
            u = ck._unit(q)
            sims = [sum(a * b for a, b in zip(u, r)) for r in art.rows]
            s = ck.score_vector(q, art)
            out_s.append(s.score)
            out_e.append(s.evidence)
            out_m.append(max(sims))
        return out_s, out_e, out_m
    X = np.asarray(x_vecs, dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-12
    Y = np.asarray(x_labels, dtype=np.float64)
    kk = min(k, len(X))
    scores, evid, maxs = [], [], []
    for start in range(0, len(q_vecs), 512):
        Q = np.asarray(q_vecs[start:start + 512], dtype=np.float32)
        Q /= np.linalg.norm(Q, axis=1, keepdims=True) + 1e-12
        S = Q @ X.T
        idx = np.argpartition(-S, kk - 1, axis=1)[:, :kk]
        top = np.take_along_axis(S, idx, axis=1).astype(np.float64)
        mx = top.max(axis=1, keepdims=True)
        w = np.exp((top - mx) / temperature)
        num = (w * Y[idx]).sum(axis=1)
        den = w.sum(axis=1)
        scores.extend(((num + kappa * prior) / (den + kappa)).tolist())
        evid.extend((den / (den + kappa)).tolist())
        maxs.extend(mx[:, 0].tolist())
    return scores, evid, maxs


# ── statistics ────────────────────────────────────────────────────────────────


def heuristic_rank(text: str, policy) -> int:
    return _RANK[classify_signals(text, policy).complexity.value]


def stratified_split(items: list[dict], frac: float, seed: int = SEED) -> tuple[list[int], list[int]]:
    by: dict[tuple, list[int]] = defaultdict(list)
    for i, it in enumerate(items):
        by[(it["source"], it["y"])].append(i)
    rng = random.Random(seed)
    dev, test = [], []
    for key in sorted(by):
        idx = by[key][:]
        rng.shuffle(idx)
        n_test = round(len(idx) * frac)
        test += idx[:n_test]
        dev += idx[n_test:]
    return sorted(dev), sorted(test)


def stratified_folds(items: list[dict], idx: list[int], n: int = 5, seed: int = SEED) -> list[list[int]]:
    by: dict[tuple, list[int]] = defaultdict(list)
    for i in idx:
        by[(items[i]["source"], items[i]["y"])].append(i)
    rng = random.Random(seed + 1)
    folds: list[list[int]] = [[] for _ in range(n)]
    j = 0
    for key in sorted(by):
        g = by[key][:]
        rng.shuffle(g)
        for i in g:
            folds[j % n].append(i)
            j += 1
    return [sorted(f) for f in folds]


def bootstrap_delta(a, b, y, n_boot=1000, seed=SEED):
    """Paired bootstrap of AUC(a) − AUC(b): (point, lo, hi, auc_a_ci, auc_b_ci)."""
    rng = random.Random(seed)
    n = len(y)
    da, aa, bb = [], [], []
    for _ in range(n_boot):
        s = [rng.randrange(n) for _ in range(n)]
        ya = [y[i] for i in s]
        x1 = ck.auc([a[i] for i in s], ya)
        x2 = ck.auc([b[i] for i in s], ya)
        aa.append(x1)
        bb.append(x2)
        da.append(x1 - x2)

    def ci(v):
        v = sorted(x for x in v if x == x)
        return round(v[int(0.025 * len(v))], 4), round(v[int(0.975 * len(v)) - 1], 4)

    return (round(ck.auc(a, y) - ck.auc(b, y), 4), *ci(da), ci(aa), ci(bb))


def best_threshold(scores, y) -> float:
    """Threshold maximising balanced accuracy on (out-of-fold) scores."""
    best, best_t = -1.0, 0.5
    for t in sorted(set(round(s, 3) for s in scores)):
        ba = balanced_accuracy([s >= t for s in scores], y)
        if ba > best:
            best, best_t = ba, t
    return best_t


def balanced_accuracy(pred, y) -> float:
    tp = sum(1 for p, t in zip(pred, y) if p and t)
    tn = sum(1 for p, t in zip(pred, y) if not p and not t)
    pos = sum(y)
    neg = len(y) - pos
    return 0.5 * ((tp / pos if pos else 0.0) + (tn / neg if neg else 0.0))


def wilson(k: int, n: int, z: float = 1.959964):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return round(c - h, 4), round(c + h, 4)


# ── commands ──────────────────────────────────────────────────────────────────


def load_labelled(args) -> list[dict]:
    items = public_items(args.data_dir, args.cheap, args.strong) if args.data_dir else []
    for g in args.groundtruth_dir or []:
        items += groundtruth_items(Path(g).expanduser())
    return items


def attach_vectors(items, args):
    vecs = embed_all([it["text"] for it in items], url=args.ollama_url, cache_path=args.cache,
                     seed_caches=[Path(p).expanduser() for p in args.seed_cache or []])
    kept = [dict(it, v=vecs[text_key(it["text"])]) for it in items if text_key(it["text"]) in vecs]
    return kept, len(items) - len(kept)


def evaluate(items: list[dict], organic: list[str], args) -> dict:
    y = [it["y"] for it in items]
    heur = {name: [heuristic_rank(it["text"], pol) for it in items]
            for name, pol in (("hook", HOOK_POLICY), ("gateway", GATEWAY_POLICY))}
    public = [i for i, it in enumerate(items) if it["origin"] == "public"]
    owner = [i for i, it in enumerate(items) if it["origin"] == "owner"]
    pub_items = [items[i] for i in public]
    dev_l, test_l = stratified_split(pub_items, 0.2)
    dev = [public[i] for i in dev_l]
    test = [public[i] for i in test_l]
    rep: dict = {"n_public": len(public), "n_owner_labelled": len(owner),
                 "base_rate_public": round(sum(y[i] for i in public) / max(1, len(public)), 4),
                 "n_dev": len(dev), "n_test": len(test),
                 "hyperparameters": {"k": ck.DEFAULT_K, "kappa": ck.DEFAULT_KAPPA,
                                     "temperature": ck.DEFAULT_TEMPERATURE}}

    # 5-fold CV on dev
    folds = stratified_folds(items, dev)
    oof = {}
    per_fold = []
    for f in folds:
        fs = set(f)
        idx = [i for i in dev if i not in fs]
        s, _, _ = knn_scores([items[i]["v"] for i in f], [items[i]["v"] for i in idx], [y[i] for i in idx])
        oof.update(zip(f, s))
        fy = [y[i] for i in f]
        per_fold.append({"knn": round(ck.auc(s, fy), 4),
                         "hook": round(ck.auc([heur["hook"][i] for i in f], fy), 4),
                         "gateway": round(ck.auc([heur["gateway"][i] for i in f], fy), 4)})
    dy = [y[i] for i in dev]
    d_knn = [oof[i] for i in dev]
    rep["cv"] = {"folds": per_fold,
                 "knn_mean": round(statistics.mean(p["knn"] for p in per_fold), 4),
                 "knn_sd": round(statistics.stdev(p["knn"] for p in per_fold), 4),
                 "hook_mean": round(statistics.mean(p["hook"] for p in per_fold), 4),
                 "gateway_mean": round(statistics.mean(p["gateway"] for p in per_fold), 4),
                 "pooled_delta_vs_hook": bootstrap_delta(d_knn, [heur["hook"][i] for i in dev], dy,
                                                         n_boot=args.n_boot)}
    thr = best_threshold(d_knn, dy)
    rep["threshold"] = thr

    # held-out
    s_test, e_test, m_test = knn_scores([items[i]["v"] for i in test], [items[i]["v"] for i in dev],
                                        [y[i] for i in dev])
    ty = [y[i] for i in test]
    ho = {"auc_knn": round(ck.auc(s_test, ty), 4)}
    for name in ("hook", "gateway"):
        h = [heur[name][i] for i in test]
        pt, lo, hi, ci_k, ci_h = bootstrap_delta(s_test, h, ty, n_boot=args.n_boot)
        ho[f"auc_{name}"] = round(ck.auc(h, ty), 4)
        ho[f"auc_{name}_ci"] = ci_h
        ho["auc_knn_ci"] = ci_k
        ho[f"delta_vs_{name}"] = {"point": pt, "ci95": [lo, hi]}
        ho[f"balacc_{name}"] = round(balanced_accuracy([r >= 2 for r in h], ty), 4)
    # how much of the kNN signal is just "which dataset is this": a baseline that
    # scores each held-out item with its source's dev base rate
    src_rate: dict[str, list[int]] = defaultdict(list)
    for i in dev:
        src_rate[items[i]["source"]].append(y[i])
    src_s = [statistics.mean(src_rate.get(items[i]["source"], [0])) for i in test]
    pt, lo, hi, _, ci_src = bootstrap_delta(s_test, src_s, ty, n_boot=args.n_boot)
    ho["auc_source_rate"] = round(ck.auc(src_s, ty), 4)
    ho["auc_source_rate_ci"] = ci_src
    ho["delta_vs_source_rate"] = {"point": pt, "ci95": [lo, hi]}
    ho["balacc_knn"] = round(balanced_accuracy([s >= thr for s in s_test], ty), 4)
    ho["flag_rate_knn"] = round(sum(s >= thr for s in s_test) / len(s_test), 4)
    ho["median_nn_sim"] = round(statistics.median(m_test), 4)
    rep["held_out"] = ho

    # leave-one-source-out (transfer to an unseen distribution)
    sources = sorted({items[i]["source"] for i in public})
    lo_s: dict[int, float] = {}
    for src in sources:
        q = [i for i in public if items[i]["source"] == src]
        idx = [i for i in public if items[i]["source"] != src]
        s, _, _ = knn_scores([items[i]["v"] for i in q], [items[i]["v"] for i in idx], [y[i] for i in idx])
        lo_s.update(zip(q, s))
    py = [y[i] for i in public]
    pt, lo, hi, ci_k, ci_h = bootstrap_delta([lo_s[i] for i in public], [heur["hook"][i] for i in public],
                                             py, n_boot=args.n_boot)
    rep["leave_one_source_out"] = {"n_sources": len(sources), "auc_knn": round(ck.auc([lo_s[i] for i in public], py), 4),
                                   "auc_knn_ci": ci_k, "auc_hook": round(ck.auc([heur["hook"][i] for i in public], py), 4),
                                   "auc_hook_ci": ci_h, "delta_vs_hook": {"point": pt, "ci95": [lo, hi]}}

    # owner distribution
    if owner:
        s_o, _, _ = knn_scores([items[i]["v"] for i in owner], [items[i]["v"] for i in public],
                               [y[i] for i in public])
        oy = [y[i] for i in owner]
        rep["owner"] = {"n": len(owner), "positives": sum(oy),
                        "auc_knn": round(ck.auc(s_o, oy), 4),
                        "auc_hook": round(ck.auc([heur["hook"][i] for i in owner], oy), 4)}
    else:
        rep["owner"] = {"n": 0, "note": "no labelled owner turns exist; target distribution unmeasured"}
    if organic:
        vecs = embed_all(organic, url=args.ollama_url, cache_path=args.cache, seed_caches=[])
        ov = [vecs[text_key(t)] for t in organic if text_key(t) in vecs]
        ot = [t for t in organic if text_key(t) in vecs]
        s_o, e_o, m_o = knn_scores(ov, [items[i]["v"] for i in public], [y[i] for i in public])
        rep["organic_unlabelled"] = {
            "n": len(ov), "median_nn_sim": round(statistics.median(m_o), 4),
            "flag_rate_knn": round(sum(s >= thr for s in s_o) / len(s_o), 4),
            "flag_rate_knn_ci": wilson(sum(s >= thr for s in s_o), len(s_o)),
            "flag_rate_hook": round(sum(heuristic_rank(t, HOOK_POLICY) >= 2 for t in ot) / len(ot), 4),
            "median_score": round(statistics.median(s_o), 4),
            "median_evidence": round(statistics.median(e_o), 4)}
    return rep


def latency(art_path: Path, texts: list[str], url: str) -> dict:
    """Per-prompt embed and kNN time through the RUNTIME code path (pure python)."""
    import os

    os.environ["LLM_ROUTER_COMPLEXITY_KNN_ARTIFACT"] = str(art_path)
    ck.clear_cache()
    t0 = time.perf_counter()
    art = ck.load_artifact()
    load_s = time.perf_counter() - t0
    if art is None:
        raise SystemExit(f"no usable artifact at {art_path}")
    emb, knn = [], []
    for t in texts:
        a = time.perf_counter()
        v = _ollama_embed(t, url)
        b = time.perf_counter()
        if v is None:
            continue
        ck.score_vector(v, art)
        c = time.perf_counter()
        emb.append((b - a) * 1000)
        knn.append((c - b) * 1000)

    def summary(v):
        v = sorted(v)
        return {"median_ms": round(statistics.median(v), 1), "p95_ms": round(v[int(0.95 * (len(v) - 1))], 1)}

    return {"n": len(emb), "index_rows": art.n, "artifact_load_s": round(load_s, 2),
            "embed": summary(emb), "knn": summary(knn),
            "total": summary([a + b for a, b in zip(emb, knn)])}


def organic_texts(paths: list[str]) -> list[str]:
    out = []
    for p in paths or []:
        for line in Path(p).expanduser().open(encoding="utf-8"):
            r = json.loads(line)
            t = r.get("text") or r.get("prompt")
            if t:
                out.append(t)
    return out


def main(argv=None) -> int:
    from llm_router.paths import state_path

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("eval", "build", "latency"))
    ap.add_argument("--data-dir", type=lambda p: Path(p).expanduser(),
                    help="dir holding corpus/ and outcomes/ (the external public corpus)")
    ap.add_argument("--cheap", help="model id whose failure marks an item hard")
    ap.add_argument("--strong", help="model id whose success marks escalation as paying")
    ap.add_argument("--groundtruth-dir", action="append", help="owner-labelled groundtruth dataset dir")
    ap.add_argument("--organic", action="append",
                    help="JSONL of unlabelled owner prompts (text|prompt), for shift + latency only")
    ap.add_argument("--seed-cache", action="append", help="read-only embed cache (k, v) JSONL")
    ap.add_argument("--cache", type=Path, default=state_path("complexity_knn_embed_cache.jsonl"))
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--out", type=Path, default=None, help="artifact path (default: state dir)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="build: required; the threshold `eval` fitted on dev out-of-fold scores")
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--latency-n", type=int, default=100)
    args = ap.parse_args(argv)

    if args.cmd == "latency":
        texts = organic_texts(args.organic)[: args.latency_n]
        rep = latency(args.out or ck.artifact_path(), texts, args.ollama_url)
        print(json.dumps(rep, indent=2))
        return 0

    if args.cmd == "build" and args.threshold is None:
        ap.error("build needs --threshold: take it from the eval report (an unevaluated threshold ships nothing)")
    if args.data_dir and not (args.cheap and args.strong):
        ap.error("--data-dir needs --cheap and --strong")
    items = load_labelled(args)
    if not items:
        ap.error("no labelled items: pass --data-dir and/or --groundtruth-dir")
    items, dropped = attach_vectors(items, args)
    print(f"labelled items: {len(items)} (dropped {dropped} without an embedding)", file=sys.stderr)

    if args.cmd == "eval":
        rep = evaluate(items, organic_texts(args.organic), args)
        rep["dropped_no_embedding"] = dropped
        rep["label_rule"] = {"cheap": args.cheap, "strong": args.strong,
                             "needs_frontier": "cheap wrong AND strong right"}
        text = json.dumps(rep, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0

    # build
    thr = args.threshold
    out = ck.write_artifact(
        args.out or ck.artifact_path(), [it["v"] for it in items], [it["y"] for it in items],
        embedding_model=EMBED_MODEL, threshold=thr,
        provenance={"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "n_public": sum(it["origin"] == "public" for it in items),
                    "n_owner": sum(it["origin"] == "owner" for it in items),
                    "label_rule": {"cheap": args.cheap, "strong": args.strong},
                    "builder": "scripts/build_complexity_knn.py"})
    print(f"wrote {out} ({len(items)} rows, threshold {thr})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
