#!/usr/bin/env python3
"""Synthetic G1 evidence (PLAN v16 G1; owner decision D-99, 2026-10-14).

    python3 scripts/g1_synthetic_evidence.py fidelity --stratum sonnet --out docs/measurements
    python3 scripts/g1_synthetic_evidence.py fidelity --stratum local --local-model qwen3:8b
    python3 scripts/g1_synthetic_evidence.py summary --out docs/measurements

Every number this script writes is labelled ``synthetic (D-99)``. All session text is invented
here from a fixed seed. No real prompt is read, and nothing is mixed into organic counts. Every
run works in a scratch ``HOME`` / ``LLM_ROUTER_HOME`` and never touches the operator's state.

``fidelity`` implements docs/prereg/PREREG-P11B-synthetic.md (P1.1-b); read that first.
``summary`` measures P1.2-a (rolling-summary coverage and spawn p95) on synthetic sessions.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent
PREREG = ROOT / "docs" / "prereg" / "PREREG-P11B-synthetic.md"
LABEL = "synthetic (D-99)"
SEED = 20261014
WINDOW = 8192
N_ITEMS = 60
#: category -> items, in generator order (PREREG "Items")
MIX = (("recent", 30), ("early_user", 10), ("early_tool", 10), ("early_assistant", 10))
CYCLES = 9
MAX_ERROR_SHARE = 0.10
BAR = 0.02

SYSTEM_PROMPT = ("You are a coding assistant continuing the session below. Answer the final user "
                 "request using only the session. Be brief.")

_A = ("quokka", "ember", "lantern", "fjord", "marble", "tundra", "cobalt", "saffron", "willow",
      "harbor", "pixel", "juniper", "orbit", "meadow", "basalt", "sorrel", "glacier", "thimble")
_B = ("falcon", "ribbon", "cactus", "anchor", "comet", "velvet", "pepper", "summit", "walnut",
      "quartz", "otter", "lagoon", "beacon", "tinsel", "zephyr", "mosaic", "nectar", "pylon")
_IDENT = ("ledger", "ingest", "batch", "parse", "row", "schema", "queue", "retry", "flush", "merge",
          "window", "cursor", "shard", "offset", "record", "buffer", "stream", "codec")


# ── items ─────────────────────────────────────────────────────────────────────


@dataclass
class Item:
    item_id: str
    category: str
    token: str
    entries: list[dict]            # Claude Code transcript entries, request excluded
    request: str


def _answer_token(rng: random.Random) -> str:
    return f"{rng.choice(_A)}-{rng.choice(_B)}-{rng.randint(100, 999)}"


def _filler(rng: random.Random, kind: str, lines: int = 70) -> str:
    out = []
    for i in range(lines):
        a, b = rng.choice(_IDENT), rng.choice(_IDENT)
        n = rng.randint(2, 97)
        if kind == "code":
            out.append(f"def {a}_{b}_{i}(rows, limit={n}):  # helper {i}\n"
                       f"    return [r for r in rows if r.get('{b}') and len(r) < limit]")
        else:
            out.append(f"2026-10-14T10:{i % 60:02d}:{n:02d} INFO {a}.{b} processed batch {i} "
                       f"rows={n * 13} lag_ms={n * 7}")
    return "\n".join(out)


def _cc(role: str, content: list | str, i: int) -> dict:
    return {"type": role, "uuid": f"e{i}", "isSidechain": False,
            "message": {"role": role, "content": content}}


def build_item(index: int, category: str) -> Item:
    """One synthetic session with one planted answer token (PREREG "Items")."""
    rng = random.Random(SEED * 1000 + index)
    token = _answer_token(rng)
    entries: list[dict] = []

    def add(role: str, content) -> None:
        entries.append(_cc(role, content, len(entries)))

    first = "Set up the ledger ingest service in this repo and keep the existing tests green."
    if category == "early_user":
        first = (f"Set up the ledger ingest service; the staging port must be {token}. "
                 "Keep the existing tests green.")
    add("user", [{"type": "text", "text": first}])
    for c in range(CYCLES):
        tool = "Read" if c % 2 == 0 else "Bash"
        inp = ({"file_path": f"/work/svc/{rng.choice(_IDENT)}_{c}.py"} if tool == "Read"
               else {"command": f"pytest -q tests/test_{rng.choice(_IDENT)}_{c}.py"})
        add("assistant", [{"type": "text", "text": f"Checking step {c + 1}."},
                          {"type": "tool_use", "id": f"t{c}", "name": tool, "input": inp}])
        body = _filler(rng, "code" if tool == "Read" else "log")
        if category == "early_tool" and c == 1:
            body = body + f'\n\n# config\ningest_salt = "{token}"\nretry_limit = 5'
        if category == "recent" and c == CYCLES - 1:
            body = body + f"\n\nFAILED tests/test_ingest.py::test_flush[{token}] - AssertionError"
        add("user", [{"type": "tool_result", "tool_use_id": f"t{c}", "content": body}])
        note = f"Step {c + 1} done; moving on."
        if category == "early_assistant" and c == 1:
            note = f"I'll name the new module {token} so it does not clash with the old one."
        add("assistant", [{"type": "text", "text": note}])
        if c < CYCLES - 1:
            add("user", [{"type": "text", "text": f"Good. Continue with step {c + 2}."}])
    request = {
        "recent": "Which parametrized case failed in the last test run? Give the id inside the brackets.",
        "early_user": "Before I deploy: which staging port did I ask for at the very start?",
        "early_tool": "What value did ingest_salt have in the config you read earlier?",
        "early_assistant": "What name did you say you would give the new module?",
    }[category]
    return Item(f"p11b-{index:03d}-{category}", category, token, entries, request)


def items() -> list[Item]:
    out: list[Item] = []
    for category, n in MIX:
        for _ in range(n):
            out.append(build_item(len(out), category))
    return out


# ── arms ──────────────────────────────────────────────────────────────────────


def _render(blocks: list[dict], role: str, *, keep_tools: bool) -> str:
    from llm_router.context_pack import extract_turn_text

    return f"[{role}]\n{extract_turn_text(blocks, keep_tools=keep_tools, cap=False).strip()}"


def full_context(item: Item) -> str:
    parts = [_render(e["message"]["content"], e["message"]["role"], keep_tools=True)
             for e in item.entries]
    return "\n\n".join(parts + [f"[user]\n{item.request}"])


def hook6_context(item: Item) -> str:
    from llm_router.context_pack import extract_turn_text

    texts = [(e["message"]["role"], extract_turn_text(e["message"]["content"], keep_tools=False).strip())
             for e in item.entries]
    texts = [(r, t) for r, t in texts if t][-6:]
    return "\n\n".join([f"[{r}]\n{t}" for r, t in texts] + [f"[user]\n{item.request}"])


def pack_context(item: Item, transcript: Path, provider: str) -> tuple[str, str]:
    """(serialized PACK context, pack mode). The rolling summary must already be stored."""
    from llm_router.context_pack import build_pack, to_messages

    pack = build_pack(item.request, door="hook", session_id=item.item_id,
                      transcript_path=str(transcript), target_window=WINDOW,
                      target_provider=provider)
    parts = []
    for m in to_messages(pack):
        role = "context" if m["role"] == "system" else m["role"]
        parts.append(f"[{role}]\n{m['content']}")
    return "\n\n".join(parts), pack.mode


def write_transcript(item: Item, directory: Path) -> Path:
    path = directory / f"{item.item_id}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in item.entries), encoding="utf-8")
    return path


# ── grading and statistics ────────────────────────────────────────────────────


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", (text or "").lower())


def passes(answer: str, token: str) -> bool:
    return normalize(token) in normalize(answer)


def mcnemar_exact(worse: int, better: int) -> float:
    """Two-sided exact McNemar p (binomial on the discordant pairs, p = 0.5)."""
    n = worse + better
    if n == 0:
        return 1.0
    k = min(worse, better)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def verdict(rows: list[dict], *, total: int) -> dict:
    """Stratum verdict from per-item rows ``{category, pack, ref, error}`` (PREREG run rule)."""
    errors = sum(1 for r in rows if r["error"])
    ok = [r for r in rows if not r["error"]]
    n = len(ok)
    worse = sum(1 for r in ok if r["ref"] and not r["pack"])
    better = sum(1 for r in ok if r["pack"] and not r["ref"])
    share = (worse - better) / n if n else None
    informative = n > 0 and errors <= MAX_ERROR_SHARE * total
    by_cat: dict[str, dict] = {}
    for r in ok:
        c = by_cat.setdefault(r["category"], {"n": 0, "pack_pass": 0, "ref_pass": 0, "worse": 0, "better": 0})
        c["n"] += 1
        c["pack_pass"] += r["pack"]
        c["ref_pass"] += r["ref"]
        c["worse"] += r["ref"] and not r["pack"]
        c["better"] += r["pack"] and not r["ref"]
    return {
        "n": n, "errors": errors, "items": total,
        "pack_pass": sum(r["pack"] for r in ok), "ref_pass": sum(r["ref"] for r in ok),
        "worse": worse, "better": better,
        "worse_minus_better_over_n": None if share is None else round(share, 4),
        "mcnemar_exact_p": mcnemar_exact(worse, better),
        "bar": BAR,
        "verdict": ("NOT INFORMATIVE" if not informative else
                    "PASS" if share is not None and share <= BAR else "FAIL"),
        "by_category": by_cat,
    }


# ── responders ────────────────────────────────────────────────────────────────


@dataclass
class Answer:
    text: str = ""
    error: str | None = None
    model: str | None = None
    cost_usd: float = 0.0


def claude_responder(model: str = "sonnet", timeout: float = 180.0) -> Callable[[str], Answer]:
    def call(context: str) -> Answer:
        argv = ["claude", "-p", "--model", model, "--safe-mode", "--tools", "",
                "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
                "--system-prompt", SYSTEM_PROMPT]
        try:
            proc = subprocess.run(argv, input=context, capture_output=True, text=True,
                                  timeout=timeout, cwd=tempfile.gettempdir())
            data = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            return Answer(error=type(exc).__name__)
        if data.get("is_error") or not str(data.get("result", "")).strip():
            return Answer(error="cli_error")
        models = list((data.get("modelUsage") or {}).keys())
        return Answer(text=str(data["result"]), model=models[0] if models else None,
                      cost_usd=float(data.get("total_cost_usd") or 0.0))
    return call


def ollama_responder(model: str, url: str = "http://127.0.0.1:11434",
                     timeout: float = 600.0) -> Callable[[str], Answer]:
    def call(context: str) -> Answer:
        body = json.dumps({"model": model, "stream": False, "options": {"temperature": 0},
                           "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                        {"role": "user", "content": context}]}).encode()
        req = urllib.request.Request(f"{url}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback
                data = json.loads(resp.read())
        except Exception as exc:                                   # noqa: BLE001
            return Answer(error=type(exc).__name__)
        text = str((data.get("message") or {}).get("content", "")).strip()
        return Answer(text=text, model=model) if text else Answer(error="empty")
    return call


def _with_retry(call: Callable[[str], Answer], context: str) -> Answer:
    first = call(context)
    return first if first.error is None else call(context)


# ── scratch state ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def scratch_env(extra: dict[str, str] | None = None) -> Iterator[Path]:
    """A fresh HOME / LLM_ROUTER_HOME for this process; the operator's state is never read."""
    keep = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("LLM_ROUTER_")}
    keep.update({k: os.environ.get(k) for k in ("HOME",)})
    with tempfile.TemporaryDirectory(prefix="g1-synth-") as tmp:
        root = Path(tmp)
        for k in list(os.environ):
            if k.startswith("LLM_ROUTER_"):
                del os.environ[k]
        os.environ.update({"HOME": str(root / "home"), "LLM_ROUTER_HOME": str(root / "lr"),
                           "LLM_ROUTER_SESSIONS_PATH": str(root / "lr" / "sessions.db"),
                           "LLM_ROUTER_SESSION_CONTEXT": "all", "LLM_ROUTER_ROLLING_SUMMARY": "1",
                           "LLM_ROUTER_SYNTHETIC": "1", **(extra or {})})
        (root / "home").mkdir()
        (root / "lr").mkdir()
        try:
            yield root
        finally:
            for k in list(os.environ):
                if k.startswith("LLM_ROUTER_"):
                    del os.environ[k]
            for k, v in keep.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def prereg_sha256() -> str:
    return hashlib.sha256(PREREG.read_bytes()).hexdigest()


