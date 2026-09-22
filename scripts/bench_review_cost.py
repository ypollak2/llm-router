#!/usr/bin/env python3
"""Does handing Claude a local candidate cost less than Claude doing the work?

This is the number the whole local-execution architecture rests on, and it has
never been measured. Codex raised it against its own design: "native review
repeats all the work — savings can disappear; benchmark this explicitly."

Two conditions per task, each on a fresh sandbox:

  A  direct   Claude does the task itself
  B  review   the local model produces a candidate, Claude reviews the diff
              and fixes it if wrong

The metric is CLAUDE tokens and CLAUDE turns, not wall clock. Local execution is
free and slow; Claude is the thing being spent. Weighted the way the subscription
meters it: input + 1.25*cache_write + 0.1*cache_read + 5*output.

Both conditions are scored by the brutal suite's own verifier, because a cheap
wrong answer is not a saving. Tasks deliberately include two the local model is
known to FAIL — review of a wrong candidate is the expensive case, and a
benchmark that only measures the happy path would overstate the saving.

    python3 scripts/bench_review_cost.py --tasks 5
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import bench_backend_quality as bench  # noqa: E402

# M-02: every row this script causes is benchmark traffic, not usage. Declared
# here rather than inferred later -- `routing_quality.detect_synthetic` prefers
# an explicit statement by the harness, and until now no bench script made one,
# so 1,813 fixture rows were counted as production spend.
import os as _os

_os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

W = {"input": 1.0, "cw": 1.25, "cr": 0.1, "out": 5.0}

# 3 the local model passes, 2 it fails — the mix that keeps the answer honest.
TASK_IDS = ["br-stable-rank", "br-retry-contract", "br-append-only",
            "br-money-total", "br-dedupe-order"]


def claude_usage_since(project_dir: str, since: float) -> tuple[float, int]:
    """Weighted tokens and assistant turns for one `claude -p` run."""
    weighted, turns = 0.0, 0
    pattern = os.path.expanduser(f"~/.claude/projects/{project_dir}/*.jsonl")
    for path in glob.glob(pattern):
        if os.path.getmtime(path) < since - 5:
            continue
        for line in Path(path).read_text(errors="replace").splitlines():
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            u = (rec.get("message") or {}).get("usage") or {}
            if not u:
                continue
            weighted += (u.get("input_tokens", 0) * W["input"]
                         + u.get("cache_creation_input_tokens", 0) * W["cw"]
                         + u.get("cache_read_input_tokens", 0) * W["cr"]
                         + u.get("output_tokens", 0) * W["out"])
            turns += 1
    return weighted, turns


def slug(path: Path) -> str:
    """Claude Code's project-directory name for a working directory.

    It resolves symlinks first — on macOS /tmp is /private/tmp — and turns both
    separators and underscores into dashes. Getting this wrong reads zero tokens
    and silently reports a free run.
    """
    return str(path.resolve()).replace("/", "-").replace("_", "-")


def run_claude(prompt: str, sandbox: Path, timeout: int = 300) -> tuple[str, float, int]:
    t0 = time.time()
    env = dict(os.environ)
    env.update({"LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_DIRECT_EXECUTION": "false"})
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--model", "sonnet",
             "--permission-mode", "acceptEdits", "--add-dir", str(sandbox),
             "--settings", '{"hooks":{}}', "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}',
             "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(python3 -m pytest:*)"],
            capture_output=True, text=True, cwd=str(sandbox), env=env,
            stdin=subprocess.DEVNULL, timeout=timeout)
        out = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        out = "(timeout)"
    tok, turns = claude_usage_since(slug(sandbox), t0)
    return out, tok, turns


def snapshot_text(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts:
            try:
                out[str(p.relative_to(root))] = p.read_text(errors="replace")
            except OSError:
                pass
    return out


def unified_diff(before: dict, after: dict) -> str:
    import difflib
    chunks = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name, ""), after.get(name, "")
        if b == a:
            continue
        chunks.append("".join(difflib.unified_diff(
            b.splitlines(True), a.splitlines(True),
            fromfile=f"a/{name}", tofile=f"b/{name}")))
    return "".join(chunks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=len(TASK_IDS))
    ap.add_argument("--out", default="/tmp/review_cost.json")
    args = ap.parse_args()

    files, tasks = bench.SUITES["brutal"]
    chosen = [t for t in tasks if t[0] in TASK_IDS[:args.tasks]]
    orig = bench.OUT_DIR / "orig_test_parser.py"
    bench.OUT_DIR.mkdir(parents=True, exist_ok=True)
    orig.write_text(bench.FILES["tests/test_parser.py"])

    rows = []
    for tid, kind, prompt, allowed, verifier in [t[:5] for t in chosen]:
        # ── A: Claude does it ────────────────────────────────────────────────
        sb_a = Path(f"/tmp/rc_a_{tid}")
        bench.build(sb_a, files)
        _, tok_a, turns_a = run_claude(prompt, sb_a)
        ok_a, _ = bench.verify(sb_a, verifier, "direct", orig)

        # ── B: local proposes, Claude reviews ────────────────────────────────
        sb_b = Path(f"/tmp/rc_b_{tid}")
        bench.build(sb_b, files)
        before = snapshot_text(sb_b)
        from llm_router.tools.local_task import llm_local_task
        asyncio.run(llm_local_task(objective=prompt, workdir=str(sb_b),
                                   acceptance_check=None, budget_s=300))
        diff = unified_diff(before, snapshot_text(sb_b))
        review_prompt = (
            f"A local model attempted this task:\n\n{prompt}\n\n"
            f"It produced this candidate patch, which is UNVERIFIED — no check "
            f"has established it is correct:\n\n```diff\n{diff[:6000]}\n```\n\n"
            f"The files are already in this directory in that state. Verify the "
            f"work actually satisfies the task. If it does, reply APPROVED. If it "
            f"does not, fix it."
        )
        _, tok_b, turns_b = run_claude(review_prompt, sb_b)
        ok_b, _ = bench.verify(sb_b, verifier, "review", orig)

        ratio = (tok_b / tok_a) if tok_a else float("nan")
        rows.append({"task": tid, "direct_tokens": round(tok_a), "direct_turns": turns_a,
                     "direct_correct": ok_a, "review_tokens": round(tok_b),
                     "review_turns": turns_b, "review_correct": ok_b,
                     "ratio": round(ratio, 3), "diff_lines": diff.count("\n")})
        print(f"{tid:20s} direct {tok_a:9,.0f} tok/{turns_a:2d} turns ok={ok_a}  |  "
              f"review {tok_b:9,.0f} tok/{turns_b:2d} turns ok={ok_b}  |  "
              f"ratio {ratio:.2f}", flush=True)
        Path(args.out).write_text(json.dumps(rows, indent=2))

    d = sum(r["direct_tokens"] for r in rows)
    b = sum(r["review_tokens"] for r in rows)
    print(f"\nTOTAL  direct {d:,}  review {b:,}  ratio {b/d:.2f}" if d else "no data")
    print(f"correct: direct {sum(r['direct_correct'] for r in rows)}/{len(rows)}, "
          f"review {sum(r['review_correct'] for r in rows)}/{len(rows)}")
    print("\nKILL CRITERION: ratio >= 0.80 means review costs as much as doing it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
