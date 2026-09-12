#!/usr/bin/env python3
"""Run the candidate model pool over the external corpus and record graded outcomes.

This produces the table the router is actually fit on: for every (item, model, budget), did
the model get it right, what did it cost, how long did it take. Nothing here touches
RouterArena -- the corpus is the audited external one built by ``build_corpus.py``.

Two backends:

* ``--backend ollama``  -- local models, free. Used for the full dry run: it exercises every
  grader, every output contract and the whole accounting path at zero cost, so a bug shows up
  before any money is spent rather than after.
* ``--backend openrouter`` -- the real candidate pool, priced from RouterArena's own
  ``model_cost.json``.

**Output caps are enforced here, in the harness.** They are the accuracy fix *and* the budget
control: measured against the cost table, the same sweep costs $74 at contracted output
lengths and $1,336 at reasoning-mode lengths. A bug that lets output run long is the only
realistic way this run becomes expensive, so the cap is applied at the request layer and
re-checked on the response.

Usage::

    python scripts/routerarena/label_outcomes.py --backend ollama --models qwen3.5:latest \\
        --limit 40 --dry-run-report
    python scripts/routerarena/label_outcomes.py --backend openrouter --tier cheap
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "data" / "corpus"
OUT = REPO / "data" / "outcomes"
COST_TABLE = REPO / "data" / "benchmarks" / "model_cost.json"

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


# --------------------------------------------------------------------------------------
# Output contracts -- per skill cluster, what the answer must look like and how long it may be
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    """What a given skill cluster's answer must look like, and how much room it gets."""

    system: str
    max_tokens: int
    grader: str


CONTRACTS: dict[str, Contract] = {
    "mcq_knowledge": Contract(
        "Answer with the single option letter only. No explanation.", 8, "letter"),
    "mcq_clinical": Contract(
        "Answer with the single option letter only. No explanation.", 8, "letter"),
    "science_qa": Contract(
        "Answer with the single option letter only. No explanation.", 8, "letter"),
    "cloze_wordsense": Contract(
        "Answer with the single option letter only. No explanation.", 8, "letter"),
    "causal": Contract(
        "Answer with the single option letter only. No explanation.", 8, "letter"),
    "short_answer": Contract(
        "Reply with the answer only -- a name, number or short phrase. No sentence, "
        "no explanation.", 24, "normalised"),
    "reading_comp": Contract(
        "Answer using the shortest exact span from the passage. No explanation.", 32, "normalised"),
    "long_rc": Contract(
        "Answer using the shortest exact span from the passage. No explanation.", 32, "normalised"),
    "math": Contract(
        "Reply with the final answer only. No working, no explanation.", 24, "numeric"),
    "table_numeric": Contract(
        "Reply with the final answer only. No working, no explanation.", 24, "numeric"),
    "nli": Contract(
        "Reply with exactly one word: entailment, neutral, or contradiction.", 6, "exact_word"),
    "translation": Contract(
        "Reply with the translation only. No commentary.", 200, "chrf"),
    # Items that carry tests are executed; the shape check is only a fallback for those that
    # do not (see resolve_grader).
    "code": Contract(
        "Reply with a single Python function in a fenced code block. No prose.", 400, "code_exec"),
}
DEFAULT_CONTRACT = Contract("Answer concisely. No explanation.", 32, "normalised")


# --------------------------------------------------------------------------------------
# Graders
# --------------------------------------------------------------------------------------

_LETTER = re.compile(r"\b([A-J])\b")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_THINK = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)


def strip_thinking(text: str) -> str:
    """Remove <think> blocks. Local models spend their whole token budget in them otherwise."""
    return _THINK.sub("", text).strip()


