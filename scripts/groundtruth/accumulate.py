#!/usr/bin/env python3
"""Turn a live routing task into a Ground Truth candidate — or say why not.

Two ways in:

    # live, from the capture hook
    from groundtruth.accumulate import accumulate

    # offline, over captured prompts
    python3 scripts/groundtruth/accumulate.py --from-capture
    python3 scripts/groundtruth/accumulate.py --report

The order is deliberate and is the point of the whole phase:

    1. assess eligibility      — cheap, pure, decides what state is needed
    2. capture the envelope    — only the state step 1 said was needed
    3. re-check completeness   — the envelope may have failed to get it
    4. admit or reject         — with a reason, always

Step 3 is what keeps this honest. A task can pass every content check and still
be rejected because the repo was not a git tree, or the external evidence was
never frozen. Eligibility is not a property of the prompt alone; it is a
property of the prompt plus what we actually managed to preserve.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import envelope as envmod  # noqa: E402
from groundtruth import pool as poolmod  # noqa: E402
from groundtruth.eligibility import assess  # noqa: E402
from groundtruth.scrub import residual_risk, scrub  # noqa: E402


def accumulate(
    prompt: str,
    *,
    route_id: str | None = None,
    session_id: str | None = None,
    prompt_sha256: str | None = None,
    task_type: str | None = None,
    complexity: str | None = None,
    cwd: Path | None = None,
    external=None,
    tool_names=None,
    test_command: str | None = None,
    model_config: dict | None = None,
    pool: poolmod.Pool | None = None,
    dedup: bool = True,
) -> tuple[bool, str, dict]:
    """Assess, capture, admit. Returns (admitted, reason, eligibility_json).

    Never raises: this runs on the routing path, and losing a candidate is
    always preferable to failing a user's request.
    """
    try:
        clean, report = scrub(prompt)
        flags = residual_risk(clean)

        # Pass 1: what does this task need?
        first = assess(prompt, task_type=task_type,
                       has_repo_state=False, has_external_evidence=bool(external))

        if not first.replayable and not first.verification_candidate:
            # Nothing an envelope could add would rescue it; skip the cost.
            p = pool or poolmod.Pool()
            cand = poolmod.make_candidate(
                task_id=_task_id(prompt_sha256, route_id, clean),
                prompt=clean, prompt_sha256=prompt_sha256 or "",
                eligibility=first, envelope={}, route_id=route_id,
                session_id=session_id, task_type=task_type, complexity=complexity,
                scrub_counts=dict(report.counts), residual_flags=flags)
            ok, reason = p.admit(cand, clean, dedup=dedup)
            return ok, reason, first.to_json()

        # Pass 2: capture only the state pass 1 asked for.
        env = envmod.build(
            prompt=prompt, required_state=first.required_state,
            route_id=route_id, session_id=session_id,
            prompt_sha256=prompt_sha256, task_type=task_type,
            cwd=cwd, external=external, tool_names=tool_names,
            test_command=test_command, model_config=model_config)
        complete, missing = env.completeness()

        # Pass 3: re-assess against what was ACTUALLY captured.
        final = assess(
            prompt, task_type=task_type,
            has_repo_state=bool(env.repo and env.repo.reconstructable),
            has_external_evidence=bool(env.external),
            envelope_complete=complete)

        p = pool or poolmod.Pool()
        cand = poolmod.make_candidate(
            task_id=_task_id(prompt_sha256, route_id, clean),
            prompt=clean, prompt_sha256=prompt_sha256 or "",
            eligibility=final, envelope=env.to_json(), route_id=route_id,
            session_id=session_id, task_type=task_type, complexity=complexity,
            scrub_counts=dict(report.counts), residual_flags=flags)
        ok, reason = p.admit(cand, clean, dedup=dedup)
        return ok, reason, final.to_json()
    except Exception as exc:  # noqa: BLE001 — accumulation never breaks routing
        return False, f"accumulation-error: {type(exc).__name__}", {}


def _task_id(prompt_sha256: str | None, route_id: str | None,
             prompt: str = "") -> str:
    """Content-derived, so two different prompts can never share an id.

    An earlier version trusted the caller's `prompt_sha256`; a caller passing a
    constant collapsed seven distinct tasks into one pool row. The prompt's own
    hash is the only value that cannot be wrong about which task this is.
    """
    from groundtruth.extract_corpus import exact_key
    if prompt:
        return f"gtc-{exact_key(prompt)}"
    return f"gtc-{(prompt_sha256 or route_id or str(time.time()))[:16]}"


# ── Offline driver ───────────────────────────────────────────────────────────

def from_capture(capture_path: Path, pool: poolmod.Pool, limit: int = 0) -> dict:
    """Run the gate over an existing prompt_capture.jsonl.

    Useful to see what the gate WOULD have done, and the only way to seed the
    pool before capture has been running. Note the envelope is captured now,
    not then, so repo state reflects today's tree — which is why these rows
    will mostly be rejected as incomplete. That is correct: they are.
    """
    seen = admitted = 0
    reasons: dict[str, int] = {}
    if not capture_path.exists():
        return {"seen": 0, "admitted": 0, "reasons": {}, "note": "no capture file"}
    for line in capture_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        prompt = rec.get("prompt")
        if not prompt:
            continue
        seen += 1
        ok, reason, _ = accumulate(
            prompt, route_id=rec.get("route_id"), session_id=rec.get("session_id"),
            prompt_sha256=rec.get("prompt_sha256"), task_type=rec.get("task_type"),
            complexity=rec.get("complexity"), pool=pool)
        admitted += ok
        reasons[reason] = reasons.get(reason, 0) + 1
        if limit and seen >= limit:
            break
    return {"seen": seen, "admitted": admitted, "reasons": reasons}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", type=Path, default=None)
    ap.add_argument("--from-capture", action="store_true",
                    help="run the gate over ~/.llm-router/prompt_capture.jsonl")
    ap.add_argument("--capture-path", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    p = poolmod.Pool(args.pool)

    if args.from_capture:
        cap = args.capture_path or (poolmod.default_pool_path().parent
                                    / "prompt_capture.jsonl")
        res = from_capture(cap, p, limit=args.limit)
        print(f"seen {res['seen']}  admitted {res['admitted']}")
        for r, n in sorted(res.get("reasons", {}).items(), key=lambda kv: -kv[1]):
            print(f"  {r:38s} {n}")
        if res.get("note"):
            print(f"  ({res['note']})")
        return 0

    if args.report or True:
        from groundtruth.accumulate_report import render
        print(render(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
