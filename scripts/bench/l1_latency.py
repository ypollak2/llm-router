#!/usr/bin/env python3
"""Real-corpus L1 latency and stability (PLAN v16 P1.6-c, R-CLS-8 heuristic half; W2-8 / UN-11).

L1 is the synchronous rules path of ``llm_router.engine.decide`` (the one decision all doors share;
no LLM, no network). ``tests/test_engine_core.py`` times it on ``synth()`` prompts, a proxy
population; this script times it on a REAL prompt corpus that you pass in. The corpus never
enters the repo and its text is never printed: the output carries ids, counts and a sha256 only.

Corpus format: JSON Lines, one object per line, ``{"i": <int|str id>, "text": "<prompt>"}``
(``--id-field`` / ``--text-field`` rename the keys). Lines without text are skipped and counted.
The first ``--n`` usable rows in file order are used (deterministic, no sampling).

Method
  * Latency: each prompt is decided once in a fresh worker process, ``time.perf_counter`` around
    ``decide(text, door="hook")``, decision cache OFF so duplicate prompts are computed, not served.
    The first call is reported as ``cold_first_ms``; p50/p95/p99/max are over all n of run 1.
  * Stability: ``--runs`` workers (default 2), each a separate process with a different
    PYTHONHASHSEED, decide the same prompts. The first ``--stab-n`` (default 500) prompts are
    compared on every Decision field except ``layer`` and ``ms``. Reported: agreement share and the
    disagreeing ids (never text).
  * Isolation: scratch HOME / LLM_ROUTER_HOME per worker; ``socket`` connect, create_connection and
    getaddrinfo raise, so a model or network call cannot happen (a decision that hits the guard is
    counted in ``net_attempts`` and fails the run).
  * ``uncontended`` is true only when load1 (``os.getloadavg``) < ``--max-load`` (default 5).

Usage
  python scripts/bench/l1_latency.py --corpus /path/to/corpus.jsonl [--n 1000] [--runs 2] [--out f.json]

A rehearsal and a gate run use the same command; only the SHA and the machine state differ, and the
output records both. ``--engine-spec module:func`` swaps the decide function (positive control in
the test; default ``llm_router.engine:decide``).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
P95_BAR_MS = 50.0
SEEDS = ("101", "202", "303", "404")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_corpus(path: Path, n: int, id_field: str = "i", text_field: str = "text") -> tuple[list[tuple[str, str]], int]:
    rows, skipped = [], 0
    with path.open(encoding="utf-8") as f:
        for ln, line in enumerate(f):
            if not line.strip():
                continue
            try:
                o = json.loads(line)
                text = o[text_field]
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            if not isinstance(text, str) or not text.strip():
                skipped += 1
                continue
            rows.append((str(o.get(id_field, ln)), text))
            if len(rows) >= n:
                break
    return rows, skipped


def pct(sorted_ms: list[float], q: float) -> float:
    if not sorted_ms:
        return float("nan")
    return sorted_ms[max(0, min(len(sorted_ms) - 1, int(q * len(sorted_ms) + 0.5) - 1))]


def summarize(ms: list[float]) -> dict:
    s = sorted(ms)
    return {"n": len(s), "p50_ms": round(pct(s, 0.50), 3), "p95_ms": round(pct(s, 0.95), 3),
            "p99_ms": round(pct(s, 0.99), 3), "max_ms": round(s[-1], 3) if s else None}


# ── worker: one process, one pass over the corpus ────────────────────────────

def _guard_network():
    hits = []

    def boom(*a, **k):
        hits.append(1)
        raise OSError("network blocked by l1_latency")

    socket.socket.connect = boom  # type: ignore[assignment]
    socket.create_connection = boom
    socket.getaddrinfo = boom
    return hits


def _decision_key(d) -> str:
    import dataclasses
    if dataclasses.is_dataclass(d):
        f = {k: v for k, v in dataclasses.asdict(d).items() if k not in ("layer", "ms")}
    else:
        f = {k: v for k, v in dict(d).items() if k not in ("layer", "ms")}
    return json.dumps(f, sort_keys=True, default=str)


def worker(corpus: Path, n: int, id_field: str, text_field: str, spec: str) -> int:
    rows, _ = load_corpus(corpus, n, id_field, text_field)
    mod, _, fn = spec.partition(":")
    hits = _guard_network()
    decide = getattr(importlib.import_module(mod), fn)
    ms, keys = [], {}
    for rid, text in rows:
        t = time.perf_counter()
        d = decide(text, door="hook")
        ms.append((time.perf_counter() - t) * 1000)
        keys[rid] = hashlib.sha256(_decision_key(d).encode()).hexdigest()[:16]
    json.dump({"ms": ms, "keys": keys, "net_attempts": len(hits),
               "hashseed": os.environ.get("PYTHONHASHSEED")}, sys.stdout)
    return 0


# ── parent ───────────────────────────────────────────────────────────────────

def _run_worker(args, seed: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="l1bench-") as home:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_ROUTER_", "OLLAMA_"))}
        env.update(HOME=home, LLM_ROUTER_HOME=home, LLM_ROUTER_DECIDE_CACHE="off", PYTHONHASHSEED=seed,
                   PYTHONPATH=os.pathsep.join(filter(None, [str(REPO / "src"), env.get("PYTHONPATH", "")])))
        cmd = [sys.executable, os.path.abspath(__file__), "--worker", "--corpus", str(args.corpus), "--n", str(args.n),
               "--id-field", args.id_field, "--text-field", args.text_field, "--engine-spec", args.engine_spec]
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=home)
    if p.returncode != 0:
        raise SystemExit("worker failed (seed %s): %s" % (seed, p.stderr.strip().splitlines()[-1:] or p.returncode))
    return json.loads(p.stdout)


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def measure(args) -> dict:
    rows, skipped = load_corpus(args.corpus, args.n, args.id_field, args.text_field)
    if not rows:
        raise SystemExit("corpus has no usable rows: nothing to measure")
    load1 = os.getloadavg()[0]
    runs = [_run_worker(args, SEEDS[i % len(SEEDS)]) for i in range(args.runs)]
    ms0 = runs[0]["ms"]
    warm = ms0[1:]
    stab_ids = [rid for rid, _ in rows[: args.stab_n]]
    stability = None
    if len(runs) >= 2:
        base = runs[0]["keys"]
        bad = sorted({rid for r in runs[1:] for rid in stab_ids if r["keys"].get(rid) != base.get(rid)})
        stability = {"n": len(stab_ids), "runs": len(runs), "hashseeds": [r["hashseed"] for r in runs],
                     "agree": len(stab_ids) - len(bad), "agree_share": round((len(stab_ids) - len(bad)) / len(stab_ids), 6),
                     "disagreeing_ids": bad}
    lat = summarize(ms0)
    net = sum(r["net_attempts"] for r in runs)
    now = datetime.now(timezone.utc)
    return {
        "script": "scripts/bench/l1_latency.py", "label": args.label,
        "window": {"date": now.strftime("%Y-%m-%d"), "start_utc": now.isoformat(timespec="seconds")},
        "sha": _git_sha(), "corpus_sha256": _sha256(args.corpus), "corpus_file": args.corpus.name,
        "n": len(rows), "n_requested": args.n, "rows_skipped": skipped,
        "engine_spec": args.engine_spec,
        "latency": {**lat, "cold_first_ms": round(ms0[0], 3), "warm": summarize(warm),
                    "per_run_p95_ms": [round(pct(sorted(r["ms"]), 0.95), 3) for r in runs],
                    "bar_p95_ms": P95_BAR_MS, "p95_within_bar": lat["p95_ms"] <= P95_BAR_MS},
        "stability": stability,
        "network": {"guarded": True, "attempts": net},
        "machine": {"load1_before": round(load1, 2), "max_load": args.max_load,
                    "uncontended": load1 < args.max_load, "python": sys.version.split()[0]},
        "ok": net == 0 and (stability is None or not stability["disagreeing_ids"]),
    }


def _priv_write(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--stab-n", type=int, default=500)
    ap.add_argument("--max-load", type=float, default=5.0)
    ap.add_argument("--id-field", default="i")
    ap.add_argument("--text-field", default="text")
    ap.add_argument("--engine-spec", default="llm_router.engine:decide")
    ap.add_argument("--label", default="unlabelled", help="e.g. rehearsal or gate")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        return worker(args.corpus, args.n, args.id_field, args.text_field, args.engine_spec)
    if args.runs < 1:
        ap.error("--runs must be >= 1")
    res = measure(args)
    text = json.dumps(res, indent=2, sort_keys=True)
    if args.out:
        _priv_write(args.out, text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
