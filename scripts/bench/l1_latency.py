#!/usr/bin/env python3
"""Real-corpus L1 latency and stability (PLAN v16 P1.6-c, R-CLS-8 heuristic half; W2-8 / UN-11).

L1 is the synchronous rules path of ``llm_router.engine.decide`` (the one decision all doors share;
no LLM, no network). ``tests/test_engine_core.py`` times it on ``synth()`` prompts, a proxy
population; this script times it on a REAL prompt corpus that you pass in. The corpus never
enters the repo and its text is never printed: the output carries ids, counts and a sha256 only.

Corpus format: JSON Lines, one object per line, ``{"i": <int|str id>, "text": "<prompt>"}``
(``--id-field`` / ``--text-field`` rename the keys). Lines without text are skipped and counted.
``i`` ids are echoed in the output, so they MUST be opaque (no prompt text); an id with whitespace or over 64 chars is replaced by ``row<line>`` and counted in ``ids_redacted``.
Default sample: a seeded random ``--n`` of the usable rows (``--seed``, recorded); ``--first-n`` takes the first n in file order.

Method
  * Latency: each prompt is decided once in a fresh worker process, ``time.perf_counter`` around
    ``decide(text, door="hook")``, decision cache OFF so duplicate prompts are computed, not served.
    The first call is reported as ``cold_first_ms``; p50/p95/p99/max are over all n of run 1.
  * Stability: ``--runs`` workers (default 2), each a separate process with a different
    PYTHONHASHSEED, decide the same prompts. The first ``--stab-n`` (default 500) prompts are
    compared on every Decision field except ``layer`` and ``ms``. Reported: agreement share and the
    disagreeing ids (never text).
  * Scope: rules-only by default (scratch home has no lexical artifact); ``--lexical`` / ``--calibration`` point at
    artifacts (passed as LLM_ROUTER_ENGINE_LEXICAL / _CALIBRATION), their sha256 is recorded. ``scope`` comes from what the worker actually loaded
    (``engine._lexical() is not None``), not from the flag; a given artifact that did not load makes ok false.
  * ``ok`` requires: 0 network attempts, 0 ``fallback`` layer decisions, n == requested, p95 within the bar, stability
    clean. Exit code is 1 when ``ok`` is false.
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
import math
import os
import random
import re
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


def _opaque_id(raw, ln: int) -> tuple[str, bool]:
    """Ids are echoed in the output, so they must be opaque. An id that looks like text (whitespace, or
    longer than 64 chars) is replaced by ``row<line>`` and counted in ``ids_redacted``."""
    sid = str(raw)
    if re.search(r"\s", sid) or len(sid) > 64:
        return "row%d" % ln, True
    return sid, False


def load_corpus(path: Path, n: int, id_field: str = "i", text_field: str = "text", seed: int = 1,
                first_n: bool = False) -> tuple[list[tuple[str, str]], int, int]:
    """Returns (rows, skipped, ids_redacted). Default: a seeded random sample of ``n`` usable rows,
    kept in file order; ``first_n`` takes the first ``n`` instead."""
    rows, skipped, redacted = [], 0, 0
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
            sid, red = _opaque_id(o.get(id_field, ln), ln)
            redacted += red
            rows.append((sid, text))
            if first_n and len(rows) >= n:
                break
    if not first_n and len(rows) > n:
        keep = sorted(random.Random(seed).sample(range(len(rows)), n))
        rows = [rows[k] for k in keep]
    return rows, skipped, redacted


def pct(sorted_ms: list[float], q: float) -> float:
    if not sorted_ms:
        return float("nan")
    return sorted_ms[max(0, min(len(sorted_ms) - 1, math.ceil(q * len(sorted_ms)) - 1))]


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


def worker(corpus: Path, n: int, id_field: str, text_field: str, spec: str, seed: int, first_n: bool) -> int:
    rows, _, _ = load_corpus(corpus, n, id_field, text_field, seed, first_n)
    mod, _, fn = spec.partition(":")
    hits = _guard_network()
    decide = getattr(importlib.import_module(mod), fn)
    ms, keys, layers = [], {}, {}
    for rid, text in rows:
        t = time.perf_counter()
        d = decide(text, door="hook")
        ms.append((time.perf_counter() - t) * 1000)
        lay = str(getattr(d, "layer", None) or (d.get("layer") if isinstance(d, dict) else None) or "unknown")
        layers[lay] = layers.get(lay, 0) + 1
        keys[rid] = hashlib.sha256(_decision_key(d).encode()).hexdigest()[:16]
    from llm_router import engine as _eng  # what the worker really loaded, not what the flags asked for
    lex_loaded = _eng._lexical() is not None
    cal_loaded = bool(_eng._calibration())
    json.dump({"lexical_loaded": lex_loaded, "calibration_loaded": cal_loaded, "ms": ms, "keys": keys, "net_attempts": len(hits), "layers": layers,
               "hashseed": os.environ.get("PYTHONHASHSEED")}, sys.stdout)
    return 0


# ── parent ───────────────────────────────────────────────────────────────────

def _run_worker(args, seed: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="l1bench-") as home:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_ROUTER_", "OLLAMA_"))}
        for var, path in (("LLM_ROUTER_ENGINE_LEXICAL", args.lexical), ("LLM_ROUTER_ENGINE_CALIBRATION", args.calibration)):
            if path:
                env[var] = str(path)
        env.update(HOME=home, LLM_ROUTER_HOME=home, LLM_ROUTER_DECIDE_CACHE="off", PYTHONHASHSEED=seed,
                   PYTHONPATH=os.pathsep.join(filter(None, [str(REPO / "src"), env.get("PYTHONPATH", "")])))
        cmd = [sys.executable, os.path.abspath(__file__), "--worker", "--corpus", str(args.corpus), "--n", str(args.n),
               "--id-field", args.id_field, "--text-field", args.text_field, "--engine-spec", args.engine_spec,
               "--seed", str(args.seed)] + (["--first-n"] if args.first_n else [])
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=home)
    if p.returncode != 0:
        # never echo worker stderr: an exception message could carry prompt text. Type name only.
        last = (p.stderr.strip().splitlines() or [""])[-1].strip()
        kind = last if re.fullmatch(r"ERR:[A-Za-z_][A-Za-z0-9_]*", last) else "exit %s" % p.returncode
        raise SystemExit("worker failed (hashseed %s): %s" % (seed, kind))
    return json.loads(p.stdout)


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def measure(args) -> dict:
    rows, skipped, redacted = load_corpus(args.corpus, args.n, args.id_field, args.text_field, args.seed, args.first_n)
    if not rows:
        raise SystemExit("corpus has no usable rows: nothing to measure")
    load1 = os.getloadavg()[0]
    lex_sha = _sha256(args.lexical) if args.lexical else None
    cal_sha = _sha256(args.calibration) if args.calibration else None
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
    load1_after = os.getloadavg()[0]
    layers: dict[str, int] = {}
    for r in runs:
        for k, v in r["layers"].items():
            layers[k] = layers.get(k, 0) + v
    lex_loaded = all(r["lexical_loaded"] for r in runs)
    cal_loaded = all(r["calibration_loaded"] for r in runs)
    reasons = []
    if args.lexical and not lex_loaded:
        reasons.append("artifact given but not loaded: lexical")
    if args.calibration and not cal_loaded:
        reasons.append("artifact given but not loaded: calibration")
    net = sum(r["net_attempts"] for r in runs)
    now = datetime.now(timezone.utc)
    return {
        "script": "scripts/bench/l1_latency.py", "label": args.label,
        "window": {"date": now.strftime("%Y-%m-%d"), "start_utc": now.isoformat(timespec="seconds")},
        "sha": _git_sha(), "corpus_sha256": _sha256(args.corpus), "corpus_file": args.corpus.name,
        "n": len(rows), "n_requested": args.n, "rows_skipped": skipped, "ids_redacted": redacted,
        "scope": "rules+lexical" if lex_loaded else "rules-only",
        "sampling": {"method": "first-n" if args.first_n else "seeded-random", "seed": None if args.first_n else args.seed},
        "artifacts": {"lexical_sha256": lex_sha, "calibration_sha256": cal_sha,
                      "lexical_loaded": lex_loaded, "calibration_loaded": cal_loaded},
        "not_ok_reasons": reasons,
        "layers": layers,
        "engine_spec": args.engine_spec,
        "latency": {**lat, "cold_first_ms": round(ms0[0], 3), "warm": summarize(warm),
                    "per_run_p95_ms": [round(pct(sorted(r["ms"]), 0.95), 3) for r in runs],
                    "bar_p95_ms": P95_BAR_MS, "p95_within_bar": lat["p95_ms"] <= P95_BAR_MS},
        "stability": stability,
        "network": {"guarded": True, "attempts": net},
        "machine": {"load1_before": round(load1, 2), "load1_after": round(load1_after, 2), "max_load": args.max_load,
                    "uncontended": max(load1, load1_after) < args.max_load, "python": sys.version.split()[0]},
        "n_ok": len(rows) == args.n,
        "ok": (not reasons and net == 0 and layers.get("fallback", 0) == 0 and len(rows) == args.n
               and lat["p95_ms"] <= P95_BAR_MS and (stability is None or not stability["disagreeing_ids"])),
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
    ap.add_argument("--lexical", type=Path, help="engine_lexical.json to load (L3); default none = rules-only")
    ap.add_argument("--calibration", type=Path, help="calibration artifact to load; default none")
    ap.add_argument("--seed", type=int, default=1, help="seed of the default random sample (recorded)")
    ap.add_argument("--first-n", action="store_true", help="take the first n rows in file order instead of a sample")
    ap.add_argument("--label", default="unlabelled", help="e.g. rehearsal or gate")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        try:
            return worker(args.corpus, args.n, args.id_field, args.text_field, args.engine_spec, args.seed, args.first_n)
        except BaseException as e:  # noqa: BLE001 -- type name only, never the message
            sys.stderr.write("ERR:%s\n" % type(e).__name__)
            return 3
    if args.runs < 1:
        ap.error("--runs must be >= 1")
    res = measure(args)
    text = json.dumps(res, indent=2, sort_keys=True)
    if args.out:
        _priv_write(args.out, text + "\n")
    print(text)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
