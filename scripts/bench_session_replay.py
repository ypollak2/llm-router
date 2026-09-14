#!/usr/bin/env python3
"""Replay real sessions in order, so each prompt carries its real predecessors.

Two earlier benchmark designs each biased the answer in a different direction,
and neither described what the user actually experiences:

  * ONE shared session id across a shuffled corpus. A draft about prompt N became
    the injected context for unrelated prompt N+1 — 29 of 144 drafts carried the
    phrase "Gable 5" into prompts with nothing to do with it. Measured 72%/55%,
    inflated by contamination.
  * One ISOLATED empty session per prompt. No contamination, but no history
    either, so 95 of 200 prompts were correctly gated as context-dependent with
    nothing to resolve them. Measured 44%/33%, deflated to a floor.

Production is neither. A session accumulates its OWN coherent history, and the
prompt the user types next follows the turns that really preceded it. This
replays exactly that: real sessions, in order, with the real transcript attached
and a session id per session — not per prompt, and not one for all of them.

    python3 scripts/bench_session_replay.py --sessions 4 --max-per-session 25
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
HOOK = ROOT / "src/llm_router/hooks/auto-route.py"
PY_BIN = str(ROOT / ".venv/bin/python")
TRANSCRIPTS = Path.home() / ".claude/projects/-Users-yaliandrona"
DEBUG_LOG = Path.home() / ".llm-router/auto-route-debug.log"

_spec = importlib.util.spec_from_file_location(
    "_acc", ROOT / "scripts/bench_draft_acceptance.py")
_acc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_acc)

# A slash command, a paste, or a hook's own injected text is not something the
# user typed at the router. Counting them measures the harness.
_NOT_A_PROMPT = re.compile(
    r"^\s*(/|<command-|<local-command|<system-reminder|Caveat:|\[Request interrupted)", re.S)


def user_turns(path: Path) -> list[str]:
    """The prompts a human actually typed in this session, in order."""
    out: list[str] = []
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "user" or ev.get("isMeta"):
                continue
            content = (ev.get("message") or {}).get("content")
            if not isinstance(content, str):
                continue
            text = content.strip()
            if not text or _NOT_A_PROMPT.match(text) or len(text) > 4000:
                continue
            out.append(text)
    return out


def _log_size() -> int:
    try:
        return DEBUG_LOG.stat().st_size
    except OSError:
        return 0


def _reason_since(offset: int) -> str:
    try:
        with DEBUG_LOG.open() as fh:
            fh.seek(offset)
            tail = fh.read()
    except OSError:
        return "unknown"
    for line in tail.splitlines():
        if "DIRECT SKIP:" in line:
            return line.split("DIRECT SKIP:", 1)[1].strip()
        if "DIRECT MODEL SKIPPED:" in line:
            return re.sub(r"[0-9.]+", "N", line.split("SKIPPED:", 1)[1].strip())
    return "no reason logged"


def run_one(prompt: str, session_id: str, transcript: Path, timeout: int) -> tuple[str | None, str, float]:
    env = dict(os.environ)
    env["LLM_ROUTER_CONTEXT_INJECTION"] = "on"
    env.pop("LLM_ROUTER_OLLAMA_MODEL", None)
    payload = json.dumps({"session_id": session_id, "prompt": prompt,
                          "cwd": str(ROOT), "transcript_path": str(transcript)})
    mark = _log_size()
    t0 = time.monotonic()
    try:
        r = subprocess.run([PY_BIN, str(HOOK)], input=payload, capture_output=True,
                           text=True, env=env, cwd=str(ROOT), timeout=timeout)
        body = _acc.draft_body(r.stdout or "")
    except subprocess.TimeoutExpired:
        body = None
    dt = time.monotonic() - t0
    return body, ("drafted" if body else _reason_since(mark)), dt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--max-per-session", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=150)
    ap.add_argument("--out", default="/tmp/session_replay.json")
    args = ap.parse_args()

    files = sorted(TRANSCRIPTS.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
    picked: list[tuple[Path, list[str]]] = []
    for f in files:
        turns = user_turns(f)
        if len(turns) >= 10:
            picked.append((f, turns[:args.max_per_session]))
        if len(picked) >= args.sessions:
            break

    total = sum(len(t) for _, t in picked)
    print(f"replaying {len(picked)} real sessions, {total} prompts, in order\n")

    captured: list[dict] = []
    reasons: collections.Counter = collections.Counter()
    produced = accepted = 0
    for path, turns in picked:
        # One id per SESSION — the whole point. Salted per run so a replay never
        # inherits a previous replay's stored context.
        sid = hashlib.sha1(f"{path.name}|{os.getpid()}".encode()).hexdigest()[:8]
        print(f"── {path.name[:8]}  ({len(turns)} turns)")
        for i, prompt in enumerate(turns, 1):
            body, reason, dt = run_one(prompt, sid, path, args.timeout)
            ok, why = _acc.verdict(body, prompt) if body else (False, reason)
            produced += bool(body)
            accepted += ok
            reasons[reason if not body else ("acceptable" if ok else why.split(":")[0])] += 1
            captured.append({"session": path.name, "turn": i, "prompt": prompt,
                             "body": body, "reason": reason, "acceptable": ok,
                             "seconds": round(dt, 1)})
            Path(args.out).write_text(json.dumps(captured, indent=1))
            mark = "ACCEPT " if ok else ("weak   " if body else "NO     ")
            print(f"   {i:3d}. {mark} [{dt:5.1f}s] {prompt[:44]!r}"
                  + ("" if ok else f" — {(why or reason)[:38]}"))

    print(f"\n{'='*62}\n  prompts        : {total}  (real sessions, real order, real history)")
    print(f"  drafts produced: {produced}/{total} ({100*produced/total:.0f}%)")
    print(f"  ACCEPTABLE     : {accepted}/{total} ({100*accepted/total:.0f}%)")
    print("\n  breakdown:")
    for k, v in reasons.most_common():
        print(f"     {v:4d}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
