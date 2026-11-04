#!/usr/bin/env python3
"""Does a log-only (shadow) routing verdict slow the NEXT local text-job call? Measured with the real models.

The live local text-job lane runs a commit message on one model (``local_routing.json``, ``--lane-model``). Ollama
here keeps a single big model resident, so a shadow verdict that loads the decision model would evict the lane's model
and the next commit message would pay a cold load. For each run this script:

1. loads the lane model (``/api/generate`` with no prompt: a load, not a text job) and checks it is resident;
2. runs ONE prompt through the real ``auto-route.py`` hook in ``shadow`` mode (a copy of the hooks directory taken from
   ``--src``, whose ``usage-refresh.py`` is a no-op) and waits for the detached worker to finish;
3. lists the resident models (``/api/ps``);
4. times one commit-message call to the lane model: the router's own prompt builder
   (``text_jobs.build_commit_message_prompt``) on a small fixed diff, sent straight to Ollama. This is the same model
   and prompt as the lane, not the bound lane itself (no router gates, no acceptance check).

Run it once per source tree (``--src`` = a ``src`` directory, e.g. the tree before a fix and the tree after) with
``PYTHONPATH`` set to the same directory. Every run costs one lane-model load, one lane-model call and, when the tree
classifies, one decision-model call; ``--max-calls`` (default 12) caps the model calls and refuses a plan above it.
HOME and LLM_ROUTER_HOME must be scratch directories::

    HOME=/tmp/w10 LLM_ROUTER_HOME=/tmp/w10/.llm-router PYTHONPATH=<tree>/src \\
      python scripts/door_verdict_lane_effect.py --src <tree>/src --workdir /tmp/w10/lane --label after
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DIFF = """diff --git a/src/retry.py b/src/retry.py
--- a/src/retry.py
+++ b/src/retry.py
@@ -10,7 +10,9 @@ def backoff(attempt, base=0.5, cap=30.0):
-    return min(cap, base * 2 ** attempt)
+    delay = min(cap, base * 2 ** attempt)
+    return delay * (0.5 + random.random() / 2)  # jitter, so retries do not arrive together
"""
BASE = "http://localhost:11434"   # replaced by --ollama-url
PROMPT = "What does os.path.join do on Windows when the second argument is absolute?"


def _post(path: str, body: dict, timeout: float = 300.0) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - local URL
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def _ps() -> list[str]:
    with urllib.request.urlopen(f"{BASE}/api/ps", timeout=5) as r:  # noqa: S310 - local URL
        return [m.get("name", "") for m in json.loads(r.read()).get("models", [])]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--lane-model", default="qwen3.6:35b-a3b-coding")
    ap.add_argument("--decision-model", default="nimble:9b")
    ap.add_argument("--max-calls", type=int, default=16)
    ap.add_argument("--classifies", action="store_true", help="this tree classifies in shadow (counts one decision-model call per run)")
    ap.add_argument("--no-prompt", action="store_true", help="the control: no hook run, only the lane model's call")
    ap.add_argument("--ollama-url", default="http://localhost:11434",
                    help="the Ollama to measure on. A shared Ollama is evicted by its other users, so a measurement of "
                         "what OUR verdict does needs a single-slot server of its own (OLLAMA_MAX_LOADED_MODELS=1)")
    args = ap.parse_args()

    real_state = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".llm-router")
    state = Path(os.environ.get("LLM_ROUTER_HOME", ""))
    if str(state) in ("", ".", real_state):
        print("refusing to run against the real state directory; set HOME and LLM_ROUTER_HOME to scratch dirs",
              file=sys.stderr)
        return 2
    planned = args.runs * (2 + (1 if args.classifies and not args.no_prompt else 0))   # more if a load is repeated
    if planned > args.max_calls:
        print(f"plan needs {planned} model calls (load + text job per run, plus the verdict), cap is {args.max_calls}",
              file=sys.stderr)
        return 2

    global BASE
    BASE = args.ollama_url.rstrip("/")
    ollama_env = dict(os.environ, OLLAMA_HOST=BASE.split("://", 1)[-1])
    sys.path.insert(0, str(args.src))
    from llm_router import text_jobs

    system, user = text_jobs.build_commit_message_prompt(DIFF)
    work = args.workdir
    hooks = work / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    shutil.copytree(args.src / "llm_router" / "hooks", hooks)
    os.truncate(hooks / "usage-refresh.py", 0)
    state.mkdir(parents=True, exist_ok=True)
    ledger = state / "door_verdicts.jsonl"

    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_ROUTER_", "CLAUDE_CODE_", "CODEX_"))}
    env.update({
        "HOME": os.environ["HOME"], "LLM_ROUTER_HOME": str(state), "PYTHONPATH": str(args.src),
        "LLM_ROUTER_QA_ROUTING": "on", "LLM_ROUTER_ENFORCE": "suggest", "LLM_ROUTER_DIRECT_EXECUTION": "0",
        "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1", "LLM_ROUTER_MAP_REDISCOVERY": "off",
        "LLM_ROUTER_LOCAL_CLASSIFIER": "shadow", "LLM_ROUTER_CLASSIFIER_BACKEND": "systemone",
        "LLM_ROUTER_DECISION_MODEL": args.decision_model, "LLM_ROUTER_CLASSIFIER_KEEP_ALIVE": "10m",
        "LLM_ROUTER_OLLAMA_URL": BASE,
    })

    def rows() -> list[dict]:
        try:
            return [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
        except OSError:
            return []

    results = []
    calls = 0
    for i in range(args.runs):
        subprocess.run(["ollama", "stop", args.decision_model], capture_output=True, check=False, env=ollama_env)
        attempts = 0
        before_ps: list[str] = []
        while attempts < 3 and args.lane_model not in before_ps:   # another process on this Ollama may evict it: reload
            attempts += 1
            if calls >= args.max_calls:
                raise SystemExit(f"call cap {args.max_calls} reached while loading the lane model")
            # a load, not a text job, with the SAME num_ctx as the call below: a load at the server default context
            # makes the call reload the runner (the artefact of this script's first version)
            _post("/api/generate", {"model": args.lane_model, "keep_alive": "10m", "options": {"num_ctx": 8192}})
            calls += 1
            time.sleep(0.5)
            before_ps = _ps()
        n0 = len([r for r in rows() if r.get("outcome") == "background_result"])
        session = f"lane-{args.label}-{i}-{os.getpid()}"
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": f"{PROMPT} (run {i})",
                              "session_id": session, "cwd": str(work)})
        t0 = time.perf_counter()
        if not args.no_prompt:
            subprocess.run([sys.executable, str(hooks / "auto-route.py")], input=payload, capture_output=True,
                           text=True, env=env, cwd=str(work), timeout=120)
        hook_ms = (time.perf_counter() - t0) * 1000.0
        deadline = time.monotonic() + (0 if args.no_prompt else 40)
        while time.monotonic() < deadline and len(
                [r for r in rows() if r.get("outcome") == "background_result"]) <= n0:
            time.sleep(0.2)
        bg = [r for r in rows() if r.get("outcome") == "background_result"][n0:]
        if args.classifies and bg and bg[-1].get("source") == "llm":
            calls += 1
        after_ps = _ps()
        t1 = time.perf_counter()
        resp = _post("/api/chat", {"model": args.lane_model, "stream": False, "think": False, "keep_alive": "10m",
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}],
                                   "options": {"temperature": 0, "num_predict": 120, "num_ctx": 8192}})
        job_ms = (time.perf_counter() - t1) * 1000.0
        calls += 1
        results.append({
            "run": i, "hook_ms": round(hook_ms, 1), "resident_before_prompt": before_ps, "load_attempts": attempts,
            "valid": args.lane_model in before_ps,
            "verdict_source": bg[-1].get("source") if bg else None, "verdict_ms": bg[-1].get("ms") if bg else None,
            "resident_after_prompt": after_ps, "lane_model_still_resident": args.lane_model in after_ps,
            "text_job_ms": round(job_ms, 1), "text_job_ok": bool((resp.get("message") or {}).get("content", "").strip()),
            "text_job_load_ms": round((resp.get("load_duration") or 0) / 1e6, 1),
        })
        if calls > args.max_calls:
            raise SystemExit(f"call cap {args.max_calls} passed")
    jobs = sorted(r["text_job_ms"] for r in results)
    print(json.dumps({
        "label": args.label, "src": str(args.src), "lane_model": args.lane_model, "model_calls": calls,
        "loadavg": [round(x, 2) for x in os.getloadavg()], "text_job_ms_sorted": jobs, "valid_runs": sum(1 for r in results if r["valid"]), "runs": results,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
