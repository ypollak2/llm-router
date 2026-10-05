#!/usr/bin/env python3
"""Build the frozen benchmark file that ``llm-router kpi`` reads for O2 and D5.

Inputs are already-measured results; nothing is generated here (no model call).

* ``--truth``  truth.jsonl from the blind agentic A/B (one row per task: hidden-test
  pass per tier haiku/sonnet/opus, ``cheapest_tier``).  Lives under ~/.rsi, never
  committed.
* ``--repo``   a checkout of the repo the tasks were mined from.  The task text
  (commit subject + body + source file list) is read from git here ONLY to run the
  production classifier on it; the text is not written to the output.

O2 (quality held): among tasks the Claude reference tier (Opus) passed, the share
the cheap routed tier (Haiku) also passed.  D5 (classifier accuracy): the production
proxy classifier's effective tier vs ``cheapest_tier``; tasks no tier passed carry no
tier truth and are left out of accuracy (counted as ``n_no_truth``).  Under-route =
predicted tier cheaper than the cheapest passing tier.

Output: ids (commit sha) + labels only, plus aggregates with provenance.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_router.classify import GATEWAY_POLICY, classify_signals  # noqa: E402

RANK = {"haiku": 0, "sonnet": 1, "opus": 2}
# proxy/claude_tiers.yaml route.default + haiku_rewrite: false (Haiku is never served).
RAW = {"simple": "haiku", "moderate": "sonnet", "complex": "opus", "deep_reasoning": "opus"}


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout


def task_prompt(repo: str, sha: str) -> str:
    """Same text the A/B harness sent (run_tier.build_prompt): subject, body, src files."""
    subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
    body = _git(repo, "log", "-1", "--format=%b", sha).strip()
    files = _git(repo, "show", "--name-only", "--format=", sha).split()
    src = [f for f in files if not f.startswith("tests/")]
    return (f"Task:\n{(subject + chr(10) * 2 + body).strip()}\n\n"
            f"Files expected to need changes: {src}\n")


def predict(text: str) -> tuple[str, str]:
    cx = classify_signals(text[-3000:], GATEWAY_POLICY).complexity.value
    raw = RAW[cx]
    return raw, ("sonnet" if raw == "haiku" else raw)


def build(truth_path: Path, repo: str) -> dict:
    rows = [json.loads(line) for line in truth_path.read_text().splitlines() if line.strip()]
    items, o2_hit, o2_n, d5_ok, d5_under, d5_n = [], 0, 0, 0, 0, 0
    for r in rows:
        raw, eff = predict(task_prompt(repo, r["commit_sha"]))
        truth = r["cheapest_tier"]
        items.append({"id": r["commit_sha"][:10], "pass": r["pass"], "cheapest_tier": truth,
                      "predicted_raw": raw, "predicted_effective": eff})
        if r["pass"]["opus"]:
            o2_n += 1
            o2_hit += bool(r["pass"]["haiku"])
        if truth in RANK:
            d5_n += 1
            d5_ok += eff == truth
            d5_under += RANK[eff] < RANK[truth]
    mtime = datetime.fromtimestamp(truth_path.stat().st_mtime, tz=timezone.utc)
    return {
        "generated_at": mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o2": {"acceptable_rate": o2_hit / o2_n if o2_n else 0.0, "n": o2_n,
               "definition": "tasks Opus passed (hidden tests) that Haiku also passed"},
        "d5": {"accuracy": d5_ok / d5_n if d5_n else 0.0,
               "under_route_rate": d5_under / d5_n if d5_n else 0.0, "n": d5_n,
               "n_no_truth": len(rows) - d5_n,
               "definition": "production proxy classifier, effective tier, vs cheapest "
                             "tier that passed hidden tests; no-pass tasks excluded"},
        "provenance": {
            "source": "classifier-v2-release/ab/work/agentic/truth.jsonl (blind A/B, "
                      "hidden unit tests, majority of junit grades)",
            "n_tasks": len(rows), "tiers": ["haiku", "sonnet", "opus"],
            "population": "single-repo (llm-router) mined commit tasks; not the real "
                          "prompt pool; 72.7% of real prompts (3+ tool calls) have no truth",
            "classifier": "llm_router.classify.classify_signals(GATEWAY_POLICY), "
                          "effective mapping (haiku_rewrite false)",
            "truth_not_used": "owner relabels and Opus-judge labels (complexity-v2/p_eval)",
        },
        "items": items,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--truth", required=True, type=Path)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    out = build(a.truth.expanduser(), str(Path(a.repo).expanduser()))
    a.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("generated_at", "o2", "d5")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
