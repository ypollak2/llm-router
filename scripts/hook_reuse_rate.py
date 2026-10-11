#!/usr/bin/env python3
"""P1.6-h instrument: how often does the proxy reuse the hook's Decision?

The proxy writes one counts-only line per classification it makes for a session
whose hook left a fresh ``last_classification_<sid>.json``
(``~/.llm-router/decision_reuse.jsonl``: ``{"ts", "reused"}``; no text, no session
id). This prints n, the reuse rate with a Wilson 95% interval, the window and the
file's sha256. The bar is >= 90% on >= 100 hook-seen turn-first calls of ORGANIC
traffic: this script only measures; with n < 100 it says so and does not pass.

    python scripts/hook_reuse_rate.py [--file PATH] [--since UNIX_TS] [--min-n 100]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

Z95 = 1.959963984540054


def wilson95(k: int, n: int) -> tuple[float, float]:
    p = k / n
    d = 1 + Z95 * Z95 / n
    c = (p + Z95 * Z95 / (2 * n)) / d
    h = Z95 * math.sqrt(p * (1 - p) / n + Z95 * Z95 / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def summarize(rows: list[dict], *, min_n: int = 100, bar: float = 0.90) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if r.get("reused") is True)
    out = {"n": n, "reused": k, "min_n": min_n, "bar": bar}
    if n == 0:
        return {**out, "rate": None, "wilson95": None, "verdict": "PENDING LIVE DATA (n=0)"}
    lo, hi = wilson95(k, n)
    ts = [r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))]
    out.update(rate=round(k / n, 4), wilson95=[round(lo, 4), round(hi, 4)],
               window=[min(ts), max(ts)] if ts else None)
    if n < min_n:
        out["verdict"] = "PENDING LIVE DATA (n=%d < %d); not a pass" % (n, min_n)
    else:
        out["verdict"] = "PASS" if lo >= bar else ("FAIL" if hi < bar else "NOT DISTINGUISHABLE from the bar (CI straddles it)")
    return out


def read_rows(path: Path, since: float = 0.0) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("ts", 0) >= since:
            rows.append(r)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", type=Path)
    ap.add_argument("--since", type=float, default=0.0)
    ap.add_argument("--min-n", type=int, default=100)
    a = ap.parse_args(argv)
    path = a.file
    if path is None:
        from llm_router.paths import state_path

        path = state_path("decision_reuse.jsonl")
    if not path.exists():
        print(json.dumps({"n": 0, "verdict": "PENDING LIVE DATA (no file)", "file": str(path)}))
        return 0
    res = summarize(read_rows(path, a.since), min_n=a.min_n)
    res["file"] = str(path)
    res["file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
