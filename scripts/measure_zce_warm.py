#!/usr/bin/env python3
"""Live measurement for plan 3.7: serve rate of zero-Claude local edits by model state.

Drives `zero_claude_edit.maybe_replace` (the exact function the UserPromptSubmit
hook calls) against COPIES of the edit fixtures, under the hook's real ~37 s
deadline, and counts what happened to each call.

States (what the model looks like when the prompt arrives):
  cold         `ollama stop <model>` before every call: nothing resident.
  legacy-warm  resident, loaded the way the OLD session-start warm-up loaded it
               (bare /api/generate: server-default context, 5 min keep-alive).
               The ZCE call asks for a different num_ctx, so Ollama reloads.
  warm         resident with the options ZCE sends (num_ctx, keep_alive=-1):
               what the new session-start warm-up produces.

Outcomes per call:
  served     an edit was applied and written to the temp copy
  fail-fast  fell through to Claude in well under the deadline, citing a cold model
  timeout    blocked: "no response" / out of time budget (the deadline was burned)
  other      anything else (reason printed)

Usage (from a worktree, with Ollama up and nothing else using it):

    ollama ps                       # must be empty or only your model
    uv run python scripts/measure_zce_warm.py --n 5 --states cold,legacy-warm,warm \\
        --out /tmp/zce_measure.jsonl

Fixtures are read from --fixtures (default: the routing-experiment-2026-10-01
fixtures) and COPIED; the originals are never modified. State goes to a
throwaway LLM_ROUTER_HOME, never ~/.llm-router.

This script puts real load on the local Ollama server. Do not run it while
another experiment is using it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DEFAULT_FIXTURES = Path.home() / ".rsi/research/routing-experiment-2026-10-01/fixtures"
HOOK_BUDGET_S = 37.0  # ~ _readonly_draft_deadline(): 55 s hook budget minus fallback reserve


def _ollama_url() -> str:
    return (os.environ.get("LLM_ROUTER_OLLAMA_URL") or os.environ.get("OLLAMA_BASE_URL")
            or "http://localhost:11434").rstrip("/")


def _post(path: str, payload: dict, timeout: float = 120.0) -> None:
    req = urllib.request.Request(
        _ollama_url() + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def _stop(model: str) -> None:
    subprocess.run(["ollama", "stop", model], capture_output=True, timeout=60)
    for _ in range(30):  # wait until /api/ps no longer lists it
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout
        if model.split(":")[0] not in out:
            return
        time.sleep(1)


def _prime_legacy(model: str) -> None:
    """Exactly the old session-start warm-up payload."""
    _post("/api/generate", {"model": model, "prompt": " ", "stream": False})


def _prime_new(model: str) -> None:
    """What the new warm-up sends: same num_ctx + keep_alive as the ZCE call."""
    from llm_router import warm
    _post("/api/generate", warm.warmup_payload(model))


def _init_repo(path: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "m", "GIT_AUTHOR_EMAIL": "m@x",
           "GIT_COMMITTER_NAME": "m", "GIT_COMMITTER_EMAIL": "m@x"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=path, check=True, capture_output=True, env=env)


def _classify(outcome, elapsed: float) -> tuple[str, str]:
    if outcome is None:
        return "other", "maybe_replace returned None (scope not enabled?)"
    reason = outcome.log_reason or ""
    if outcome.applied:
        return "served", reason
    low = reason.lower()
    if outcome.action == "fallthrough" and "cold" in low:
        return "fail-fast", reason
    if outcome.action == "block" and ("no response" in low or "time budget" in low):
        return "timeout", reason
    return "other", reason


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    ap.add_argument("--n", type=int, default=5, help="fixtures per state (first n of the manifest)")
    ap.add_argument("--states", default="cold,legacy-warm,warm")
    ap.add_argument("--budget", type=float, default=HOOK_BUDGET_S)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    manifest = json.loads((args.fixtures / "manifest.json").read_text())
    work = Path(tempfile.mkdtemp(prefix="zce-measure-"))
    os.environ["LLM_ROUTER_HOME"] = str(work / "home")
    os.environ["LLM_ROUTER_ZERO_CLAUDE_SCOPE"] = "edit"
    os.environ.pop("LLM_ROUTER_OLLAMA_MODEL", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from llm_router import zero_claude_edit as zce

    model = zce.edit_model()
    print(f"model={model} budget={args.budget}s n={args.n}/state out={args.out}", flush=True)
    rows = []
    for state in [s.strip() for s in args.states.split(",") if s.strip()]:
        for entry in manifest[: args.n]:
            repo = work / f"{state}-{entry['idx']:02d}"
            shutil.copytree(entry["path"], repo, ignore=shutil.ignore_patterns("__pycache__", ".git"))
            _init_repo(repo)
            if state == "cold":
                _stop(model)
            elif state == "legacy-warm":
                _stop(model)
                _prime_legacy(model)
            elif state == "warm":
                _stop(model)
                _prime_new(model)
            else:
                raise SystemExit(f"unknown state {state!r}")
            prompt = f"fix {entry['func']} in {entry['module']}: {entry['summary']}"
            t0 = time.monotonic()
            outcome = zce.maybe_replace(prompt=prompt, cwd=str(repo), deadline_s=t0 + args.budget)
            elapsed = time.monotonic() - t0
            label, reason = _classify(outcome, elapsed)
            test_ok = None
            if label == "served":
                r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", entry["test"]],
                                   cwd=repo, capture_output=True, text=True, timeout=60)
                test_ok = r.returncode == 0
            row = {"state": state, "idx": entry["idx"], "name": entry["name"], "outcome": label,
                   "elapsed_s": round(elapsed, 2), "fixture_test_passes": test_ok, "reason": reason[:200]}
            rows.append(row)
            print(json.dumps(row), flush=True)
    args.out.write_text("".join(json.dumps(r) + "\n" for r in rows))

    print("\nstate        n  served(test ok)  fail-fast  timeout  other   latency p50 / max (s)")
    for state in dict.fromkeys(r["state"] for r in rows):
        sub = [r for r in rows if r["state"] == state]
        c = lambda k: sum(1 for r in sub if r["outcome"] == k)  # noqa: E731
        ok = sum(1 for r in sub if r["fixture_test_passes"])
        lat = sorted(r["elapsed_s"] for r in sub)
        print(f"{state:<12} {len(sub):<2} {c('served'):>3} ({ok})         {c('fail-fast'):>5}  "
              f"{c('timeout'):>7}  {c('other'):>5}   {lat[len(lat) // 2]:.1f} / {lat[-1]:.1f}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
