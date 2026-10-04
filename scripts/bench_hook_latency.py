#!/usr/bin/env python3
"""What does the hook-latency recorder cost? -- the number behind KPI G1's guardrail.

``llm_router.hook_latency`` is on the path of every hook invocation, so its own
cost is the first thing it must be measured against. Two measurements:

``ab``     Real hooks, A/B, interleaved. Variant A is the hook scripts exactly as
           on ``--baseline`` (default ``origin/main``, which has no recorder);
           variant B is this working tree. Each round runs A then B (order
           shuffled) as a subprocess with a benign payload; the report is the
           median and mean wall time of each and the paired median difference.
           Each variant gets its own throwaway HOME and LLM_ROUTER_HOME and a
           minimal PATH, so nothing touches ~/.claude or ~/.llm-router.
``micro``  The recorder itself in fresh processes: the one-off import of
           ``llm_router.hook_latency`` (with ``llm_router`` already imported, as
           every hook has it), ``begin()``, the first write, and the warm write.

session-start is not run: it spawns detached background processes and can reach
the network, which a benchmark has no business triggering. Its overhead is the
same stanza measured on the other hooks.

    python scripts/bench_hook_latency.py ab --rounds 300 --hooks enforce-route,auto-route
    python scripts/bench_hook_latency.py micro --runs 40
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Every row this writes is benchmark traffic, not production (M-02).
os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

PAYLOADS: dict[str, dict] = {
    "enforce-route": {"session_id": "bench", "hook_event_name": "PreToolUse", "tool_name": "Read",
                      "tool_input": {"file_path": "/etc/hosts"}, "cwd": "/tmp"},
    "agent-route": {"session_id": "bench", "hook_event_name": "PreToolUse", "tool_name": "Agent",
                    "tool_input": {"subagent_type": "Explore", "description": "d", "prompt": "list files"},
                    "cwd": "/tmp"},
    "auto-route": {"session_id": "bench", "hook_event_name": "UserPromptSubmit", "prompt": "hi", "cwd": "/tmp"},
    "status-bar": {"session_id": "bench", "hook_event_name": "UserPromptSubmit", "prompt": "hi", "cwd": "/tmp"},
    "subagent-start": {"session_id": "bench", "hook_event_name": "SubagentStart",
                       "agent_type": "general-purpose", "agent_id": "a1", "cwd": "/tmp"},
    "usage-refresh": {"session_id": "bench", "hook_event_name": "PostToolUse",
                      "tool_name": "mcp__llm_router__llm", "tool_input": {},
                      "tool_response": {"content": [{"type": "text", "text": "ok"}]}, "cwd": "/tmp"},
    "cc-usage-track": {"session_id": "bench", "hook_event_name": "PostToolUse", "tool_name": "Agent",
                       "tool_input": {"subagent_type": "Explore"},
                       "tool_response": {"usage": {"input_tokens": 10, "output_tokens": 5}}, "cwd": "/tmp"},
    "agent-depth-release": {"session_id": "bench", "hook_event_name": "PostToolUse", "tool_name": "Agent",
                            "tool_input": {}, "tool_response": {}, "cwd": "/tmp"},
    "playwright-compress": {"session_id": "bench", "hook_event_name": "PostToolUse",
                            "tool_name": "mcp__playwright__browser_snapshot", "tool_input": {},
                            "tool_response": "short page", "cwd": "/tmp"},
    "bash-compress": {"session_id": "bench", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "ls"}, "tool_response": {"stdout": "a\nb\n", "stderr": ""},
                      "cwd": "/tmp"},
    "session-end": {"session_id": "bench", "hook_event_name": "Stop", "transcript_path": "/nonexistent",
                    "cwd": "/tmp"},
}

_MICRO_CHILD = r"""
import json, time
import llm_router                      # package init is paid by every hook anyway
t0 = time.perf_counter()
from llm_router import hook_latency as hl
t1 = time.perf_counter()
hl.begin("enforce-route", "PreToolUse", time.monotonic())
t2 = time.perf_counter()
hl._finish()                           # what atexit does: the first write
t3 = time.perf_counter()
warm = []
for _ in range(2000):
    a = time.perf_counter(); hl.record("enforce-route", "PreToolUse", 12.3); warm.append(time.perf_counter() - a)
warm.sort()
print(json.dumps({"import_us": (t1 - t0) * 1e6, "begin_us": (t2 - t1) * 1e6,
                  "first_write_us": (t3 - t2) * 1e6, "warm_median_us": warm[1000] * 1e6,
                  "warm_p95_us": warm[1900] * 1e6}))
