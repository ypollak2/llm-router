#!/usr/bin/env python3
"""Synthetic G1 evidence (PLAN v16 G1; owner decision D-99, 2026-10-14).

    python3 scripts/g1_synthetic_evidence.py fidelity --stratum sonnet --out docs/measurements
    python3 scripts/g1_synthetic_evidence.py fidelity --stratum local --local-model qwen3:8b
    python3 scripts/g1_synthetic_evidence.py summary --out docs/measurements
    python3 scripts/g1_synthetic_evidence.py o3
    python3 scripts/g1_synthetic_evidence.py replay-corpus --corpus /tmp/g1.jsonl
    python3 scripts/synthetic_replay.py --corpus /tmp/g1.jsonl --out /tmp/g1-replay --keep-work
    python3 scripts/g1_synthetic_evidence.py identity --work-dir <kept root> --summary /tmp/g1-replay/summary.json

Every number this script writes is labelled ``synthetic (D-99)``. All session text is invented
here from a fixed seed. No real prompt is read, and nothing is mixed into organic counts. Every
run works in a scratch ``HOME`` / ``LLM_ROUTER_HOME`` and never touches the operator's state.

``fidelity`` implements docs/prereg/PREREG-P11B-synthetic.md (P1.1-b); read that first.
``summary`` measures P1.2-a (rolling-summary coverage and spawn p95) on synthetic sessions.
``o3`` checks P1.9-a (O3 turns / typed prompts, D-58 population rules) on synthetic transcripts and ledger.
``replay-corpus`` + ``identity`` measure P1.10-a per path and the P0.9-a/b hook latencies through the
$0 stub-upstream replay (``scripts/synthetic_replay.py``, D-42 world).
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
#: PLAN v16 P1.1-b: "Until n >= 60 paired the result is 'not informative'" (60 paired per stratum).
MIN_PAIRED = 60
#: Superseding rule file (stricter run rule: n >= 60 paired, smoke runs never scored), frozen after
#: the Sonnet stratum ran; it governs every later fidelity run.
AMEND = ROOT / "docs" / "prereg" / "PREREG-P11B-synthetic-AMEND1.md"

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
    informative = n >= MIN_PAIRED and errors <= MAX_ERROR_SHARE * total
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
        "bar": BAR, "min_paired": MIN_PAIRED,
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


def loopback_base_url(env: dict | None = None) -> str | None:
    """``ANTHROPIC_BASE_URL`` when it points at this machine (e.g. the live router proxy on :8787),
    else None. A synthetic run through it would land unlabelled in the live ledger."""
    import urllib.parse

    raw = (env if env is not None else os.environ).get("ANTHROPIC_BASE_URL") or ""
    host = (urllib.parse.urlparse(raw).hostname or "").lower()
    return raw if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.startswith("127.") else None


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
        try:
            (root / "home").mkdir()
            (root / "lr").mkdir()
            for k in list(os.environ):
                if k.startswith("LLM_ROUTER_"):
                    del os.environ[k]
            os.environ.update({"HOME": str(root / "home"), "LLM_ROUTER_HOME": str(root / "lr"),
                               "LLM_ROUTER_SESSIONS_PATH": str(root / "lr" / "sessions.db"),
                               "LLM_ROUTER_SESSION_CONTEXT": "all", "LLM_ROUTER_ROLLING_SUMMARY": "1",
                               "LLM_ROUTER_SYNTHETIC": "1", **(extra or {})})
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
    scored = verdict(rows, total=len(chosen))
    if limit:  # a --limit run checks the harness; it is never a result (AMEND1)
        scored["verdict"] = "SMOKE (not scored)"
    return {
        "label": LABEL, "must": "P1.1-b", "stratum": stratum, "arms": f"PACK vs {reference}",
        "prereg": PREREG.name, "prereg_sha256": prereg_sha256(), "seed": SEED, "window": WINDOW,
        "models": sorted(models), "summary_model": summary_model or "extractive",
        "pack_modes": modes, "cost_usd": round(cost, 4),
        "amend_sha256": hashlib.sha256(AMEND.read_bytes()).hexdigest() if AMEND.exists() else None,
        **scored, "rows": rows,
    }


# ── summary coverage (P1.2-a) ─────────────────────────────────────────────────


def run_summary(n_sessions: int = 100, wait_s: float = 300.0) -> dict:
    """P1.2-a on synthetic sessions. For each session, ``context_summary.spawn()`` (what the Stop
    hook calls) starts the real detached worker with its default argv. Coverage counts sessions
    with a stored summary once the workers are done (or ``wait_s`` passed). Spawn p95 is the wall
    time of those same ``spawn()`` calls. The Stop hook process itself is not run here: its wall
    time is P0.9's."""
    from llm_router import context_summary

    src = str(ROOT / "src")
    pythonpath = os.pathsep.join([src] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])
    with scratch_env({"PYTHONPATH": pythonpath}) as root:
        tdir = root / "transcripts"
        tdir.mkdir()
        counts: dict[str, int] = {}
        times: list[float] = []
        started = 0
        for i in range(n_sessions):
            it = build_item(10_000 + i, MIX[i % len(MIX)][0])
            path = write_transcript(it, tdir)
            sid = f"synth-p12a-{i:03d}"
            counts[sid] = len(context_summary.read_messages(path))
            t0 = time.perf_counter()
            started += bool(context_summary.spawn(sid, str(path)))
            times.append((time.perf_counter() - t0) * 1000)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            cov = context_summary.coverage(counts, min_messages=12)
            if cov["covered"] == cov["n_sessions"]:
                break
            time.sleep(0.5)
        cov = context_summary.coverage(counts, min_messages=12)
    return {"label": LABEL, "must": "P1.2-a", "workers_started": started,
            "method": "spawn() with the real detached worker per session; Stop hook process not run",
            **summary_verdict(cov, times)}


