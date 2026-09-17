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
  * score by exact match against the real repo-relative path

No model judges another model. No rubric.

TWO SCORES, REPORTED SEPARATELY (see `score()`)

The first version of this script scored `answer in text or basename in text`,
on the deliberate argument that naming the file is the knowledge under test and
the directory is a formatting preference. The argument holds; that
implementation of it did not. `okf.py` is a substring of
`wrong_directory/okf.py`, so an answer that named a directory — and named the
wrong one — scored correct. So:

    strict   the answer names exactly the gold repo-relative path
    lenient  strict, OR it names the right file and asserts no directory

An answer naming the WRONG directory is false under both. It did not omit the
directory; it asserted one.

Consequence for anything already written down: the "+64.0%" in this script's
own first commit (a535f22) was measured under the old rule and is NOT
comparable with a strict figure. Say which rule a number came from, every time,
and print its n beside it.

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
    # monotonic, not wall clock: macOS Maintenance Sleep advances time.time()
    # and not time.monotonic(). One benchmark task in this repo was recorded at
    # 918.6s of which 902s was the laptop asleep.
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        return payload.get("message", {}).get("content", ""), time.monotonic() - t0
    except Exception as exc:
        return f"<error: {type(exc).__name__}>", time.monotonic() - t0


# A path-ish token: optional directories, then a filename with a source suffix.
_PATH_TOKEN = re.compile(
    r"[\w./\\-]*\w[\w./\\-]*\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|md|sh|toml|cfg|yaml|yml)"
)


def _normalise(token: str) -> str:
    """One spelling per path, so comparison is about the path and not the typing."""
    token = token.strip().strip("`'\"“”‘’").replace("\\", "/")
    token = token.rstrip(".,;:)»]")
    while token.startswith(("./", "/")):
        token = token.lstrip("/")
        if token.startswith("./"):
            token = token[2:]
    return token


def score(answer: str, q: dict) -> tuple[bool, bool]:
    """Return (strict, lenient) for one answer.

    The original rule was ``q["answer"] in text or q["basename"] in text``, and
    its docstring defended the second half on purpose: naming the file is the
    knowledge under test, and the directory is a formatting preference the
    question did not ask for.

    The argument is reasonable. That implementation of it is not. `okf.py` is a
    substring of `wrong_directory/okf.py`, so an answer that names a directory —
    and names the wrong one — scored correct. The lenient rule was meant to
    forgive an answer that OMITS the directory. It also forgave an answer that
    got the directory wrong, which is the opposite of forgiving.

    So both readings survive, separately, because they measure different things:

        strict   the answer names exactly the gold repo-relative path
        lenient  strict, OR it names the right file and asserts no directory

    An answer naming the wrong directory is false under both. Naming two
    candidates is false under both: that is a list, not an answer.

    Report both with their n. The published "+64.0%" from this script's own
    first commit was measured under the old rule and is not comparable with a
    strict figure — say which one a number is, every time.
    """
    text = (answer or "").strip()
    if not text:
        return False, False

    tokens = [_normalise(t) for t in _PATH_TOKEN.findall(text)]
    tokens = [t for t in tokens if t]
    qualified = {t for t in tokens if "/" in t}
    bare = {t for t in tokens if "/" not in t}

    gold = _normalise(q["answer"])
    gold_base = q["basename"]

    strict = qualified == {gold}
    # A directory was asserted, so the omission this forgives did not happen.
    lenient = strict or (not qualified and bare == {gold_base})
    return strict, lenient


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
    with_ctx = 0
    tally = {"bare_strict": 0, "ctx_strict": 0, "bare_lenient": 0, "ctx_lenient": 0}
    for i, q in enumerate(questions, 1):
        context = okf_context(q["prompt"])
        if context:
            with_ctx += 1
        a_bare, t_bare = ask(args.model, q["prompt"], "", args.timeout)
        a_ctx, t_ctx = ask(args.model, q["prompt"], context, args.timeout)
        bare_strict, bare_lenient = score(a_bare, q)
        ctx_strict, ctx_lenient = score(a_ctx, q)
        tally["bare_strict"] += bare_strict
        tally["ctx_strict"] += ctx_strict
        tally["bare_lenient"] += bare_lenient
        tally["ctx_lenient"] += ctx_lenient
        results.append({**q, "retrieved": bool(context),
                        "bare_strict": bare_strict, "ctx_strict": ctx_strict,
                        "bare_lenient": bare_lenient, "ctx_lenient": ctx_lenient,
                        "bare": a_bare[:200], "ctx": a_ctx[:200],
                        "secs": round(t_bare + t_ctx, 1)})
        print(f"{i:3d}/{len(questions)} {q['symbol'][:34]:36s} "
              f"bare={'Y' if bare_strict else 'n'} okf={'Y' if ctx_strict else 'n'} "
              f"{'(no material retrieved)' if not context else ''}", flush=True)

    n = len(questions)
    # Two rules, reported side by side and never averaged together. STRICT is the
    # headline: it is the one that rejects a wrong directory. LENIENT also counts
    # an answer that names the right file and asserts no directory at all.
    print(f"\nn = {n} questions · model = {args.model} · seed = {args.seed}")
    print(f"\n{'':22s}{'strict':>16s}{'lenient':>16s}")
    for label, key in (("without OKF context", "bare"), ("with OKF context", "ctx")):
        s, le = tally[f"{key}_strict"], tally[f"{key}_lenient"]
        print(f"{label:22s}{s:>7d}{s / n:>9.1%}{le:>7d}{le / n:>9.1%}")
    d_strict = tally["ctx_strict"] - tally["bare_strict"]
    d_lenient = tally["ctx_lenient"] - tally["bare_lenient"]
    print(f"\nlift (strict):  {d_strict / n:+.1%} ({d_strict:+d} of {n})")
    print(f"lift (lenient): {d_lenient / n:+.1%} ({d_lenient:+d} of {n})")
    if n < 50:
        print(f"\n** n = {n} is too few to tell. Days with 21-64 prompts in this "
              f"repo produced 2.5%, 1.6%, 0% and 4.3% and all four were noise. **")
    print(f"\nmaterial was retrieved for {with_ctx}/{n} questions "
          f"({with_ctx / n:.0%}) — retrieval is high-precision, so an empty "
          f"result is the expected common case and caps the achievable lift")
    if with_ctx:
        sub = [r for r in results if r["retrieved"]]
        s_bare = sum(r["bare_strict"] for r in sub)
        s_ctx = sum(r["ctx_strict"] for r in sub)
        print(f"on those {len(sub)} alone (strict): {s_bare / len(sub):.1%} -> "
              f"{s_ctx / len(sub):.1%} ({s_ctx - s_bare:+d} of {len(sub)})")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nper-question detail: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
