#!/usr/bin/env python3
"""Does OKF context actually make a local model's repo answers better?

The claim the project rests on is that retrieving repo material before routing
turns an unanswerable question into an answerable one. It has never been
measured, and the obvious way to measure it has a hole: if I write the questions
and run the system, the questions drift toward what the system happens to do
well.

So nothing here is authored. Questions are DERIVED from the repository:

  * take symbols the repo defines exactly once (`git ls-files` + a definition
    regex), so the ground truth is unambiguous and comes from grep, not opinion
  * ask which file defines each one
  * score by string match against the real path

No model judges another model. No rubric. The score is "did it name the right
file", which is checkable by `in`.

Each question runs twice — without OKF context and with it — against the same
local model, so the only variable is the retrieval.

    python3 scripts/bench_grounding.py --n 25 --model qwen3-coder:30b

Reports accuracy in both conditions and the lift. A negative lift is a real
result and gets reported as one.
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
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Definitions worth asking about. Deliberately narrow: a name defined by a
# regex this loose in a comment or a string would poison the ground truth.
_DEF_RE = re.compile(r"^(?:async\s+)?def\s+([a-z_][a-z0-9_]{4,})\s*\(", re.M)
_CLASS_RE = re.compile(r"^class\s+([A-Z][A-Za-z0-9_]{3,})\s*[(:]", re.M)


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return [REPO / line for line in out.splitlines() if line.strip()]


def derive_questions(n: int, seed: int) -> list[dict]:
    """Symbols defined exactly once across the tracked tree."""
    where: dict[str, list[Path]] = defaultdict(list)
    for path in _tracked_python_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in (_DEF_RE, _CLASS_RE):
            for name in set(match.findall(text)):
                where[name].append(path)

    unique = sorted(name for name, paths in where.items() if len(paths) == 1)
    # Dunder and test scaffolding names are noise, not repo knowledge.
    unique = [n for n in unique
              if not n.startswith("test_") and not n.startswith("__")]
    random.Random(seed).shuffle(unique)

    questions = []
    for name in unique[:n]:
        target = where[name][0].relative_to(REPO)
        questions.append({
            "symbol": name,
            "answer": str(target),
            "basename": target.name,
            "prompt": (f"Which file in this project defines `{name}`? "
                       f"Answer with the file path and nothing else."),
        })
    return questions


def okf_context(prompt: str) -> str:
    """Whatever the router would actually retrieve for this prompt."""
    try:
        from llm_router import okf
        docs = okf.find_relevant(prompt)
    except Exception:
        return ""
    if not docs:
        return ""
    body = "\n\n".join(d.as_context_block() for d in docs)
    return f"<knowledge_context>\n{body}\n</knowledge_context>"


def ask(model: str, prompt: str, context: str, timeout: int) -> tuple[str, float]:
    system = ("You are answering a question about a specific code repository. "
              "Answer with a file path only.")
    if context:
        system += ("\n\nUse this material from the repository:\n" + context)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "stream": False, "think": False,
    }).encode()
    url = os.environ.get("LLM_ROUTER_OLLAMA_URL", "http://localhost:11434")
    req = urllib.request.Request(f"{url}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        return payload.get("message", {}).get("content", ""), time.time() - t0
    except Exception as exc:
        return f"<error: {type(exc).__name__}>", time.time() - t0


def scores(answer: str, q: dict) -> bool:
    """Correct if the real path appears. The basename alone counts: naming the
    file is the knowledge being tested, and the directory is a formatting
    preference the question did not ask for."""
    text = (answer or "").strip()
    return q["answer"] in text or q["basename"] in text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--model", default="qwen3-coder:30b")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    questions = derive_questions(args.n, args.seed)
    if not questions:
        print("no uniquely-defined symbols found — is this a git checkout?")
        return 1
    print(f"{len(questions)} questions derived from the repo "
          f"(seed={args.seed}, model={args.model})\n")

    results = []
    bare_ok = ctx_ok = with_ctx = 0
    for i, q in enumerate(questions, 1):
        context = okf_context(q["prompt"])
        if context:
            with_ctx += 1
        a_bare, t_bare = ask(args.model, q["prompt"], "", args.timeout)
        a_ctx, t_ctx = ask(args.model, q["prompt"], context, args.timeout)
        ok_bare, ok_ctx = scores(a_bare, q), scores(a_ctx, q)
        bare_ok += ok_bare
        ctx_ok += ok_ctx
        results.append({**q, "retrieved": bool(context),
                        "bare_ok": ok_bare, "ctx_ok": ok_ctx,
                        "bare": a_bare[:200], "ctx": a_ctx[:200],
                        "secs": round(t_bare + t_ctx, 1)})
        print(f"{i:3d}/{len(questions)} {q['symbol'][:34]:36s} "
              f"bare={'Y' if ok_bare else 'n'} okf={'Y' if ok_ctx else 'n'} "
              f"{'(no material retrieved)' if not context else ''}", flush=True)

    n = len(questions)
    print(f"\n{'':22s}{'correct':>9s}{'accuracy':>11s}")
    print(f"{'without OKF context':22s}{bare_ok:>9d}{bare_ok / n:>10.1%}")
    print(f"{'with OKF context':22s}{ctx_ok:>9d}{ctx_ok / n:>10.1%}")
    print(f"\nlift: {(ctx_ok - bare_ok) / n:+.1%} "
          f"({ctx_ok - bare_ok:+d} of {n})")
    print(f"material was retrieved for {with_ctx}/{n} questions "
          f"({with_ctx / n:.0%}) — retrieval is high-precision, so an empty "
          f"result is the expected common case and caps the achievable lift")
    if with_ctx:
        sub = [r for r in results if r["retrieved"]]
        s_bare = sum(r["bare_ok"] for r in sub)
        s_ctx = sum(r["ctx_ok"] for r in sub)
        print(f"on those {len(sub)} alone: {s_bare / len(sub):.1%} -> "
              f"{s_ctx / len(sub):.1%} ({s_ctx - s_bare:+d})")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nper-question detail: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
