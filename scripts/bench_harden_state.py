"""Time ``state_files.harden_state`` on a synthetic tree (W0-4c: source of the RETENTION number).

Usage: ``python scripts/bench_harden_state.py [N_FILES] [REPEATS]``  (defaults 3000, 5)

Builds N_FILES files at 0644 in 30 directories (0755) under a temporary LLM_ROUTER_HOME, then times
``harden_state()``: the first pass (all files need a chmod) and the repeat passes (nothing to change,
which is every doctor run after the first). Prints one JSON line with N, the repeat count, the
machine and the timings in seconds. Uses a temporary directory only.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    n = int(argv[1]) if len(argv) > 1 else 3000
    repeats = int(argv[2]) if len(argv) > 2 else 5
    from llm_router import state_files

    with tempfile.TemporaryDirectory(prefix="harden-bench-") as tmp:
        root = Path(tmp) / ".llm-router"
        os.environ["LLM_ROUTER_HOME"] = str(root)
        os.umask(0o022)
        for i in range(n):
            d = root / f"d{i % 30}"
            d.mkdir(parents=True, exist_ok=True)
            with state_files.open_private(d / f"f{i}.jsonl", "w") as fh:
                fh.write("{}\n")
            os.chmod(d / f"f{i}.jsonl", 0o644)
        for d in root.iterdir():
            os.chmod(d, 0o755)
        t0 = time.perf_counter()
        first = state_files.harden_state()
        first_s = time.perf_counter() - t0
        again = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            state_files.harden_state()
            again.append(time.perf_counter() - t0)
    print(json.dumps({
        "files": n, "repeats": repeats, "machine": f"{platform.platform()} {platform.machine()}",
        "python": platform.python_version(), "first_pass_s": round(first_s, 4),
        "first_pass_files_fixed": (first or {}).get("files_fixed"),
        "repeat_median_s": round(statistics.median(again), 4), "repeat_max_s": round(max(again), 4),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
