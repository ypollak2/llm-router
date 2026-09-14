#!/usr/bin/env python3
"""Would Claude actually USE these drafts? Measured on real prompts.

The draft use rate has been 0 for every window ever measured, and the obvious
explanations — wrong local model, no repo context — were both real and are both
now fixed. This asks whether fixing them changes the outcome.

The corpus is the user's OWN prompts, recovered from Claude Code session
transcripts. Inventing four questions (the first attempt at this) measured the
questions, not the system.

ACCEPTANCE IS SCORED MECHANICALLY, never by a model judging a model. The rules
are the four reasons drafts were actually discarded, taken from a hand
classification of 101 real injections:

    37.6%  asked the user a question instead of answering
    32.7%  asserted a status it could not observe ("all tests passed")
    25.7%  generic advice with no reference to the repo
     4.0%  cited files that do not exist

The first three are string-detectable. The fourth is `grounding.grounding_violations`,
which is already in the repo and checks the one claim settleable without another
model: does the cited file exist.

A draft that passes all four is one Claude could relay without telling the user
something false. That is not proof it is CORRECT — no mechanical check can give
that — and the report says so.

    python3 scripts/bench_draft_acceptance.py --n 30 --model qwen3.8:latest --okf on
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

HOOK = ROOT / "src/llm_router/hooks/auto-route.py"
PY = str(ROOT / ".venv/bin/python")

ASKS_BACK = re.compile(
    r"would you like me to|shall i |should i |let me know if|please confirm|"
    r"do you want me to|would you prefer", re.I)
ASSERTS_STATUS = re.compile(
    r"all tests? (pass|passed)|completed successfully|no further action|"
    r"has (been )?(completed|finished) successfully|✅|task .* (complete|done)", re.I)


def draft_body(stdout: str) -> str | None:
    try:
        payload = json.loads(stdout)
    except Exception:                                        # noqa: BLE001
        return None
    blob = json.dumps(payload)
    if "UNVERIFIED DRAFT" not in blob:
        return None
    body = blob.split("UNVERIFIED DRAFT")[1].split("END UNVERIFIED DRAFT")[0]
    return body.replace("(no context — verify or discard)", " ")


def verdict(body: str, prompt: str) -> tuple[bool, str]:
    """(acceptable, reason-if-not)."""
    if ASKS_BACK.search(body):
        return False, "asks the user a question instead of answering"
    if ASSERTS_STATUS.search(body):
        return False, "asserts a status it cannot observe"
    try:
        from llm_router.grounding import grounding_violations
        bad = grounding_violations(body, "", prompt)
        if bad:
            return False, f"cites files that do not exist: {bad[:2]}"
    except Exception:                                        # noqa: BLE001
        pass
    if len(body.strip()) < 80:
        return False, "too short to be an answer"
    return True, ""


DEBUG_LOG = Path(
    os.environ.get("LLM_ROUTER_HOME", "").strip() or (Path.home() / ".llm-router")
).expanduser() / "auto-route-debug.log"


def _log_size() -> int:
    try:
        return DEBUG_LOG.stat().st_size
    except OSError:
        return 0


def _skips_since(offset: int) -> list[str]:
    """Why the hook abandoned each model in the chain, for this invocation only.

    Without this a missing draft is indistinguishable from a slow one, which is
    exactly the confusion that made three runs of one config spread 17%-53%.
    """
    try:
        with DEBUG_LOG.open() as fh:
            fh.seek(offset)
            tail = fh.read()
    except OSError:
        return []
    return [ln.split("DIRECT MODEL SKIPPED:", 1)[1].strip()
            for ln in tail.splitlines() if "DIRECT MODEL SKIPPED:" in ln]


def preflight(model: str) -> None:
    """Refuse to benchmark a model this machine does not have.

    A run pinned to `qwen3.5:latest` — not installed here — was reported as the
    "old configuration" baseline and compared against a new one. Ollama answers a
    missing model with a 404 the hook swallows, so the run produced numbers and no
    error. An absent model must stop the benchmark, not quietly become its result.
    """
    if model == "auto":
        return
    sys.path.insert(0, str(ROOT / "src"))
    from llm_router.hooks.direct_executor import available_ollama_models

    installed = available_ollama_models(timeout=5.0)
    if installed is None:
        raise SystemExit("ollama is not reachable — cannot verify the model, refusing to measure")
    if model not in installed and model.split(":")[0] not in {m.split(":")[0] for m in installed}:
        raise SystemExit(
            f"{model} is not installed (have: {', '.join(sorted(installed)) or 'nothing'}).\n"
            "Pull it or pass --model auto to measure the real discovered chain."
        )


def run(prompt: str, model: str, okf: bool, timeout: int) -> str | None:
    env = dict(os.environ)
    if model != "auto":
        env["LLM_ROUTER_OLLAMA_MODEL"] = model
    else:
        env.pop("LLM_ROUTER_OLLAMA_MODEL", None)
    env["LLM_ROUTER_CONTEXT_INJECTION"] = "on" if okf else "off"
    env["LLM_ROUTER_TRACE"] = ""
    payload = json.dumps({"session_id": "aabbccdd", "prompt": prompt,
                          "cwd": str(ROOT), "transcript_path": ""})
    try:
        r = subprocess.run([PY, str(HOOK)], input=payload, capture_output=True,
                           text=True, env=env, cwd=str(ROOT), timeout=timeout)
        return draft_body(r.stdout or "")
    except subprocess.TimeoutExpired:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--model", default="qwen3.8:latest")
    ap.add_argument("--okf", default="on")
    ap.add_argument("--corpus", default="/tmp/corpus.json")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    corpus = json.loads(Path(args.corpus).read_text())
    random.Random(args.seed).shuffle(corpus)
    sample = corpus[:args.n]

    okf = args.okf == "on"
    preflight(args.model)
    print(f"model={args.model}  context_injection={args.okf}  n={len(sample)}  seed={args.seed}")
    produced = accepted = 0
    reasons: dict[str, int] = {}
    skip_causes: dict[str, int] = {}
    for i, prompt in enumerate(sample, 1):
        mark = _log_size()
        t0 = time.monotonic()
        body = run(prompt, args.model, okf, args.timeout)
        dt = time.monotonic() - t0
        skips = _skips_since(mark)
        if body is None:
            reasons["no draft produced"] = reasons.get("no draft produced", 0) + 1
            for sk in skips:
                key = re.sub(r"\d+ chars", "N chars", sk)
                skip_causes[key] = skip_causes.get(key, 0) + 1
            why = "; ".join(skips) or "no model abandoned — gated before the call"
            print(f"  {i:3d}. NO DRAFT  [{dt:5.1f}s] {prompt[:40]!r} — {why[:70]}")
            continue
        produced += 1
        ok, why = verdict(body, prompt)
        accepted += ok
        if not ok:
            reasons[why.split(":")[0]] = reasons.get(why.split(":")[0], 0) + 1
        print(f"  {i:3d}. {'ACCEPT ' if ok else 'reject '}  {prompt[:46]!r} "
              f"{'' if ok else '— ' + why[:50]}")

    n = len(sample)
    print(f"\n  drafts produced : {produced}/{n} ({100*produced/n:.0f}%)")
    print(f"  ACCEPTABLE      : {accepted}/{n} ({100*accepted/n:.0f}%)")
    if skip_causes:
        print("\n  why models were abandoned (transport level):")
        for cause, count in sorted(skip_causes.items(), key=lambda x: -x[1]):
            print(f"     {count:3d}  {cause}")
    print("\n  why the rest were not:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"     {count:3d}  {reason}")
    print("\n  'acceptable' means Claude could relay it without saying something")
    print("  false. It is NOT a claim that the draft is correct — no mechanical")
    print("  check can establish that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
