#!/usr/bin/env python3
"""Wall time of the status line command (``hooks/statusline-command.sh``) in
debug mode (``LLM_ROUTER_STATUSLINE=fast``, the fast line; the default is the
full layout, which this bar does not apply to).

Pre-registered bar: p95 <= 100 ms over n=200 runs, on a COLD cache (no cache
file, no refresh stamp: every run also starts the detached refresher) and a WARM
one (fresh cache plus a usage.json quota snapshot), and with a refresher that hangs. Runs in an isolated HOME and
LLM_ROUTER_HOME; nothing of the operator's is read.

    python scripts/bench_statusline.py [--n 200]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")  # benchmark traffic is not production

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"
BAR_MS = 100.0


def _p(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[max(0, min(len(values) - 1, int(round(q * len(values))) - 1))]


def _run(env: dict) -> float:
    t0 = time.perf_counter()
    subprocess.run(["bash", str(SCRIPT)], input='{"session_id":"bench"}', env=env,
                   capture_output=True, text=True, timeout=30, check=True)
    return (time.perf_counter() - t0) * 1000.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    n = ap.parse_args().n
    home = Path(tempfile.mkdtemp(prefix="statusline-bench-"))
    state = home / ".llm-router"
    state.mkdir()
    env = {"HOME": str(home), "LLM_ROUTER_HOME": str(state), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "NO_COLOR": "1", "LLM_ROUTER_STATUSLINE_REFRESH_CMD": "true", "LLM_ROUTER_STATUSLINE": "fast"}
    cache = state / "statusline_cache.json"
    stamp = state / ".statusline-refresh.last"
    payload = {"v": 1, "mode": "smart", "ns": {"pct": 0.0, "n": 3599}, "claude_weekly_pct": 41.0,
               "codex": {"used": 7, "budget": 15, "resets_at": time.time() + 3600},
               "hooks_slow": {"hook": "auto-route", "p95_ms": 3002.3}}
    results = {}

    cold = []
    for _ in range(n):
        cache.unlink(missing_ok=True)
        stamp.unlink(missing_ok=True)
        cold.append(_run(env))
    results["cold (no cache; refresher started every run)"] = cold

    (state / "usage.json").write_text(json.dumps(
        {"session_pct": 12.4, "weekly_pct": 41.0, "sonnet_pct": 3.0, "updated_at": time.time()}))
    warm = []
    for _ in range(n):
        cache.write_text(json.dumps({**payload, "written_at": time.time()}))
        warm.append(_run(env))
    results["warm (fresh cache)"] = warm

    hang = f"{sys.executable} -c 'import time; time.sleep(60)'"
    env_hang = {**env, "LLM_ROUTER_STATUSLINE_REFRESH_CMD": hang}
    hung = []
    for _ in range(n):
        cache.unlink(missing_ok=True)
        stamp.unlink(missing_ok=True)
        hung.append(_run(env_hang))
    results["cold, refresher hangs 60 s"] = hung

    ok = True
    for name, vals in results.items():
        p50, p95, mx = _p(vals, 0.5), _p(vals, 0.95), max(vals)
        verdict = "PASS" if p95 <= BAR_MS else "FAIL"
        ok &= p95 <= BAR_MS
        print(f"{name:48s} n={len(vals)} p50={p50:6.1f}ms p95={p95:6.1f}ms max={mx:6.1f}ms  {verdict} (bar {BAR_MS:.0f}ms)")
    subprocess.run(["pkill", "-f", "time.sleep(60)"], check=False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