# ── fidelity (P1.1-b) ─────────────────────────────────────────────────────────


def run_fidelity(stratum: str, responder: Callable[[str], Answer], *, provider: str,
                 summary_model: str | None = None, limit: int | None = None,
                 progress: Callable[[str], None] = lambda s: None) -> dict:
    from llm_router import context_summary

    chosen = items()[: limit or N_ITEMS]
    rows, models, cost, modes = [], set(), 0.0, {}
    with scratch_env() as root:
        tdir = root / "transcripts"
        tdir.mkdir()
        for it in chosen:
            path = write_transcript(it, tdir)
            context_summary.maybe_update(it.item_id, path, model=summary_model, free_pct=100.0)
            pack, mode = pack_context(it, path, provider)
            modes[mode] = modes.get(mode, 0) + 1
            ref = full_context(it) if stratum == "sonnet" else hook6_context(it)
            a_pack, a_ref = _with_retry(responder, pack), _with_retry(responder, ref)
            for a in (a_pack, a_ref):
                cost += a.cost_usd
                if a.model:
                    models.add(a.model)
            err = a_pack.error or a_ref.error
            row = {"item": it.item_id, "category": it.category, "error": err,
                   "pack": bool(not err and passes(a_pack.text, it.token)),
                   "ref": bool(not err and passes(a_ref.text, it.token))}
            rows.append(row)
            progress(json.dumps(row))
    reference = "FULL" if stratum == "sonnet" else "HOOK6"
    return {
        "label": LABEL, "must": "P1.1-b", "stratum": stratum, "arms": f"PACK vs {reference}",
        "prereg": PREREG.name, "prereg_sha256": prereg_sha256(), "seed": SEED, "window": WINDOW,
        "models": sorted(models), "summary_model": summary_model or "extractive",
        "pack_modes": modes, "cost_usd": round(cost, 4),
        **verdict(rows, total=len(chosen)), "rows": rows,
    }