def summary_verdict(cov: dict, spawn_ms: list[float]) -> dict:
    """P1.2-a bars: coverage >= 95% of >= 20 sessions, spawn p95 <= 50 ms over >= 100 spawns."""
    times = sorted(spawn_ms)
    p95 = times[min(len(times) - 1, math.ceil(0.95 * len(times)) - 1)] if times else None
    verdict = ("NOT INFORMATIVE" if not cov.get("informative") or len(times) < 100 else
               "PASS" if (cov.get("pct") or 0) >= 95 and p95 <= 50 else "FAIL")
    return {"coverage": cov, "spawn_ms_p95": None if p95 is None else round(p95, 2), "spawns": len(times),
            "bars": {"coverage_pct": 95, "spawn_ms_p95": 50, "n_sessions": 20, "n_spawns": 100},
            "verdict": verdict}


# ── O3 integrity (P1.9-a) ─────────────────────────────────────────────────────

O3_BAR = (0.90, 1.10)
O3_MIN_PROMPTS = 50
O3_MIN_COVERAGE = 0.95
#: (session suffix, typed prompts, share of continuation ledger rows lost, proxy down for the 2nd half)
O3_SESSIONS = (("a", 60, 0.0, False), ("b", 75, 0.03, False), ("c", 52, 0.0, False),
               ("d", 55, 0.0, True), ("e", 30, 0.0, False))
#: per-prompt rates of the shapes that broke the M0.3 count (PR #295) and of the other host shapes
O3_RATES = {"side_call": 0.25, "orphan": 0.15, "failed_retry": 0.05, "subagent": 0.12,
            "subagent_mislabel": 0.3, "notification": 0.10, "command": 0.05, "typed_beside": 0.03,
            "interrupt": 0.03}
_EPOCH = 1_791_300_000.0


