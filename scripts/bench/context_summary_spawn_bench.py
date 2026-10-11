"""P1.2-a spawn latency harness: p95 of context_summary.spawn() over N spawns.

Runs as its own small process (not inside pytest, whose large heap inflates fork
time). Each spawn execs a no-op child. Prints JSON with n, p50, p95, max (ms) and
os.getloadavg()[0]. Usage: python scripts/bench/context_summary_spawn_bench.py [N]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

from llm_router import context_summary as cs  # noqa: E402


def main(n: int = 100) -> dict:
    os.environ["LLM_ROUTER_HOME"] = tempfile.mkdtemp(prefix="cs-bench-")
    argv = [sys.executable, "-I", "-c", "pass"]
    ms = []
    for i in range(n):
        t0 = time.perf_counter()
        assert cs.spawn(f"bench-{i}", "/x/t.jsonl", argv=argv)
        ms.append((time.perf_counter() - t0) * 1000)
    time.sleep(0.5)
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass
    ms.sort()
    return {"n": n, "p50_ms": round(ms[n // 2], 2), "p95_ms": round(ms[int(n * 0.95) - 1], 2),
            "max_ms": round(ms[-1], 2), "load1": round(os.getloadavg()[0], 2)}


if __name__ == "__main__":
    print(json.dumps(main(int(sys.argv[1]) if len(sys.argv) > 1 else 100)))