"""


def _env(root: Path, tag: str) -> dict[str, str]:
    home = root / f"home_{tag}"
    (home / "state").mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "LLM_ROUTER_HOME": str(home / "state"), "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8", "LLM_ROUTER_ENFORCE": "smart",
            "LLM_ROUTER_SYNTHETIC": os.environ["LLM_ROUTER_SYNTHETIC"]}


def _run(script: Path, payload: dict, env: dict[str, str]) -> float:
    start = time.perf_counter()
    subprocess.run([sys.executable, str(script)], input=json.dumps(payload).encode(), env=env,
                   capture_output=True, timeout=120, check=False)
    return (time.perf_counter() - start) * 1000.0


def ab(rounds: int, hooks: list[str], baseline: str, control: bool) -> None:
    root = Path(tempfile.mkdtemp(prefix="hookbench-"))
    base, inst = root / "base", root / "inst"
    subprocess.run(f"git -C {REPO} archive {baseline} src/llm_router/hooks | tar -x -C {root}", shell=True, check=True)
    shutil.copytree(root / "src/llm_router/hooks", base)
    # --control: B is a second copy of the SAME baseline, so the paired difference
    # is the noise floor of this machine and method, not a measured cost.
    shutil.copytree(root / "src/llm_router/hooks" if control else REPO / "src/llm_router/hooks", inst)
    print(f"A = {baseline} (no recorder)   B = {'a second copy of A (CONTROL)' if control else 'working tree'}   "
          f"rounds={rounds}   python={sys.version.split()[0]}")
    for hook in hooks:
        payload = PAYLOADS[hook]
        env_a, env_b = _env(root, f"{hook}_a"), _env(root, f"{hook}_b")
        for _ in range(3):  # warm bytecode and page caches; discarded
            _run(base / f"{hook}.py", payload, env_a)
            _run(inst / f"{hook}.py", payload, env_b)
        a: list[float] = []
        b: list[float] = []
        for _ in range(rounds):
            order = [(base, env_a, a), (inst, env_b, b)]
            random.shuffle(order)
            for d, env, sink in order:
                sink.append(_run(d / f"{hook}.py", payload, env))
        log = Path(env_b["LLM_ROUTER_HOME"], "hook_latency.jsonl")
        recorded = [json.loads(x)["elapsed_ms"] for x in log.read_text().splitlines()] if log.exists() else []
        if not recorded:
            print(f"{hook:20s} A median {statistics.median(a):7.2f} ms  B median {statistics.median(b):7.2f} ms  "
                  f"paired median diff {statistics.median([y - x for x, y in zip(a, b)]):+6.2f} ms  "
                  f"mean diff {statistics.mean(b) - statistics.mean(a):+6.2f} ms  (no recorder rows)")
            continue
        print(f"{hook:20s} A median {statistics.median(a):7.2f} ms  B median {statistics.median(b):7.2f} ms  "
              f"paired median diff {statistics.median([y - x for x, y in zip(a, b)]):+6.2f} ms  "
              f"mean diff {statistics.mean(b) - statistics.mean(a):+6.2f} ms  "
              f"(recorded elapsed median {statistics.median(recorded):.1f} ms, {len(recorded)} rows; "
              f"wall minus recorded {statistics.median(b) - statistics.median(recorded):.1f} ms)")


def micro(runs: int) -> None:
    results = []
    for _ in range(runs):
        home = tempfile.mkdtemp(prefix="micro-")
        env = {"HOME": home, "LLM_ROUTER_HOME": home + "/state", "PATH": "/usr/bin:/bin",
               "LLM_ROUTER_SYNTHETIC": os.environ["LLM_ROUTER_SYNTHETIC"]}
        out = subprocess.run([sys.executable, "-c", _MICRO_CHILD], env=env, capture_output=True, text=True, timeout=60)
        out.check_returncode()
        results.append(json.loads(out.stdout))
    print(f"fresh processes: {runs}")
    for key in results[0]:
        values = sorted(r[key] for r in results)
        print(f"{key:16s} median {statistics.median(values):8.1f} us   p95 {values[int(len(values) * .95)]:8.1f} us")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("ab")
    a.add_argument("--rounds", type=int, default=100)
    a.add_argument("--hooks", default=",".join(PAYLOADS))
    a.add_argument("--baseline", default="origin/main")
    a.add_argument("--control", action="store_true", help="A/A: measure the noise floor, not the recorder")
    m = sub.add_parser("micro")
    m.add_argument("--runs", type=int, default=40)
    args = ap.parse_args()
    if args.mode == "ab":
        ab(args.rounds, args.hooks.split(","), args.baseline, args.control)
    else:
        micro(args.runs)


if __name__ == "__main__":
    main()