# ── summary coverage (P1.2-a) ─────────────────────────────────────────────────


def run_summary(n_sessions: int = 24, spawns: int = 100) -> dict:
    """P1.2-a on synthetic sessions: every session with >= 12 messages gets a stored summary
    when its Stop hook runs the worker; spawn() p95 over ``spawns`` calls."""
    from llm_router import context_summary

    with scratch_env() as root:
        tdir = root / "transcripts"
        tdir.mkdir()
        counts: dict[str, int] = {}
        worker_failures = 0
        for i in range(n_sessions):
            it = build_item(10_000 + i, MIX[i % len(MIX)][0])
            path = write_transcript(it, tdir)
            sid = f"synth-p12a-{i:03d}"
            counts[sid] = len(context_summary.read_messages(path))
            # The Stop hook's worker, run in-process (spawn() only starts it detached).
            # coverage() counts a session only if a summary was actually stored.
            worker_failures += context_summary._worker(["--session", sid, "--transcript", str(path)]) != 0
        cov = context_summary.coverage(counts, min_messages=12)
        times = []
        noop = [sys.executable, "-c", "pass"]
        for i in range(spawns):
            t0 = time.perf_counter()
            context_summary.spawn(f"synth-spawn-{i:03d}", str(tdir / "x.jsonl"), argv=noop)
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        p95 = times[min(len(times) - 1, math.ceil(0.95 * len(times)) - 1)]
    return {"label": LABEL, "must": "P1.2-a", "coverage": cov, "spawn_ms_p95": round(p95, 2),
            "spawns": spawns, "worker_failures": worker_failures, "bars": {"coverage_pct": 95, "spawn_ms_p95": 50, "n_sessions": 20},
            "verdict": ("PASS" if cov["informative"] and (cov["pct"] or 0) >= 95 and p95 <= 50
                        else "FAIL")}


