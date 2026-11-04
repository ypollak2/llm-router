#!/usr/bin/env python3
"""A cheap, real check of the hook-side routing verdict: at most 30 model calls, no Claude, no stubs.

It talks to the Ollama on this machine (``LLM_ROUTER_OLLAMA_URL`` or localhost:11434) and the decision model
(``LLM_ROUTER_DECISION_MODEL``, ``LLM_ROUTER_CLASSIFIER_BACKEND=systemone``). Run it with ``HOME`` and
``LLM_ROUTER_HOME`` pointed at a scratch directory so its rows never land in the real ledgers::

    HOME=/tmp/w10 LLM_ROUTER_HOME=/tmp/w10/.llm-router LLM_ROUTER_LOCAL_CLASSIFIER=shadow \\
      LLM_ROUTER_CLASSIFIER_BACKEND=systemone LLM_ROUTER_DECISION_MODEL=nimble:9b \\
      python scripts/door_verdict_check.py

Three phases over one fixed set of ten synthetic prompts (no owner text):

* A  BEFORE, the proxy path (``local_classifier.classify_async``): the model is unloaded first, then three prompts
     arrive 0.5 s apart. The proxy path never waits for a load, so it answers ``cold`` (no verdict) and starts a
     background warm-up. 1 call (the warm-up).
* B  AFTER, the hook path (``door_verdict.classify``): the model is unloaded first, then the ten prompts run in
     order. The first waits through the load; the rest are warm. 10 calls.
* C  AFTER with the session-start preload (``door_verdict.warm``): unload, warm, then one prompt. 2 calls.

The call counter is hard: a phase that would pass ``MAX_CALLS`` is refused. Unloading uses ``ollama stop``, which
is not a model call. Prints one JSON object: per-phase coverage, latency (ms) and residency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time

MAX_CALLS = 30

# Ten synthetic prompts: easy, mid, hard, ambiguous, agent briefs. Fixed so a rerun is comparable.
PROMPTS: list[tuple[str, str]] = [
    ("prompt", "What does os.path.join do on Windows when the second argument is absolute?"),
    ("prompt", "Rename the variable tmp to buffer in utils.py"),
    ("prompt", "Redesign the session store so that tokens rotate without logging every user out, and plan the migration"),
    ("prompt", "Summarise what this error means: ConnectionResetError: [Errno 54] Connection reset by peer"),
    ("agent", "Read src/pager.py and explain why the last row is dropped, with line numbers"),
    ("agent", "Implement an idempotency key for the payment webhook handler, update the schema and write migration tests"),
    ("prompt", "Write a commit message for the diff I just made to the retry logic"),
    ("agent", "Search the repository for every use of the deprecated flag --legacy-auth and list the files"),
    ("prompt", "Why is the nightly job failing since Tuesday? The logs show nothing useful."),
    ("prompt", "Format the README tables"),
]


def _unload(model: str) -> None:
    subprocess.run(["ollama", "stop", model], capture_output=True, timeout=30, check=False)
    time.sleep(0.5)


def _conditions() -> dict:
    """What else was using the machine: a number from one run means little without it."""
    ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10, check=False).stdout
    return {"loadavg": [round(x, 2) for x in os.getloadavg()],
            "ollama_resident_at_start": [ln.split()[0] for ln in ps.splitlines()[1:] if ln.strip()]}


def _reset_breaker() -> None:
    """Each phase starts with a closed breaker, as a fresh process would find it after a quiet minute."""
    from llm_router import door_verdict as dv

    dv._write_state({"fails": 0, "last_fail": 0.0, "open_until": 0.0})


def _pct(v: list[float], p: float) -> float | None:
    if not v:
        return None
    s = sorted(v)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", type=int, default=len(PROMPTS), help="how many of the ten fixed prompts phase B runs")
    ap.add_argument("--skip-a", action="store_true", help="skip phase A (the proxy path, 1 call)")
    args = ap.parse_args()
    prompts = PROMPTS[: max(1, min(args.prompts, len(PROMPTS)))]
    from llm_router import door_verdict as dv
    from llm_router import local_classifier as lc

    import pwd

    real_state = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".llm-router")  # not $HOME: that is the scratch dir
    if os.environ.get("LLM_ROUTER_HOME", "") in ("", real_state):
        print("refusing to run against the real state directory; set HOME and LLM_ROUTER_HOME to a scratch dir",
              file=sys.stderr)
        return 2
    model = lc._model()
    if dv.mode() == "off" or not model:
        print("set LLM_ROUTER_LOCAL_CLASSIFIER=shadow and LLM_ROUTER_DECISION_MODEL", file=sys.stderr)
        return 2
    calls = 0

    def spend(n: int) -> None:
        nonlocal calls
        if calls + n > MAX_CALLS:
            raise SystemExit(f"call cap {MAX_CALLS} would be exceeded ({calls} + {n})")
        calls += n

    out: dict = {"model": model, "n_prompts": len(prompts), "max_calls": MAX_CALLS, "conditions": _conditions()}

    # A. the proxy path, cold
    if not args.skip_a:
        _unload(model)
        spend(1)

        async def proxy_path() -> list[dict]:
            rows = []
            lc._reset_state()
            for i, (_kind, text) in enumerate(PROMPTS[:3]):
                v = await lc.classify_async(lc.Assembled("(no context)", text), session_id="chk",
                                            text_sha=f"a{i}", timeout_s=2.0)
                rows.append({"source": v.source, "tier": v.tier, "ms": v.ms})
                await asyncio.sleep(0.5)
            await asyncio.sleep(4.0)  # let the background warm-up finish so the model is left as it was
            await lc.aclose()
            return rows

        a = asyncio.run(proxy_path())
        out["A_proxy_path_after_eviction"] = {
            "n": len(a), "verdicts": sum(1 for r in a if r["source"] in ("llm", "cache")), "rows": a}

    # B. the hook path, cold then warm
    _unload(model)
    _reset_breaker()
    b = []
    for kind, text in prompts:
        spend(1)
        v = dv.classify(text, kind)
        b.append({"kind": kind, "source": v.source, "tier": v.tier, "conf": None if v.confidence is None else
                  round(v.confidence, 3), "ms": v.ms, "resident": v.resident})
    ok = [r for r in b if r["source"] in ("llm", "cache")]
    warm = [r["ms"] for r in b[1:] if r["source"] == "llm"]
    out["B_hook_path_after_eviction"] = {
        "n": len(b), "verdicts": len(ok), "first_call_ms": b[0]["ms"], "first_resident": b[0]["resident"],
        "warm_n": len(warm), "warm_p50_ms": _pct(warm, 0.5), "warm_p95_ms": _pct(warm, 0.95),
        "warm_max_ms": max(warm) if warm else None, "tiers": {t: sum(1 for r in ok if r["tier"] == t)
                                                              for t in ("haiku", "sonnet", "opus")},
        "median_conf": statistics.median([r["conf"] for r in ok]) if ok else None, "rows": b}

    # C. the session-start preload, then the first prompt
    _unload(model)
    _reset_breaker()
    spend(2)
    t0 = time.perf_counter()
    warmed = dv.warm()
    warm_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    v = dv.classify(prompts[0][1], prompts[0][0])
    out["C_preload_then_first_prompt"] = {
        "warmed": warmed, "warm_ms": warm_ms, "first_prompt_source": v.source, "first_prompt_ms": v.ms,
        "first_prompt_resident": v.resident}

    out["calls_used"] = calls
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