def _iso(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat().replace("+00:00", "Z")


def build_o3_session(sid: str, n_prompts: int, *, lose: float = 0.0, half_down: bool = False,
                     seed: int = SEED) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    """(main transcript entries, {subagent file name: entries}, proxy ledger rows) for one synthetic
    session with ``n_prompts`` typed prompts. Claude Code shapes: one assistant message written as
    several entries, tool loops, sub-agents in ``subagents/``, task notifications (``promptSource
    system``), slash commands, prompts typed beside tool results, interrupts, one compaction; ledger
    shapes: side calls, calls the transcript never holds, failed calls retried, mislabelled sub-agent
    first calls, lost rows. Rows carry ``session_kind="synthetic"``."""
    rng = random.Random(f"{seed}-{sid}")
    main: list[dict] = []
    subs: dict[str, list[dict]] = {}
    rows: list[dict] = []
    t = _EPOCH
    seq = 0

    def tick(lo: float = 2.0, hi: float = 20.0) -> float:
        nonlocal t
        t += rng.uniform(lo, hi)
        return t

    def mid() -> str:
        nonlocal seq
        seq += 1
        return f"msg_{sid}_{seq:05d}"

    def user(content, **extra) -> None:
        main.append({"type": "user", "sessionId": sid, "timestamp": _iso(tick(1, 3)), "isSidechain": False,
                     "message": {"role": "user", "content": content}, **extra})

    def row(msg_id, step, **extra) -> None:
        rows.append({"ts": t, "session_id": sid, "msg_id": msg_id, "step_class": step,
                     "decision": "forwarded", "upstream_status": 200, "served_model": "claude-sonnet-5-5",
                     "requested_model": "claude-sonnet-5-5", "tier_reason": "policy",
                     "session_kind": "synthetic", **extra})

    def assistant(step: str, *, tool: str | None = None, ledger: bool = True, **row_extra) -> str:
        m = mid()
        ts = _iso(tick())
        blocks = [{"type": "text", "text": "Working on it."}]
        if tool:
            blocks.append({"type": "tool_use", "id": f"tu_{m}", "name": tool, "input": {}})
        for b in blocks:  # Claude Code writes one entry per content block, same message id
            main.append({"type": "assistant", "sessionId": sid, "timestamp": ts, "isSidechain": False,
                         "message": {"id": m, "role": "assistant", "content": [b]}})
        if ledger:
            row(m, step, **row_extra)
        return m

    def tool_result(tid: str, typed: str | None = None) -> None:
        content = [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]
        if typed:
            content.append({"type": "text", "text": typed})
        user(content)

    for p in range(n_prompts):
        down = half_down and p >= n_prompts // 2
        if p == n_prompts // 2 and n_prompts >= 60:      # compaction: an orphan call + the host summary
            row(mid(), "turn_first")
            user("This session is being continued from a previous conversation.",
                 isCompactSummary=True, isVisibleInTranscriptOnly=True)
        user([{"type": "text", "text": f"Prompt {p + 1}: change the ingest step."}])
        if rng.random() < O3_RATES["side_call"]:
            row(mid(), "turn_first", tier_reason="side_call")   # permission classifier
        if rng.random() < O3_RATES["orphan"]:
            row(mid(), "turn_first")                            # prompt suggestion / title: not in transcript
        if rng.random() < O3_RATES["failed_retry"]:
            row(None, "turn_first", upstream_status=529)        # failed: no message id
        loops = rng.randint(0, 3)
        m = assistant("turn_first", tool="Bash" if loops else None, ledger=not down)
        if rng.random() < O3_RATES["interrupt"]:
            user([{"type": "text", "text": "[Request interrupted by user]"}])
            continue
        for k in range(loops):
            if rng.random() < O3_RATES["subagent"]:
                name = f"agent-{sid}-{p}-{k}.jsonl"
                entries = [{"type": "user", "isSidechain": True, "timestamp": _iso(tick(1, 2)),
                            "message": {"role": "user", "content": [{"type": "text", "text": "brief"}]}}]
                for j in range(rng.randint(1, 3)):
                    sm = mid()
                    entries.append({"type": "assistant", "isSidechain": True, "timestamp": _iso(tick()),
                                    "message": {"id": sm, "role": "assistant",
                                                "content": [{"type": "text", "text": "sub"}]}})
                    if not down:
                        first_step = ("turn_first" if rng.random() < O3_RATES["subagent_mislabel"]
                                      else "subagent_first")
                        row(sm, first_step if j == 0 else "subagent_turn")
                subs[name] = entries
            typed = "also keep the old flag" if rng.random() < O3_RATES["typed_beside"] else None
            tool_result(f"tu_{m}", typed)
            step = "turn_first" if typed else "continuation"
            lost = step == "continuation" and rng.random() < lose
            m = assistant(step, tool="Bash" if k < loops - 1 else None, ledger=not (down or lost))
        if rng.random() < O3_RATES["notification"]:
            user([{"type": "text", "text": "<task-notification>background job done</task-notification>"}],
                 promptSource="system")
            assistant("turn_first", ledger=not down,
                      **({"turn_origin": "task_notification"} if rng.random() < 0.5 else {}))
        if rng.random() < O3_RATES["command"]:
            user([{"type": "text", "text": "<command-name>/review</command-name>"}])
            user([{"type": "text", "text": "Review the diff."}], isMeta=True)
            assistant("turn_first", ledger=not down,
                      **({"turn_origin": "command"} if rng.random() < 0.5 else {}))
    return main, subs, rows


def write_o3_session(projects: Path, sid: str, main: list[dict], subs: dict[str, list[dict]]) -> None:
    proj = projects / "-work-synthetic"
    (proj / sid / "subagents").mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in main), encoding="utf-8")
    for name, entries in subs.items():
        (proj / sid / "subagents" / name).write_text("".join(json.dumps(e) + "\n" for e in entries),
                                                     encoding="utf-8")