# ── CLI ───────────────────────────────────────────────────────────────────────


def _write(out: Path, name: str, data: dict) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fidelity")
    f.add_argument("--stratum", choices=("sonnet", "local"), required=True)
    f.add_argument("--local-model")
    f.add_argument("--summary-model")
    f.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    f.add_argument("--limit", type=int)
    f.add_argument("--out", type=Path, default=ROOT / "docs" / "measurements")
    s = sub.add_parser("summary")
    s.add_argument("--out", type=Path, default=ROOT / "docs" / "measurements")
    args = ap.parse_args(argv)

    if args.cmd == "fidelity":
        if args.stratum == "sonnet":
            responder, provider = claude_responder("sonnet"), "anthropic"
        else:
            if not args.local_model:
                ap.error("--stratum local needs --local-model")
            responder, provider = ollama_responder(args.local_model, args.ollama_url), "ollama"
        res = run_fidelity(args.stratum, responder, provider=provider,
                           summary_model=args.summary_model, limit=args.limit,
                           progress=lambda s: print(s, file=sys.stderr, flush=True))
        name = f"p11b-synthetic-{args.stratum}.json" if not args.limit else \
            f"p11b-synthetic-{args.stratum}-limit{args.limit}.json"
    else:
        res = run_summary()
        name = "p12a-synthetic.json"
    path = _write(args.out, name, res)
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=2, sort_keys=True))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
