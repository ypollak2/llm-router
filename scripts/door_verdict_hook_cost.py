#!/usr/bin/env python3
"""What the routing verdict adds to ONE prompt of the real ``auto-route.py`` hook, measured with the real model.

Runs the hook script as a subprocess for a fixed set of synthetic prompts, in conditions that differ only in
``LLM_ROUTER_LOCAL_CLASSIFIER`` and in whether the decision model is resident, and prints one JSON object:

* ``off``          the hook with the classifier off: the baseline wall time (0 model calls);
* ``shadow_warm``  shadow, model resident (1 call each, made by a detached worker);
* ``shadow_cold``  shadow, ``ollama stop`` first (1 call each: the worker pays the load);
* ``on_warm``      on, model resident (1 call each, made inline and joined before the decision);
* ``on_cold``      on, ``ollama stop`` first (1 call each: the prompt defers, the worker pays the load).

Per condition: n, hook wall ms (p50, p95, max), ``added`` ms (wall minus the ``off`` p50), and the hook's own
``verdict_wait`` phase from ``hook_latency.jsonl``. A hard cap (``--max-calls``, default 20) counts every model call
and refuses a run that would pass it. Unloading uses ``ollama stop``, which is not a model call. No Claude, no
generator of load: the current load average and the models resident at the start are printed with the result,
because a cold load costs 2 s on an idle machine and 5-10 s on a busy one (docs/bugs/CLS-COLD-1.md).

The hook is run from a COPY of the hooks directory whose ``usage-refresh.py`` is a no-op, because with detached
children allowed the hook would start the real one (a keychain read and an OAuth call). Run with ``HOME`` and
``LLM_ROUTER_HOME`` on scratch directories::

    HOME=/tmp/w10 LLM_ROUTER_HOME=/tmp/w10/.llm-router CODEX_HOME=/tmp/w10/.codex \\
      python scripts/door_verdict_hook_cost.py --workdir /tmp/w10/cost
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROMPTS = [
    "What does os.path.join do on Windows when the second argument is absolute?",
    "Explain what a foreign key is in SQL and when to avoid one.",
    "Summarise the difference between a mutex and a semaphore in two paragraphs.",
    "Compare Redis Streams and RabbitMQ for background jobs in a small team.",
    "Draft a short launch email for our new pricing page.",
    "What is the quick definition of idempotency in an HTTP API?",
    "Brainstorm three taglines for a coffee delivery app.",
    "Explain how database indexes work and what a covering index is.",
    "Analyze the pros and cons of microservices versus a monolith for ten engineers.",
    "Write a short note on why retries need jitter.",
]


def _pct(v: list[float], p: float) -> float | None:
    if not v:
        return None
    s = sorted(v)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def _resident(model: str) -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=2) as r:  # noqa: S310 - local URL
            return any(m.get("name", "").startswith(model.split(":")[0]) for m in json.loads(r.read()).get("models", []))
    except Exception:  # noqa: BLE001
        return False


def _wait(pred, timeout: float, step: float = 0.1) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--max-calls", type=int, default=20)
    ap.add_argument("--off", type=int, default=10)
    ap.add_argument("--shadow-warm", type=int, default=6)
    ap.add_argument("--shadow-cold", type=int, default=4)
    ap.add_argument("--on-warm", type=int, default=6)
    ap.add_argument("--on-cold", type=int, default=4)
    args = ap.parse_args()

    real_state = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".llm-router")
    state = Path(os.environ.get("LLM_ROUTER_HOME", ""))
    if str(state) in ("", ".", real_state):
        print("refusing to run against the real state directory; set HOME and LLM_ROUTER_HOME to scratch dirs",
              file=sys.stderr)
        return 2
    model = os.environ.get("LLM_ROUTER_DECISION_MODEL", "nimble:9b")
    plan = {"off": args.off, "shadow_cold": args.shadow_cold, "shadow_warm": args.shadow_warm,
            "on_cold": args.on_cold, "on_warm": args.on_warm}
    calls_planned = sum(plan[k] for k in plan if k != "off")
    if calls_planned > args.max_calls:
        print(f"plan needs {calls_planned} model calls, cap is {args.max_calls}", file=sys.stderr)
        return 2

    work = args.workdir
    hooks = work / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    shutil.copytree(REPO / "src" / "llm_router" / "hooks", hooks)
    os.truncate(hooks / "usage-refresh.py", 0)  # an empty script: the hook's detached refresh does nothing
    hook = hooks / "auto-route.py"
    state.mkdir(parents=True, exist_ok=True)
    ledger = state / "door_verdicts.jsonl"
    latency = state / "hook_latency.jsonl"

    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_ROUTER_", "CLAUDE_CODE_", "CODEX_"))}
    base_env.update({
        "HOME": os.environ["HOME"], "LLM_ROUTER_HOME": str(state), "LLM_ROUTER_QA_ROUTING": "on",
        "LLM_ROUTER_ENFORCE": "suggest", "LLM_ROUTER_DIRECT_EXECUTION": "0", "LLM_ROUTER_DISABLE_LLM_CLASSIFIERS": "1",
        "LLM_ROUTER_MAP_REDISCOVERY": "off", "LLM_ROUTER_CLASSIFIER_BACKEND": "systemone",
        "LLM_ROUTER_DECISION_MODEL": model, "LLM_ROUTER_CLASSIFIER_KEEP_ALIVE": "10m",
    })

    def rows() -> list[dict]:
        try:
            return [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
        except OSError:
            return []

    def hook_phase(session: str) -> dict:
        try:
            for line in reversed(latency.read_text().splitlines()):
                r = json.loads(line)
                if r.get("hook") == "auto-route" and r.get("session_id") == session:
                    return r
        except OSError:
            pass
        return {}

    def run(mode: str, i: int) -> dict:
        env = dict(base_env)
        if mode != "off":
            env["LLM_ROUTER_LOCAL_CLASSIFIER"] = mode
        else:
            env["LLM_ROUTER_LOCAL_CLASSIFIER"] = "off"
        session = f"cost-{mode}-{i}-{os.getpid()}"
        prompt = f"{PROMPTS[i % len(PROMPTS)]} (run {mode} {i})"
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt, "session_id": session,
                              "cwd": str(work)})
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, str(hook)], input=payload, capture_output=True, text=True, env=env,
                              cwd=str(work), timeout=120)
        wall = (time.perf_counter() - t0) * 1000.0
        lat = hook_phase(session)
        return {"wall_ms": round(wall, 1), "rc": proc.returncode, "hook_total_ms": lat.get("elapsed_ms"),
                "verdict_wait_ms": (lat.get("phases_ms") or {}).get("verdict_wait"), "session": session}

    out: dict = {"model": model, "conditions_at_start": {
        "loadavg": [round(x, 2) for x in os.getloadavg()],
        "ollama_resident": [m for m in subprocess.run(["ollama", "ps"], capture_output=True, text=True,
                                                      check=False).stdout.split() if ":" in m and "/" not in m][:4]},
        "plan": plan, "model_calls_planned": calls_planned}
    res: dict[str, list[dict]] = {k: [] for k in plan}
    calls = 0

    def spend() -> None:
        nonlocal calls
        if calls + 1 > args.max_calls:
            raise SystemExit(f"call cap {args.max_calls} reached")
        calls += 1

    # baseline: no classifier, no model call
    for i in range(plan["off"]):
        res["off"].append(run("off", i))
    # shadow, cold then warm
    for i in range(plan["shadow_cold"]):
        subprocess.run(["ollama", "stop", model], capture_output=True, check=False)
        time.sleep(0.5)
        spend()
        n0 = len(rows())
        r = run("shadow", i)
        r["resident_before"] = False
        res["shadow_cold"].append(r)
        _wait(lambda n0=n0: len([x for x in rows()[n0:] if x.get("outcome") == "background_result"]) >= 1, 60.0)
    for i in range(plan["shadow_warm"]):
        _wait(lambda: _resident(model), 30.0)
        spend()
        n0 = len(rows())
        r = run("shadow", 100 + i)
        r["resident_before"] = True
        res["shadow_warm"].append(r)
        _wait(lambda n0=n0: len([x for x in rows()[n0:] if x.get("outcome") == "background_result"]) >= 1, 60.0)
    # on, cold then warm
    for i in range(plan["on_cold"]):
        subprocess.run(["ollama", "stop", model], capture_output=True, check=False)
        time.sleep(0.5)
        spend()
        n0 = len(rows())
        r = run("on", 200 + i)
        r["resident_before"] = False
        res["on_cold"].append(r)
        _wait(lambda n0=n0: len([x for x in rows()[n0:] if x.get("outcome") == "background_result"]) >= 1, 60.0)
    for i in range(plan["on_warm"]):
        _wait(lambda: _resident(model), 30.0)
        spend()
        r = run("on", 300 + i)
        r["resident_before"] = True
        res["on_warm"].append(r)

    base = statistics.median([r["wall_ms"] for r in res["off"]]) if res["off"] else None
    summary = {}
    for k, v in res.items():
        w = [r["wall_ms"] for r in v]
        vw = [r["verdict_wait_ms"] for r in v if isinstance(r.get("verdict_wait_ms"), (int, float))]
        added = [x - base for x in w] if base is not None and k != "off" else []
        summary[k] = {
            "n": len(v), "ok": sum(1 for r in v if r["rc"] == 0),
            "wall_ms": {"p50": _pct(w, 0.5), "p95": _pct(w, 0.95), "max": max(w) if w else None},
            "added_ms": {"p50": _pct(added, 0.5), "p95": _pct(added, 0.95), "max": round(max(added), 1) if added else None},
            "verdict_wait_ms": {"n": len(vw), "p50": _pct(vw, 0.5), "p95": _pct(vw, 0.95),
                                "max": max(vw) if vw else None},
        }
    out.update({"baseline_off_p50_ms": base, "model_calls": calls, "summary": summary, "raw": res,
                "door_rows": len(rows())})
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