def _parse_iso(raw) -> float | None:
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def o3_integrity(projects: Path, proxy_rows: list[dict], *, since: float, until: float,
                 kinds: frozenset[str] = frozenset({"organic"})) -> dict:
    """P1.9 variant of the M0.3 integrity check (D-58), per session: O3 human turns (the units
    ``kpi`` counts, ``offload_share.build_units`` + ``turn_units`` with the transcript join) over
    typed prompts (``o3_transcripts.is_typed_prompt``) in the window, with proxy coverage = main-thread
    assistant message ids found in the ledger / all of them. A session is a gate unit when its kind
    is in ``kinds``, it has >= 50 typed prompts and >= 1 proxy turn; under 95% coverage it is
    UNMEASURABLE (never a fail)."""
    from llm_router import o3_transcripts as o3t
    from llm_router import offload_share as osh

    days = (until - since) / 86400.0
    built = osh.build_units(proxy_rows, [], now=until, days=days, kind_of=lambda sid, stamp: stamp,
                            allowed=kinds, thread_of=o3t.thread_lookup(projects_dir=projects))
    turns: dict[str, int] = {}
    for u in osh.turn_units(built["units"]):
        turns[u["session_id"]] = turns.get(u["session_id"], 0) + 1
    ledger_ids: dict[str, set] = {}
    kind: dict[str, str] = {}
    for r in proxy_rows:
        ledger_ids.setdefault(r.get("session_id"), set()).add(r.get("msg_id"))
        kind.setdefault(r.get("session_id"), r.get("session_kind"))
    sessions = {}
    for path in sorted(projects.glob("*/*.jsonl")):
        sid = path.stem
        prompts, main_ids = 0, set()
        for line in path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            ts = _parse_iso(e.get("timestamp"))
            if ts is None or not since <= ts <= until:
                continue
            if o3t.is_typed_prompt(e):
                prompts += 1
            if e.get("type") == "assistant" and not e.get("isSidechain"):
                main_ids.add(e["message"]["id"])
        cov = len(main_ids & ledger_ids.get(sid, set())) / len(main_ids) if main_ids else None
        n_turns = turns.get(sid, 0)
        ratio = n_turns / prompts if prompts else None
        if kind.get(sid) not in kinds or prompts < O3_MIN_PROMPTS or n_turns < 1:
            state = "NOT A GATE UNIT"
        elif cov is None or cov < O3_MIN_COVERAGE:
            state = "UNMEASURABLE"
        else:
            state = "PASS" if O3_BAR[0] <= ratio <= O3_BAR[1] else "FAIL"
        sessions[sid] = {"typed_prompts": prompts, "o3_turns": n_turns,
                         "ratio": None if ratio is None else round(ratio, 4),
                         "coverage": None if cov is None else round(cov, 4), "state": state}
    units = [s for s in sessions.values() if s["state"] in ("PASS", "FAIL")]
    verdict = ("NOT INFORMATIVE" if len(units) < 2 else
               "PASS" if all(s["state"] == "PASS" for s in units) else "FAIL")
    excluded = {k: built[k] for k in ("side_call_excluded", "subagent_first", "meta_first", "unjoined",
                                      "ledger_not_human", "no_transcript", "step_subagent_first",
                                      "step_subagent_turn")}
    return {"sessions": sessions, "measurable": len(units), "verdict": verdict, "excluded": excluded,
            "bar": list(O3_BAR), "min_prompts": O3_MIN_PROMPTS, "min_coverage": O3_MIN_COVERAGE}


