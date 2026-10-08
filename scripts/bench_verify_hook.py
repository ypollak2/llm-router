#!/usr/bin/env python3
"""Hook overhead of the verifier's Codex marker (VERIFIER_PLAN section 6: p95 <= 50 ms).

Measures the one call ``hooks/agent-route.py`` adds after a Codex delegation returned, in a FRESH
interpreter per sample. The ``verify_queue`` import is inside the timing; the ``llm_router`` package and
``codex_agent`` (already imported by the hook on this path) are not:

    _verify_enqueue_marker()    three concurrent git reads (rev-parse, ls-files -o, diff HEAD),
                                untracked files turned into diff text in Python, patch + marker write

The repo is a scratch clone (never the one given): each sample edits two tracked files and adds two
untracked files, times the calls, and resets the clone. State goes to a temp LLM_ROUTER_HOME.

    python scripts/bench_verify_hook.py --repo /path/to/repo --n 50
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")  # benchmark traffic, never production spend

CHILD = r"""
import json, os, sys, time
# What the hook has already imported by the time it delegates (it ran is_codex_available / run_codex
# and the ledger helpers): the llm_router package and codex_agent. Not timed.
import llm_router, llm_router.codex_agent
t0 = time.perf_counter()
from llm_router import verify_queue as Q
t1 = time.perf_counter()
status = Q.enqueue_from_run(os.getcwd(), session_id="bench-session", ts=time.time(), content="ok")
t2 = time.perf_counter()
print(json.dumps({"import_ms": (t1 - t0) * 1e3, "enqueue_ms": (t2 - t1) * 1e3, "total_ms": (t2 - t0) * 1e3, "status": status}))
"""


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--n", type=int, default=50)
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="llmr-bench-vh-"))
    try:
        clone = work / "clone"
        subprocess.run(["git", "clone", "-q", "--local", args.repo, str(clone)], check=True)
        tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=clone, capture_output=True, text=True,
                                 check=True).stdout.split()[:2]
        n_files = len(subprocess.run(["git", "ls-files"], cwd=clone, capture_output=True, text=True,
                                     check=True).stdout.split())
        env = {**os.environ, "LLM_ROUTER_HOME": str(work / "home"), "HOME": str(work / "h")}
        (work / "h").mkdir()
        rows = []
        for i in range(args.n):
            for rel in tracked:
                with open(clone / rel, "a") as fh:
                    fh.write(f"\n# bench edit {i}\n")
            (clone / "bench_new_a.py").write_text("A = 1\n")
            (clone / "bench_new_b.py").write_text("B = 2\n")
            out = subprocess.run([sys.executable, "-c", CHILD], cwd=clone, env=env, capture_output=True, text=True)
            rows.append(json.loads(out.stdout.strip().splitlines()[-1]))
            assert rows[-1]["status"] == "queued", rows[-1]
            subprocess.run(["git", "checkout", "-q", "--", "."], cwd=clone, check=True)
            for f in ("bench_new_a.py", "bench_new_b.py"):
                (clone / f).unlink(missing_ok=True)
            shutil.rmtree(work / "home" / "verify_queue", ignore_errors=True)
        tot = [r["total_ms"] for r in rows]
        res = {"n": len(rows), "tracked_files_in_repo": n_files,
               "total_ms": {"p50": round(statistics.median(tot), 1), "p95": round(_pct(tot, 95), 1),
                            "max": round(max(tot), 1)},
               "import_ms_p50": round(statistics.median(r["import_ms"] for r in rows), 1),
               "enqueue_ms_p50": round(statistics.median(r["enqueue_ms"] for r in rows), 1),
               "budget_p95_ms": 50}
        res["pass"] = res["total_ms"]["p95"] <= 50
        print(json.dumps(res, indent=2))
        return 0 if res["pass"] else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