def _norm(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def grade_letter(out: str, gold: str, item: dict) -> float:
    """MCQ: the extracted letter must match. Falls back to matching the option text."""
    m = _LETTER.search(out.upper())
    gold = (gold or "").strip()
    if m and gold and gold.upper() in "ABCDEFGHIJ":
        return float(m.group(1) == gold.upper())
    # gold is the option's text, not its letter -- resolve the letter through the prompt.
    options = re.findall(r"^([A-J])\.\s*(.+)$", item["prompt"], re.M)
    if m and options:
        chosen = dict(options).get(m.group(1), "")
        return float(_norm(chosen) == _norm(gold))
    return float(_norm(out) == _norm(gold))


def grade_normalised(out: str, gold: str, _item: dict) -> float:
    """Free-form short answer: normalised containment either way."""
    o, g = _norm(out), _norm(gold)
    if not g:
        return 0.0
    if o == g:
        return 1.0
    # gold answers are often one of several aliases, semicolon-separated
    for alt in (x.strip() for x in gold.split(";")):
        if alt and _norm(alt) and (_norm(alt) == o or _norm(alt) in o):
            return 1.0
    return 0.0


def grade_numeric(out: str, gold: str, _item: dict) -> float:
    """Numeric answer within 0.5%, so unit and rounding noise does not read as wrongness."""
    go, oo = _NUM.findall(gold or ""), _NUM.findall(out or "")
    if not go or not oo:
        return grade_normalised(out, gold, _item)
    try:
        g, o = float(go[0]), float(oo[0])
    except ValueError:
        return 0.0
    if g == 0:
        return float(abs(o) < 1e-9)
    return float(abs(o - g) / abs(g) <= 0.005)


def grade_exact_word(out: str, gold: str, _item: dict) -> float:
    return float(_norm(out).split()[:1] == _norm(gold).split()[:1] if _norm(out) else False)


def grade_chrf(out: str, gold: str, _item: dict) -> float:
    """Character n-gram F-score, thresholded. Stabler than BLEU at sentence level."""
    if not out or not gold:
        return 0.0

    def grams(s: str, n: int) -> set[str]:
        s = re.sub(r"\s+", " ", s.lower())
        return {s[i : i + n] for i in range(max(len(s) - n + 1, 0))}

    scores = []
    for n in (2, 4, 6):
        h, r = grams(out, n), grams(gold, n)
        if not h or not r:
            continue
        inter = len(h & r)
        p, rec = inter / len(h), inter / len(r)
        scores.append(0 if p + rec == 0 else 2 * p * rec / (p + rec))
    return float(sum(scores) / len(scores) >= 0.45) if scores else 0.0


def _extract_code(out: str) -> str:
    """Pull the Python body out of a fenced block, or fall back to the whole response."""
    fence = re.search(r"```(?:python)?\s*(.+?)```", out, re.S)
    return fence.group(1) if fence else out


def grade_code_shape(out: str, _gold: str, _item: dict) -> float:
    """Syntax-only fallback, used when execution is unavailable.

    Recorded as ``provisional`` so nothing downstream mistakes it for a pass rate.
    """
    body = _extract_code(out)
    if "def " not in body:
        return 0.0
    try:
        compile(body, "<candidate>", "exec")
    except SyntaxError:
        return 0.0
    return 1.0


def grade_code_exec(out: str, gold: str, item: dict) -> float:
    """Execute the candidate against the item's tests in a separate, restricted process.

    Real pass@1: the model's function must actually satisfy the assertions. Runs in a
    subprocess with a wall-clock limit and an address-space cap so an infinite loop or a
    runaway allocation costs a few seconds rather than the machine -- untrusted generated code
    is being run here, and the sweep executes tens of thousands of these.

    Returns 0.0 when there are no tests to run, which is why the caller only selects this
    grader for items that carry them.
    """
    import subprocess
    import tempfile

    body = _extract_code(out)
    tests = item.get("tests") or []
    if not body.strip() or not tests:
        return 0.0

    # Resource limits are best-effort: macOS refuses some RLIMIT_AS values outright, and a
    # ValueError there would abort every candidate and make the grader silently score zero for
    # everything -- which is worse than no grader, because it looks like uniformly bad models.
    # The subprocess timeout below is the limit we actually rely on.
    preamble = (
        "import resource\n"
        "for _lim, _want in ((resource.RLIMIT_AS, 2 << 30), (resource.RLIMIT_NPROC, 0)):\n"
        "    try:\n"
        "        _soft, _hard = resource.getrlimit(_lim)\n"
        "        _cap = _want if _hard == resource.RLIM_INFINITY else min(_want, _hard)\n"
        "        resource.setrlimit(_lim, (_cap, _hard))\n"
        "    except (ValueError, OSError):\n"
        "        pass\n"
    )
    program = preamble + body + "\n" + "\n".join(tests) + "\nprint('__PASS__')\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(program)
        path = fh.name
    try:
        proc = subprocess.run(  # noqa: S603 - deliberately executing candidate code
            [sys.executable, "-I", path],
            capture_output=True, text=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
        return float("__PASS__" in proc.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return 0.0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def resolve_grader(name: str, item: dict) -> str:
    """Downgrade execution grading to the shape check when an item carries no tests."""
    if name == "code_exec" and not item.get("tests"):
        return "code_shape"
    return name


GRADERS: dict[str, Callable[[str, str, dict], float]] = {
    "letter": grade_letter,
    "normalised": grade_normalised,
    "numeric": grade_numeric,
    "exact_word": grade_exact_word,
    "chrf": grade_chrf,
    "code_shape": grade_code_shape,
    "code_exec": grade_code_exec,
}
# Graders that do not actually establish correctness. Every row they produce is tagged so a
# downstream reader cannot mistake a shape check for a pass rate.
PROVISIONAL_GRADERS = {"code_shape"}


# --------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------


def _post(url: str, payload: dict, headers: dict, timeout: int = 180, retries: int = 4) -> dict:
    """POST with backoff on transient failures.

    Rate limits and upstream 5xx must be retried, not recorded. An un-retried 503 lands in the
    outcome table as a wrong answer, which systematically understates whichever model happened
    to be having a bad minute -- and per-model accuracy is the entire thing we are measuring.
    HTTP 400 is *not* retried: it means the request itself is wrong, and the caller uses it to
    detect which reasoning parameters a model accepts.
    """
    body = json.dumps(payload).encode()
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, body, {"Content-Type": "application/json", **headers}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 400 or exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def call_ollama(model: str, system: str, prompt: str, max_tokens: int) -> dict:
    """Local call. ``think: False`` matters -- otherwise the cap is spent inside <think>."""
    t0 = time.time()
    r = _post(
        f"{OLLAMA}/api/chat",
        {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens, "temperature": 0},
        },
        {},
    )
    return {
        "text": strip_thinking(r.get("message", {}).get("content", "")),
        "in_tokens": r.get("prompt_eval_count") or 0,
        "out_tokens": r.get("eval_count") or 0,
        "latency_ms": int((time.time() - t0) * 1000),
    }


# Reasoning tokens are billed as completion tokens but are NOT bounded by max_tokens, so a
# reasoning-capable model silently ignores the output cap. Measured on a smoke run:
# qwen3.5-flash averaged 6,064 output tokens against caps of 8-400 and accounted for 93% of
# total spend while being one model in eight; disabling reasoning cut it to 18 tokens, 113x
# cheaper, with accuracy unchanged. Turning reasoning off is what makes the cap mean what it says.
#
# Providers disagree on how to turn it off:
#   * most accept {"enabled": false}
#   * gpt-oss rejects that with HTTP 400 and needs {"effort": "low"} plus room to think in
# so we try the strict form first and fall back, caching which one a model accepts.
_REASONING_OFF = {"reasoning": {"enabled": False}}
_REASONING_LOW = {"reasoning": {"effort": "low"}}
_REASONING_MODE: dict[str, dict] = {}


def call_openrouter(model: str, system: str, prompt: str, max_tokens: int) -> dict:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    mode = _REASONING_MODE.get(model)
    if mode is None:
        try:
            out = _call_openrouter_once(model, system, prompt, max_tokens, _REASONING_OFF, key)
            _REASONING_MODE[model] = _REASONING_OFF
            return out
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            _REASONING_MODE[model] = _REASONING_LOW
            mode = _REASONING_LOW
    budget = max_tokens if mode is _REASONING_OFF else max_tokens + 64
    return _call_openrouter_once(model, system, prompt, budget, mode, key)


def _call_openrouter_once(
    model: str, system: str, prompt: str, max_tokens: int, reasoning: dict, key: str
) -> dict:
    t0 = time.time()
    r = _post(
        OPENROUTER,
        {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
            **reasoning,
        },
        {"Authorization": f"Bearer {key}"},
    )
    usage = r.get("usage") or {}
    return {
        "text": strip_thinking((r["choices"][0]["message"].get("content") or "")),
        "in_tokens": usage.get("prompt_tokens", 0),
        "out_tokens": usage.get("completion_tokens", 0),
        "latency_ms": int((time.time() - t0) * 1000),
    }


def load_costs() -> dict[str, tuple[float, float]]:
    """Model -> ($/M input, $/M output), from RouterArena's own published cost table."""
    if not COST_TABLE.exists():
        return {}
    raw = json.loads(COST_TABLE.read_text())
    out = {}
    for name, v in raw.items():
        if isinstance(v, dict) and "input_token_price_per_million" in v:
            out[name] = (v["input_token_price_per_million"], v["output_token_price_per_million"])
    return out


CHEAP_TIER = [
    "qwen/qwen3-235b-a22b-2507",
    "qwen/qwen3.5-9b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.5-flash-02-23",
    "qwen/qwen3-30b-a3b-instruct-2507",
    "deepseek/deepseek-v4-flash",
    "google/gemini-3.1-flash-lite-preview",
    "mistralai/ministral-8b-2512",
]

# RouterArena's cost table and OpenRouter's catalogue disagree on names for the same model.
# Cost must be looked up under RouterArena's key, because that is what the leaderboard will
# price our submission with -- calling it something else would silently mis-cost the run.
COST_ALIASES = {
    "mistralai/ministral-8b-2512": "mistralai/ministral-3-8b-2512",
    "openai/gpt-oss-120b": "openai_gpt-oss-120b",
    "google/gemini-3.1-flash-lite-preview": "gemini-3.1-flash-lite-preview",
}


def resolve_price_key(model: str, costs: dict) -> str | None:
    """RouterArena's price-table key for an OpenRouter model id, or None.

    Hand-maintaining COST_ALIASES did not survive contact with a 26-model screen. The price
    table keys most OpenAI / Google / Z-AI models *without* a vendor prefix (`gpt-4o-mini`,
    `gemini-3-flash-preview`) and one with an underscore (`openai_gpt-oss-120b`), so calling
    them by their OpenRouter id missed the lookup for **15 of 26 models** -- every one then
    priced at $0.00 by the fallback. A model priced at zero does not look broken, it looks
    *excellent*: it wins the cost axis by construction and sorts straight to the top. The
    screen's apparent leader was a missing dictionary entry. Resolve by rule, not by memory.
    """
    if model in costs:
        return model
    alias = COST_ALIASES.get(model)
    if alias in costs:
        return alias
    suffix = re.split(r"[/_]", model)[-1]
    matches = [k for k in costs if re.split(r"[/_]", k)[-1] == suffix]
    # A *unique* match only. Two table rows ending the same way give no way to tell which
    # price belongs to the model being called, and guessing mis-costs the entire run.
    return matches[0] if len(matches) == 1 else None


def assert_all_priced(models: list[str], costs: dict) -> None:
    """Refuse to start a sweep whose results could not be costed.

    Failing here costs a second. Discovering it after a 40,000-call sweep costs the sweep;
    not discovering it costs the conclusion.
    """
    missing = [m for m in models if resolve_price_key(m, costs) is None]
    if missing:
        raise SystemExit(
            "No entry in RouterArena's cost table for:\n  " + "\n  ".join(missing)
            + "\nTheir cost cannot be computed, and RouterArena's own harness would score "
              "them as failed inference. Add a COST_ALIASES entry or drop them from the run."
        )


def load_corpus(limit_per_cluster: int | None) -> list[dict]:
    """Items per cluster, round-robined across the sources feeding that cluster.

    The cap used to be ``rows[:limit]`` over items appended in filename order. That silently
    collapsed a cluster onto whichever source sorted first: after Track F, ``mcq_knowledge``
    draws on arc_challenge, arc_easy, commonsense_qa, mmlu_pro and openbookqa, and a 400-item
    cap would have taken 400 arc_challenge rows and none of the rest -- undoing the corpus
    diversification while the item count still looked exactly right.

    Round-robin instead, so a cap costs each source proportionally and a small source
    (mmlu_pro has 68 items) contributes everything it has rather than being sorted out of
    existence. Deterministic: sources are visited in sorted order.
    """
    by_cluster: dict[str, dict[str, list[dict]]] = {}
    for path in sorted(CORPUS.glob("*.jsonl")):
        if path.name.startswith("_"):
            continue
        for line in path.open():
            it = json.loads(line)
            by_cluster.setdefault(it["cluster"], {}).setdefault(path.stem, []).append(it)

    out: list[dict] = []
    for cluster in sorted(by_cluster):
        sources = by_cluster[cluster]
        names = [n for n in sorted(sources) if sources[n]]
        positions = {n: 0 for n in names}
        taken = 0
        while names and (limit_per_cluster is None or taken < limit_per_cluster):
            remaining = []
            for name in names:
                if limit_per_cluster is not None and taken >= limit_per_cluster:
                    break
                items = sources[name]
                out.append(items[positions[name]])
                positions[name] += 1
                taken += 1
                if positions[name] < len(items):
                    remaining.append(name)
            names = remaining
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", choices=("ollama", "openrouter"), default="ollama")
    ap.add_argument("--models", help="comma-separated; defaults to the cheap tier / local model")
    ap.add_argument("--limit", type=int, help="items per cluster (default: all)")
    ap.add_argument("--out", type=Path, default=OUT / "outcomes.jsonl")
    ap.add_argument("--dry-run-report", action="store_true",
                    help="print per-cluster grader behaviour instead of just a total")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent requests; >1 only makes sense for a remote backend")
    ap.add_argument("--budget", type=float, default=25.0,
                    help="hard USD ceiling; the run aborts rather than exceeding it")
    args = ap.parse_args()

    if args.models:
        models = [m.strip() for m in args.models.split(",")]
    elif args.backend == "openrouter":
        models = CHEAP_TIER
    else:
        models = ["qwen3.5:latest"]

    corpus = load_corpus(args.limit)
    costs = load_costs()
    if args.backend == "openrouter":
        assert_all_priced(models, costs)
    call = call_ollama if args.backend == "ollama" else call_openrouter

    args.out.parent.mkdir(parents=True, exist_ok=True)
    done: set[tuple[str, str]] = set()
    if args.out.exists():
        for line in args.out.open():
            r = json.loads(line)
            done.add((r["prompt_hash"], r["model"]))

    import hashlib
    import threading
    from concurrent.futures import ThreadPoolExecutor

    stats: dict[str, dict[str, Any]] = {}
    spend = 0.0
    t0 = time.time()
    lock = threading.Lock()
    aborted = threading.Event()

    jobs = [
        (model, item, hashlib.sha256(item["prompt"].encode()).hexdigest()[:16])
        for model in models
        for item in corpus
        if (hashlib.sha256(item["prompt"].encode()).hexdigest()[:16], model) not in done
    ]
    print(f"{len(jobs)} calls to make ({len(models)} models x {len(corpus)} items, "
          f"{len(done)} already done), {args.workers} workers, "
          f"budget ceiling ${args.budget:.2f}\n")

    with args.out.open("a") as fh:
        def run_one(job) -> None:
            nonlocal spend
            model, item, ph = job
            if aborted.is_set():
                return
            cluster = item["cluster"]
            c = CONTRACTS.get(cluster, DEFAULT_CONTRACT)
            try:
                r = call(model, c.system, item["prompt"], c.max_tokens)
            except Exception as exc:  # noqa: BLE001 - one bad call must not end the run
                r = {"text": "", "in_tokens": 0, "out_tokens": 0, "latency_ms": 0,
                     "error": f"{type(exc).__name__}: {exc}"}

            grader = resolve_grader(c.grader, item)
            correct = GRADERS[grader](r["text"], item.get("answer", ""), item)
            key = resolve_price_key(model, costs)
            pin, pout = costs[key] if key else (0.0, 0.0)
            cost = r["in_tokens"] * pin / 1e6 + r["out_tokens"] * pout / 1e6
            # The cap is the budget control -- verify the response actually honoured it.
            over = r["out_tokens"] > c.max_tokens * 1.5

            with lock:
                spend += cost
                fh.write(json.dumps({
                    "prompt_hash": ph, "cluster": cluster, "source": item["source"],
                    "model": model, "correct": correct, "grader": grader,
                    "provisional": grader in PROVISIONAL_GRADERS,
                    "cost_usd": cost, "in_tokens": r["in_tokens"], "out_tokens": r["out_tokens"],
                    "latency_ms": r["latency_ms"], "cap": c.max_tokens, "over_cap": over,
                    "error": r.get("error"),
                }) + "\n")
                # Flush every row. A sweep runs for hours; buffered writes mean a crash
                # discards work that was already paid for, and makes --out non-resumable.
                fh.flush()

                s = stats.setdefault(f"{model}|{cluster}", {"n": 0, "ok": 0.0, "over": 0,
                                                            "err": 0, "out": 0})
                s["n"] += 1
                s["ok"] += correct
                s["over"] += int(over)
                s["err"] += int(bool(r.get("error")))
                s["out"] += r["out_tokens"]
                n_done = sum(v["n"] for v in stats.values())

                # A hard ceiling, not a warning. The realistic way this run gets expensive is
                # an output-cap bug, and by the time a human notices a warning the money is
                # already spent -- so stop the run instead.
                if spend > args.budget and not aborted.is_set():
                    aborted.set()
                    print(f"\n!! BUDGET CEILING ${args.budget:.2f} REACHED at ${spend:.4f} "
                          f"after {n_done} calls -- aborting", file=sys.stderr)
                if n_done % 250 == 0:
                    rate = n_done / max(time.time() - t0, 1)
                    eta = (len(jobs) - n_done) / max(rate, 1e-6)
                    print(f"  {n_done}/{len(jobs)}  ${spend:.4f}  "
                          f"{rate:.1f}/s  eta {eta / 60:.0f}m", flush=True)

        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(run_one, jobs))
        else:
            for job in jobs:
                run_one(job)

    # A cluster that scores 0.000 across the board is almost always a broken grader or a
    # mismatched answer key, not a model that cannot do the task. This is precisely how our
    # real submission lost all 59 SuperGLUE-ClozeTest items -- the harness asked for a letter
    # where the gold was option text -- so it is worth failing loudly on rather than reading
    # past in a results table.
    broken = [k for k, s in stats.items() if s["n"] >= 5 and s["err"] / s["n"] > 0.2]
    if broken:
        print("\n!! HIGH ERROR RATE -- these calls failed, they are not model weakness:",
              file=sys.stderr)
        for k in broken:
            print(f"     {k}  {stats[k]['err']}/{stats[k]['n']} failed", file=sys.stderr)

    dead = [k for k, s in stats.items() if s["n"] >= 5 and s["ok"] == 0.0 and s["err"] == 0]
    if dead:
        print("\n!! ZERO-ACCURACY CLUSTERS -- suspect the grader, not the model:", file=sys.stderr)
        for k in dead:
            print(f"     {k}  (n={stats[k]['n']})", file=sys.stderr)

    print(f"\n{len(stats)} (model, cluster) pairs · ${spend:.4f} · {time.time() - t0:.0f}s")
    if args.dry_run_report and stats:
        print(f"\n{'model | cluster':<46}{'n':>5}{'acc':>8}{'avg out':>9}{'over cap':>10}{'errors':>8}")
        for k, s in sorted(stats.items()):
            print(f"{k:<46}{s['n']:>5}{s['ok'] / s['n']:>8.3f}"
                  f"{s['out'] / s['n']:>9.1f}{s['over']:>10}{s['err']:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
