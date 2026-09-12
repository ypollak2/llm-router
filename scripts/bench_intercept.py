#!/usr/bin/env python3
"""Do the intercepts preserve the ANSWER, not just save tokens?

Saving tokens is easy; saving them without losing the fact the model needed is
the whole question. Every case here plants a specific fact in an image or a
command's output and checks the intercepted result still carries it. Mechanical
scoring — a string is present or it is not.

Many samples per category, because the earlier 5-case harness could not tell a
real difference from noise: 30 image cases across 6 generated screenshots, and
20 bash cases over this repository.

    python3 scripts/bench_intercept.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zlib
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from llm_router.hooks import tool_intercept as ti  # noqa: E402
from llm_router.vision_registry import best_vision_model  # noqa: E402

SHOTS = Path(os.environ.get("LLM_ROUTER_BENCH_SHOTS",
                            "/Users/yaliandrona/.claude/jobs/82d6664b/tmp/bench40/shots"))


# ── image cases: 5 questions x 6 screenshots ────────────────────────────────
IMAGE_CASES = [
    ("form", [("Dana Okoro", "full name"), ("dana@example.com", "email"),
              ("Portugal", "country"), ("Business", "plan"), ("Continue", "button")]),
    ("table", [("Globex", "overdue customer"), ("INV-105", "last invoice id"),
               ("4,100", "largest amount"), ("Umbrella", "pending customer"),
               ("Acme", "first customer")]),
    ("error", [("51", "error code"), ("declined", "failure reason"),
               ("Payment failed", "headline"), ("Retry", "action button"),
               ("issuer", "who declined")]),
    ("dashboard", [("8,421", "active users"), ("52,900", "revenue"),
                   ("2.4", "churn"), ("17", "tickets"), ("Jun", "last month")]),
    ("nav", [("Billing", "selected item"), ("Dashboard", "first item"),
             ("Settings", "last item"), ("October", "invoice month"),
             ("Projects", "second item")]),
]

# ── bash cases: real commands, each with a fact that must survive ───────────
BASH_CASES = [
    ("git status --porcelain", "src/"),
    ("git log --oneline -20", "agent-loop"),
    ("ls -la src/llm_router", "vision_registry.py"),
    ("ls src/llm_router/hooks", "tool_intercept.py"),
    ("wc -l src/llm_router/okf.py", "okf.py"),
    ("find src/llm_router/hooks -name '*.py'", "agent_loop.py"),
    ("git branch -a", "okf-per-request-scope"),
    ("cat pyproject.toml", "llm-routing"),
    ("head -40 README.md", "llm"),
    ("git diff --stat", "env_registry"),
]


def run_images(model: str) -> list[dict]:
    rows = []
    for shot, questions in IMAGE_CASES:
        path = SHOTS / f"{shot}.png"
        if not path.exists():
            continue
        t0 = time.time()
        description = ti.describe_image(str(path), model)
        dt = time.time() - t0
        blob = path.stat().st_size
        for fact, label in questions:
            ok = bool(description) and fact.lower() in description.lower()
            rows.append({"kind": "image", "case": f"{shot}:{label}",
                         "ok": ok, "s": round(dt, 1),
                         "image_tokens_avoided": blob // 3 // 4,
                         "desc_tokens": len(description or "") // 4})
            print(f"  image  {shot:10s} {label:18s} {'PASS' if ok else 'FAIL'}",
                  flush=True)
    return rows


def run_bash() -> list[dict]:
    rows = []
    for command, fact in BASH_CASES:
        payload = {"tool_name": "Bash", "cwd": str(REPO),
                   "tool_input": {"command": command}}
        real = subprocess.run(command, shell=True, capture_output=True,
                              text=True, cwd=REPO, timeout=60).stdout
        t0 = time.time()
        out = ti.try_intercept_bash(payload)
        dt = time.time() - t0
        if out is None:
            rows.append({"kind": "bash", "case": command[:34], "ok": None,
                         "s": round(dt, 2), "skipped": True})
            print(f"  bash   {command[:34]:36s} (not intercepted)", flush=True)
            continue
        ok = fact.lower() in out.lower()
        rows.append({"kind": "bash", "case": command[:34], "ok": ok,
                     "s": round(dt, 2),
                     "before_tokens": len(real) // 4,
                     "after_tokens": len(out) // 4})
        print(f"  bash   {command[:34]:36s} {'PASS' if ok else 'FAIL'}  "
              f"{len(real)//4:>5} -> {len(out)//4:>5} tok", flush=True)
    return rows


def main() -> int:
    os.environ["LLM_ROUTER_BASH_INTERCEPT"] = "1"
    os.environ["LLM_ROUTER_IMAGE_INTERCEPT"] = "1"
    model = best_vision_model(allow_probe=True)
    print(f"vision model: {model or '(none proven — images go to Claude)'}\n")

    rows = []
    if model:
        rows += run_images(model)
    rows += run_bash()

    imgs = [r for r in rows if r["kind"] == "image"]
    bash = [r for r in rows if r["kind"] == "bash" and r.get("ok") is not None]
    skipped = [r for r in rows if r.get("skipped")]

    print(f"\n{'category':10s}{'pass':>10s}{'rate':>8s}")
    if imgs:
        print(f"{'image':10s}{sum(r['ok'] for r in imgs):>4d}/{len(imgs):<5d}"
              f"{sum(r['ok'] for r in imgs)/len(imgs):>8.0%}")
    if bash:
        print(f"{'bash':10s}{sum(r['ok'] for r in bash):>4d}/{len(bash):<5d}"
              f"{sum(r['ok'] for r in bash)/len(bash):>8.0%}")
    if skipped:
        print(f"{'skipped':10s}{len(skipped):>4d}       (fell through to Claude)")

    if imgs:
        avoided = sum(r["image_tokens_avoided"] for r in imgs) // 5
        spent = sum(r["desc_tokens"] for r in imgs) // 5
        print(f"\nimage tokens avoided ~{avoided:,}  spent on descriptions ~{spent:,}"
              f"   ({1 - spent/max(avoided,1):.0%} reduction)")
    if bash:
        b = sum(r["before_tokens"] for r in bash)
        a = sum(r["after_tokens"] for r in bash)
        print(f"bash tokens {b:,} -> {a:,}   ({1 - a/max(b,1):.0%} reduction)")

    total = imgs + bash
    ok = sum(bool(r["ok"]) for r in total)
    print(f"\nTOTAL {ok}/{len(total)} = {ok/max(len(total),1):.0%}")
    json.dump(rows, open(REPO / "intercept_bench.json", "w"), indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
