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

# M-02: benchmark traffic, declared rather than inferred. This file cannot be
# parsed by Python < 3.12 (an f-string contains a backslash), so the marker
# was inserted textually rather than via the AST pass used on its siblings.
import os as _os

_os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
HOOK = ROOT / "src/llm_router/hooks/auto-route.py"
PY_BIN = str(ROOT / ".venv/bin/python")
TRANSCRIPTS = Path.home() / ".claude/projects/-Users-yaliandrona"
def _debug_log():
    """M-04: resolved per call. The one-segment spelling
    `".llm-router/x"` was missed by the repo-wide sweep, which matched
    only `/ ".llm-router" / "x"`."""
    import os as _os
    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".llm-router"
    return root / "auto-route-debug.log"

_spec = importlib.util.spec_from_file_location(
    "_acc", ROOT / "scripts/bench_draft_acceptance.py")
_acc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_acc)

# A slash command, a paste, or a hook's own injected text is not something the
# user typed at the router. Counting them measures the harness.
_NOT_A_PROMPT = re.compile(
    r"^\s*(/|<command-|<local-command|<system-reminder|<task-notification"
    r"|<user-prompt-submit-hook|Caveat:|\[Request interrupted)", re.S)

# A bare acknowledgement carries nothing to route, and the hook deliberately
# bypasses it (CONTINUATION: bypass to host agent). Counting it as a miss makes
# the router look worse than it is for doing the right thing — on 2026-09-14 it
# understated both the baseline and the after-run by ~4 points each.
_BARE_ACK = re.compile(r"^(continue|proceed|yes|no|go|ok|okay|go on|keep going|next)[.!]?$", re.I)


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
        return _debug_log().stat().st_size
    except OSError:
        return 0


# Every way the hook can terminate without producing a draft. Reading only the
# first two of these left 8 of 115 misses in the 2026-09-14 run reported as "no
# reason logged" — 7% of prompts vanishing unexplained, in a comparison being
# argued over 6-point differences. Seven of those eight were a deliberate
# CONTINUATION bypass ("continue", "yes", "Proceed" carry nothing to route) and
# were being counted as a failure of the router rather than a choice by it.
_TERMINAL = (
    "DIRECT SKIP:",
    "DIRECT MODEL SKIPPED:",
    "DIRECT FAILED:",
    "DIRECT ERROR:",
    "CONTINUATION:",
    "EARLY EXIT:",
    "CRITICAL PRESSURE:",
    "DRAFT REJECTED",
    "SIDECAR ERROR:",
)


def _reason_since(offset: int) -> str:
    try:
        with _debug_log().open() as fh:
            fh.seek(offset)
            tail = fh.read()
    except OSError:
        return "log unreadable"
    for line in tail.splitlines():
        for marker in _TERMINAL:
            if marker in line:
                reason = line.split(marker, 1)[1].strip() or marker.rstrip(":").lower()
                # Collapse digits so "timeout_35.4s" and "timeout_36.1s" bucket
                # together — but ONLY in a numeric run, never a bare dot. The
                # first version used [0-9.]+ and turned ".claude/settings.json"
                # into "Nclaude/settingsNjson", destroying exactly the filename a
                # reader needs to judge whether the rejection was correct.
                return f"{marker.rstrip(':').lower()}: {re.sub(r'\d[\d.]*', 'N', reason)}"[:70]
    if "DIRECT SUCCESS" in tail:
        return "drafted then discarded downstream"
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

    routable = [c for c in captured if not _BARE_ACK.match(c["prompt"].strip())]
    r_prod = sum(1 for c in routable if c["body"])
    r_acc = sum(1 for c in routable if c["acceptable"])
    print(f"\n{'='*62}\n  prompts        : {total}  (real sessions, real order, real history)")
    print(f"  drafts produced: {produced}/{total} ({100*produced/total:.0f}%)")
    print(f"  ACCEPTABLE     : {accepted}/{total} ({100*accepted/total:.0f}%)")
    if routable and len(routable) != total:
        print(f"\n  excluding {total - len(routable)} bare acknowledgements the hook")
        print(f"  deliberately bypasses ('continue', 'yes', 'Proceed'):")
        print(f"     drafts produced: {r_prod}/{len(routable)} ({100*r_prod/len(routable):.0f}%)")
        print(f"     ACCEPTABLE     : {r_acc}/{len(routable)} ({100*r_acc/len(routable):.0f}%)")
    print("\n  breakdown:")
    for k, v in reasons.most_common():
        print(f"     {v:4d}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
