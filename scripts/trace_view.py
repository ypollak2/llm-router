#!/usr/bin/env python3
"""Render an execution trace as a timeline you can actually read.

    LLM_ROUTER_TRACE=1 <run something>
    python3 scripts/trace_view.py                 # the whole trace
    python3 scripts/trace_view.py --run a1b2c3    # one run
    python3 scripts/trace_view.py --last          # the most recent run only
    python3 scripts/trace_view.py --verdict       # one line per run: did it work?

The verdict view exists because the question is usually not "what happened"
but "did the local model actually do the work, or did it chat and stop". The
loop's own return string cannot answer that — "Agent reached maximum
iterations" is emitted whether it made 40 useful tool calls or none.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def trace_file() -> Path:
    raw = os.environ.get("LLM_ROUTER_TRACE_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "trace.jsonl"


def load(path: Path) -> list[dict]:
    if not path.exists():
        print(f"no trace at {path} — run something with LLM_ROUTER_TRACE=1 first")
        raise SystemExit(1)
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


ICON = {
    "loop.start": "▶", "loop.end": "■",
    "llm.request": "→", "llm.response": "←", "llm.error": "✗",
    "tool.call": "  ⚙", "tool.result": "  ✓", "tool.repeat_refused": "  ↻",
    "task.start": "▶▶", "task.end": "■■",
}


def render(events: list[dict]) -> None:
    t0 = events[0]["ts"] if events else 0
    for e in events:
        ev = e.get("event", "?")
        icon = ICON.get(ev, " ")
        rel = e["ts"] - t0
        bits = []
        for k in ("model", "tool", "reason", "status", "iteration", "n_tool_calls",
                  "tools_used", "ms", "result_len", "check_passed", "error"):
            if k in e and e[k] not in (None, ""):
                bits.append(f"{k}={e[k]}")
        print(f"{rel:7.2f}s {icon:3s} {ev:26s} {' '.join(bits)}")
        for k in ("args", "result", "content", "objective", "prompt", "report"):
            if e.get(k):
                text = str(e[k]).replace("\n", "\\n")
                print(f"              {k}: {text[:180]}")


def verdicts(events: list[dict]) -> None:
    """One line per run: tool calls made, files touched, how it ended."""
    runs: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        runs[e.get("run", "?")].append(e)

    print(f"{'run':14s} {'tools':>5s} {'llm':>4s} {'end reason':32s} {'status':18s} verdict")
    print("-" * 100)
    for run, evs in runs.items():
        tools = sum(1 for e in evs if e["event"] == "tool.call")
        llm = sum(1 for e in evs if e["event"] == "llm.request")
        end = next((e for e in reversed(evs) if e["event"] == "loop.end"), {})
        task = next((e for e in reversed(evs) if e["event"] == "task.end"), {})
        reason = end.get("reason", "—")
        status = task.get("status", "—")
        # The judgement the loop's own return string cannot make.
        if tools == 0:
            verdict = "NEVER TOUCHED THE REPO — chatted only"
        elif reason == "max_iterations":
            verdict = "ran out of turns mid-task"
        elif reason == "budget_exhausted":
            verdict = "ran out of time mid-task"
        elif reason == "repeated_identical_call":
            verdict = "stuck repeating one call"
        elif reason == "llm_unreachable":
            verdict = "Ollama did not answer"
        elif task.get("check_passed") is True:
            verdict = "verified by an independent check"
        elif task.get("check_passed") is False:
            verdict = "did work; the check REJECTED it"
        else:
            verdict = "finished, unverified"
        print(f"{run:14s} {tools:5d} {llm:4d} {reason[:32]:32s} {status[:18]:18s} {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--last", action="store_true")
    ap.add_argument("--verdict", action="store_true")
    ap.add_argument("--file")
    args = ap.parse_args()

    path = Path(args.file).expanduser() if args.file else trace_file()
    events = load(path)
    if args.last:
        last_run = events[-1].get("run")
        events = [e for e in events if e.get("run") == last_run]
    elif args.run:
        events = [e for e in events if str(e.get("run", "")).startswith(args.run)]

    if not events:
        print("no matching events")
        return 1
    if args.verdict:
        verdicts(events)
    else:
        render(events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