def run_o3() -> dict:
    """P1.9-a on synthetic sessions (D-99): the counting rule against the host and ledger shapes
    that broke the M0.3 count. Kind ``synthetic`` stands in for ``organic``."""
    with scratch_env() as root:
        projects = root / "projects"
        rows: list[dict] = []
        shape: dict[str, dict] = {}
        for suffix, n, lose, down in O3_SESSIONS:
            sid = f"synth-o3-{suffix}"
            main, subs, r = build_o3_session(sid, n, lose=lose, half_down=down)
            write_o3_session(projects, sid, main, subs)
            rows.extend(r)
            shape[sid] = {"planned_prompts": n, "lost_continuations": lose, "proxy_down_2nd_half": down}
        until = max(r["ts"] for r in rows) + 60
        res = o3_integrity(projects, rows, since=_EPOCH - 60, until=until, kinds=frozenset({"synthetic"}))
    return {"label": LABEL, "must": "P1.9-a", "seed": SEED, "rates": O3_RATES, "design": shape, **res}


# ── replay corpus (P1.10-a, P0.9-a/b) ─────────────────────────────────────────

_ASKS = (("code/moderate", "Add a retry with backoff to the {a} {b} client and cover it with a test."),
         ("query/simple", "Why does the {a} {b} step return None on an empty batch?"),
         ("analyze/moderate", "Compare the {a} and {b} queue settings and say which one drops fewer rows."),
         ("generate/simple", "Write a short docstring for the {a}_{b} helper."),
         ("research/moderate", "Survey how three libraries handle {a} {b} timeouts and summarise."),
         ("code/complex", "Refactor the {a} {b} pipeline into stages with a pluggable sink; keep tests green."))


def replay_corpus(n_sessions: int = 60, turns: int = 5) -> list[dict]:
    """A ``scripts/synthetic_replay.py`` corpus (its README format): ``n_sessions`` sessions of
    ``turns`` human turns, each a turn-first and a tool continuation; every turn after the first also
    launches one sub-agent (its first call, one sub-agent continuation, the parent's Agent result)."""
    rng = random.Random(SEED + 7)
    out: list[dict] = []
    for s in range(n_sessions):
        sid, t = f"g1-replay-{s:03d}", 0.0

        def rec(turn, step, messages, cat, thread="main", lang="en"):
            nonlocal t
            t += 2.0
            out.append({"session_id": sid, "turn": turn, "step": step, "role": "user", "messages": messages,
                        "intended_category": cat, "lang": lang, "thread": thread, "t": t})

        for turn in range(1, turns + 1):
            cat, ask = rng.choice(_ASKS)
            text = ask.format(a=rng.choice(_IDENT), b=rng.choice(_IDENT))
            lead = [] if turn == 1 else [{"role": "assistant", "content": [{"type": "text", "text": "Done."}]}]
            rec(turn, "turn_first", lead + [{"role": "user", "content": [{"type": "text", "text": text}]}], cat)
            tid = f"t{turn}"
            rec(turn, "continuation", [
                {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                                   "input": {"command": f"pytest -q tests/test_{turn}.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "4 passed"}]}],
                cat, lang="n/a")
            if turn == 1:
                continue
            brief = f"Inspect the {rng.choice(_IDENT)} module and report its public functions in five lines."
            thread = f"sub-{turn}-1"
            rec(turn, "subagent", [{"role": "user", "content": [{"type": "text", "text": brief}]}],
                "analyze/simple", thread=thread)
            rec(turn, "continuation", [
                {"role": "assistant", "content": [{"type": "tool_use", "id": f"s{turn}", "name": "Bash",
                                                   "input": {"command": "ls /work/svc"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"s{turn}",
                                              "content": "ingest.py ledger.py"}]}],
                "analyze/simple", thread=thread, lang="n/a")
            rec(turn, "continuation", [
                {"role": "assistant", "content": [{"type": "tool_use", "id": f"a{turn}", "name": "Agent",
                                                   "input": {"description": "inspect module", "prompt": brief,
                                                             "subagent_type": "general-purpose"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"a{turn}",
                                              "content": "Two public functions."}]}],
                cat, lang="n/a")
    return out


#: ``scripts/synthetic_replay.PROXY_VARIANTS`` as session-id suffixes.
_PROBE_SUFFIXES = ("-nosys", "-bare", "-reminder")


def _identity_rows(state: Path) -> dict[str, list[dict]]:
    """``{path: [{session_id, task_id}]}`` from a replay's scratch state (the kpi P1.10-a paths,
    plus execution events and edit outcomes when they have rows)."""
    import sqlite3

    paths: dict[str, list[dict]] = {}
    for name in ("proxy_calls.jsonl", "edit_outcomes.jsonl"):
        f = state / name
        if f.exists():
            rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
            if rows:
                paths[name.split(".")[0].replace("_calls", "")] = rows
    db = state / "usage.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for path, sql in (
                    ("usage", "SELECT session_id, task_id FROM usage"),
                    ("routing_decisions", "SELECT session_id, task_id FROM routing_decisions "
                                          "WHERE reason_code IS NULL OR reason_code != 'direct'"),
                    ("direct", "SELECT session_id, task_id FROM routing_decisions WHERE reason_code = 'direct'"),
                    ("execution_events", "SELECT session_id, task_id FROM execution_events")):
                try:
                    rows = [{"session_id": a, "task_id": b} for a, b in con.execute(sql)]
                except sqlite3.Error:
                    continue
                if rows:
                    paths[path] = rows
        finally:
            con.close()
    return paths


def identity_from_replay(state: Path, summary: dict) -> dict:
    """P1.10-a per path (kpi's rule: session_id not ``unknown:*`` and a real task_id, ``gen-`` is
    missing; bar 99%, 200 rows per path) and the P0.9-a/b hook latencies, from one replay run."""
    from llm_router.commands import kpi

    rows_by_path = _identity_rows(state)
    if "proxy" in rows_by_path:
        # synthetic_replay.proxy_body also sends every turn-first record as three probe envelopes
        # (PROXY_VARIANTS) under the session id ``<sid>-<variant>``: a session no host prompt ever
        # started, so no hook began its turn. They are reported apart, never counted as host traffic.
        probe = [r for r in rows_by_path["proxy"] if str(r.get("session_id") or "").endswith(_PROBE_SUFFIXES)]
        rows_by_path["proxy"] = [r for r in rows_by_path["proxy"] if r not in probe]
        if probe:
            rows_by_path["proxy_probe_envelopes"] = probe
    paths = {}
    for path, rows in sorted(rows_by_path.items()):
        share = kpi._identity_share([{"session_id": r.get("session_id"),
                                      "task_id": kpi._prd_task(r.get("task_id"))} for r in rows])
        share["verdict"] = ("not scored (replay probe)" if path == "proxy_probe_envelopes" else
                            "n<200" if share["n"] < 200 else
                            "PASS" if share["share"] >= kpi.G3_PRD_BAR else "FAIL")
        paths[path] = share
    life = summary["timing"]["lifecycle"]
    hooks = {"P0.9-a session-start": (life.get("session-start:SessionStart"), 2000, 50),
             "P0.9-b status-bar": (life.get("status-bar:UserPromptSubmit"), 300, 200)}
    latency = {}
    for name, (row, bar, n_min) in hooks.items():
        if row:
            latency[name] = {"n": row["n"], "p50_ms": row["p50_ms"], "p95_ms": row["p95_ms"], "bar_ms": bar,
                             "verdict": "n<min" if row["n"] < n_min else "PASS" if row["p95_ms"] <= bar else "FAIL"}
    scored = [p for name, p in paths.items() if name != "proxy_probe_envelopes"]
    return {"label": LABEL, "must": ["P1.10-a", "P0.9-a", "P0.9-b"], "paths": paths, "latency": latency,
            "verdict_p110a": ("PASS" if scored and all(p["verdict"] == "PASS" for p in scored) else
                              "FAIL" if any(p["verdict"] == "FAIL" for p in scored) else "NOT INFORMATIVE"),
            "replay": {"errors": summary["deterministic"]["errors"],
                       "network_violations": summary["deterministic"]["network"]["violations"],
                       "records": summary["deterministic"]["corpus"], "git_sha": summary["run"]["git_sha"],
                       "wall_s": summary["timing"]["wall_s"]}}


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
    o = sub.add_parser("o3")
    o.add_argument("--out", type=Path, default=ROOT / "docs" / "measurements")
    c = sub.add_parser("replay-corpus", help="write the P1.10-a / P0.9 corpus for scripts/synthetic_replay.py")
    c.add_argument("--corpus", type=Path, required=True)
    c.add_argument("--sessions", type=int, default=60)
    i = sub.add_parser("identity", help="P1.10-a and P0.9-a/b from a kept synthetic_replay run")
    i.add_argument("--work-dir", type=Path, required=True, help="the replay's kept scratch root")
    i.add_argument("--summary", type=Path, required=True, help="the replay's summary.json")
    i.add_argument("--out", type=Path, default=ROOT / "docs" / "measurements")
    args = ap.parse_args(argv)

    if args.cmd == "replay-corpus":
        recs = replay_corpus(args.sessions)
        args.corpus.parent.mkdir(parents=True, exist_ok=True)
        args.corpus.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        print(f"wrote {len(recs)} records to {args.corpus}")
        return 0

    if args.cmd == "fidelity":
        if args.stratum == "sonnet":
            if loopback_base_url():
                ap.error("ANTHROPIC_BASE_URL points at this machine (the live router?): the synthetic "
                         "calls would land in the live ledger. Unset it for this run.")
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
    elif args.cmd == "o3":
        res, name = run_o3(), "p19a-synthetic.json"
    elif args.cmd == "identity":
        summary = json.loads(args.summary.read_text(encoding="utf-8"))
        res, name = identity_from_replay(args.work_dir / "state", summary), "p110a-p09-synthetic.json"
    else:
        res = run_summary()
        name = "p12a-synthetic.json"
    path = _write(args.out, name, res)
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=2, sort_keys=True))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
