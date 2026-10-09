#!/usr/bin/env python3
"""Micro-benchmark of the proxy's tier decision phases (PLAN v16 P0.9-e).

The decision is ``classify + quota_read + stickiness + haiku_checks``
(``proxy.tiers.DECISION_PHASES``); the bar is p95 <= 50 ms on turn-first calls.
This script sends a FIXED corpus of synthetic, Claude Code shaped turn-first
requests through the real proxy app (``proxy.server.build_app``, in process,
upstream mocked) and reads each phase back from the ledger row the proxy wrote,
so it times exactly what production records in ``tier_phases_ms``.

The corpus is deterministic (seeded): conversations of ~170 KB to ~2.8 MB (live
turn-first ``req_bytes`` span 65 KB-2.8 MB), with a system prompt, 30-160 tool
schemas, tool_use/tool_result history, mid-conversation ``role: system`` messages
and a newest human prompt that carries a ``<system-reminder>`` block. The mix of
cases follows the live turn-first rows (``CASES``). Every text is synthetic;
nothing is read from a real ledger or transcript.

The policy mirrors the live proxy's: ``--tiers conversation``, ``haiku_rewrite``
and ``haiku_fold_system`` on, Opus in ``pinned_models``.

    python scripts/bench_proxy_decision.py --runs 200            # warm: one process
    python scripts/bench_proxy_decision.py --runs 200 --cold     # one fresh process per run
    python scripts/bench_proxy_decision.py --json out.json

State (ledger, usage.json, failopen records) lives in a temporary
``LLM_ROUTER_HOME``; the real ``~/.llm-router`` is never read or written.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PHASES = ("classify", "quota_read", "stickiness", "haiku_checks")
SIZES = (65_000, 120_000, 300_000, 480_000, 560_000, 620_000, 680_000, 900_000, 1_900_000, 2_800_000)
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"

_SENTENCES = (
    "Look at how the proxy decides which tier serves a turn and why it sometimes takes longer than it should.",
    "Read the ledger rows for the last day and check whether the phase timings add up.",
    "Then change the code so the slow path no longer runs on every request, and keep the behaviour identical.",
    "Add a regression test that fails on the old code and passes on the new one.",
    "Do not touch the hooks or the configuration files; the change belongs in the proxy package only.",
    "When you are done, run the whole suite and tell me what broke, if anything.",
    "Explain the trade-off you chose in two or three sentences so the reviewer can follow it.",
    "If the numbers contradict what we expected, say so first and show the evidence.",
)


def _prompt(n_chars: int) -> str:
    out, i = [], 0
    while sum(len(x) + 1 for x in out) < n_chars:
        out.append(_SENTENCES[i % len(_SENTENCES)])
        i += 1
    return " ".join(out)[:n_chars]


# The corpus mirrors the live turn-first mix (2026-10-08T19:58Z..10-09, n=133:
# 38 config_pinned Opus, 36 long first prompts, 53 moderate, 5 complex, 1 simple)
# as a 20-case cycle: (kind, requested model, prompt length in chars, first call).
CASES = (
    *[("pinned", OPUS, 900, False)] * 6,
    *[("first_long", SONNET, 1_500, True)] * 5,
    *[("moderate", SONNET, n, False) for n in (450, 700, 1_000, 1_300, 1_600, 1_900, 1_200)],
    ("complex", SONNET, 3_200, False),
    ("simple", SONNET, 120, False),
)

_WORDS = ("alpha beta gamma delta epsilon proxy router tier cache ledger phase classify quota sticky "
          "haiku sonnet opus request response stream token budget session hook latency decision "
          "module function class import return value error test fixture parse encode decode "
          "שלום עולם naïve café — → ✓ ✗ ▸").split()


def _text(rng: random.Random, n: int) -> str:
    out, size = [], 0
    while size < n:
        w = rng.choice(_WORDS)
        out.append(w)
        size += len(w) + 1
    return " ".join(out)


def _tools(rng: random.Random, n: int) -> list[dict]:
    tools = []
    for i in range(n):
        props = {f"arg{j}": {"type": "string", "description": _text(rng, 120)} for j in range(6)}
        # Tool 0 is the sub-agent launcher, which makes a first call a main-session
        # turn_first (``steps.step_kind``), as in Claude Code.
        tools.append({"name": "Agent" if i == 0 else f"Tool{i}", "description": _text(rng, 1_400),
                      "input_schema": {"type": "object", "properties": props, "required": ["arg0"],
                                       "additionalProperties": False}})
    return tools


def build_body(target_bytes: int, prompt: str, seed: int, *, model: str = SONNET,
               first_call: bool = False) -> dict:
    """One synthetic turn-first request of about ``target_bytes`` serialized (at
    least its system prompt, tools and first message). A first call has one user
    message; any other call carries a tool_use/tool_result history before the new
    human prompt."""
    rng = random.Random(seed)
    reminder = "<system-reminder>\n" + _text(rng, 3_000) + "\n</system-reminder>"
    body = {
        "model": model,
        "max_tokens": 32_000,
        "stream": True,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "metadata": {"user_id": json.dumps({"session_id": f"bench-{seed:08d}", "device_id": "x"})},
        "system": [{"type": "text", "text": _text(rng, 9_000)} for _ in range(3)],
        # 30 to 160 tools: a session with many MCP servers sends a few hundred KB of
        # schemas on every call (live first calls are ~475 KB with one message).
        "tools": _tools(rng, 30 + (seed % 3) * 65),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>\n" + _text(rng, 30_000) + "\n</system-reminder>"},
            {"type": "text", "text": _text(rng, 200)}]}],
    }
    size = len(json.dumps(body))
    if first_call:
        body["messages"][0]["content"].append({"type": "text", "text": prompt})
        return body
    turn = 0
    while size < target_bytes - 4_000:
        turn += 1
        tid = f"toolu_{seed:04d}{turn:05d}"
        blocks = [{"type": "text", "text": _text(rng, 300)},
                  {"type": "tool_use", "id": tid, "name": f"Tool{turn % 29 + 1}",
                   "input": {"arg0": _text(rng, 160), "arg1": _text(rng, 40)}}]
        result = _text(rng, rng.choice((800, 2_000, 4_000, 8_000)))
        inner = result if turn % 3 else [{"type": "text", "text": result}]
        msgs = [{"role": "assistant", "content": blocks},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": inner}]}]
        if turn % 4 == 0:
            msgs.append({"role": "system", "content": _text(rng, 600)})
        if turn % 9 == 0:  # an earlier human turn, then the assistant's reply to it
            msgs.append({"role": "user", "content": [{"type": "text", "text": _text(rng, 300)}]})
        body["messages"].extend(msgs)
        size += len(json.dumps(msgs)) + 2
    body["messages"].append({"role": "assistant", "content": [{"type": "text", "text": _text(rng, 400)}]})
    body["messages"].append({"role": "system", "content": _text(rng, 500)})
    body["messages"].append({"role": "user", "content": [{"type": "text", "text": reminder},
                                                         {"type": "text", "text": prompt}]})
    return body


def corpus(n: int) -> list[tuple[int, int, tuple]]:
    """``n`` (seed, size, case) triples: the case cycle (``CASES``) crossed with the
    size cycle (``SIZES``), so every case meets every size within 100 runs."""
    return [(i, SIZES[(i + i // len(CASES)) % len(SIZES)], CASES[i % len(CASES)]) for i in range(n)]


def request(seed: int, size: int, case: tuple) -> bytes:
    _, model, n_chars, first = case
    return json.dumps(build_body(size, _prompt(n_chars), seed, model=model, first_call=first)).encode()


def _policy_yaml(home: Path) -> Path:
    import yaml

    from llm_router.proxy import tiers as pt

    raw = yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())
    raw.update(haiku_rewrite=True, haiku_fold_system=True, pinned_models=[OPUS])
    path = home / "claude_tiers.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _setup_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "usage.json").write_text(json.dumps(
        {"session_pct": 30, "weekly_pct": 40, "updated_at": int(time.time())}))


async def _run(indices: list[tuple[int, int, tuple]], home: Path) -> list[dict]:
    import httpx

    from llm_router.proxy import server as ps

    def upstream(request):
        sse = (b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m\",\"model\":\""
               + SONNET.encode() + b"\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n"
               b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
        return httpx.Response(200, stream=httpx.ByteStream(sse), headers={"content-type": "text/event-stream"})

    ledger_path = home / "proxy_calls.jsonl"
    cfg = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=ps.TIERS_CONVERSATION,
                         tier_policy=str(_policy_yaml(home)), ledger_path=ledger_path)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    headers = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    payloads = [request(*x) for x in indices]
    out = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        for raw in payloads:
            before = ledger_path.stat().st_size if ledger_path.exists() else 0
            r = await c.post("/v1/messages?beta=true", content=raw, headers=headers)
            assert r.status_code == 200, r.status_code
            with open(ledger_path, encoding="utf-8") as fh:
                fh.seek(before)
                row = [json.loads(line) for line in fh if line.strip()][-1]
            out.append({"step_class": row.get("step_class"), "req_bytes": row.get("req_bytes"),
                        "reason": row.get("tier_reason"), "proposed": row.get("tier_proposed"),
                        "phases": row.get("tier_phases_ms") or {}})
    return out


def _pct(vals: list[float], q: float) -> float:
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))]


def summarize(rows: list[dict]) -> dict:
    rows = [r for r in rows if r["step_class"] == "turn_first"]
    out = {}
    for name in (*PHASES, "decision"):
        if name == "decision":
            vals = [sum(r["phases"].get(k, 0.0) for k in PHASES) for r in rows]
        else:
            vals = [r["phases"][name] for r in rows if name in r["phases"]]
        out[name] = ({"n": len(vals), "p50": round(_pct(vals, 0.5), 3), "p95": round(_pct(vals, 0.95), 3),
                      "max": round(max(vals), 3)} if vals else {"n": 0})
    return out


def render(title: str, s: dict) -> str:
    lines = [title, f"  {'phase':<13}{'n':>6}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}"]
    for k, v in s.items():
        if k == "cases":
            lines.append(f"  cases: {v}")
        elif v.get("n"):
            lines.append(f"  {k:<13}{v['n']:>6}{v['p50']:>10.3f}{v['p95']:>10.3f}{v['max']:>10.3f}")
        else:
            lines.append(f"  {k:<13}{0:>6}")
    return "\n".join(lines)


def _child(index: int) -> None:
    import asyncio

    home = Path(os.environ["LLM_ROUTER_HOME"])
    rows = asyncio.run(_run([corpus(index + 1)[index]], home))
    print(json.dumps(rows[0]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--cold", action="store_true", help="one fresh interpreter per run (first decision)")
    ap.add_argument("--json", help="write the summary here")
    ap.add_argument("--child", type=int, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.child is not None:
        _child(a.child)
        return 0
    with tempfile.TemporaryDirectory(prefix="bench-p09e-") as tmp:
        home = Path(tmp) / "home"
        _setup_home(home)
        env = dict(os.environ, LLM_ROUTER_HOME=str(home), HOME=str(Path(tmp)))
        if a.cold:
            rows = []
            for i in range(a.runs):
                proc = subprocess.run([sys.executable, __file__, "--child", str(i)], env=env,
                                      capture_output=True, text=True, timeout=120, check=False)
                if proc.returncode != 0:
                    print(proc.stderr[-2000:], file=sys.stderr)
                    return 1
                rows.append(json.loads(proc.stdout.strip().splitlines()[-1]))
        else:
            os.environ.update(LLM_ROUTER_HOME=str(home), HOME=str(Path(tmp)))
            import asyncio

            idx = corpus(a.runs)
            rows = asyncio.run(_run(idx, home))
    s = summarize(rows)
    s["cases"] = dict(sorted(collections.Counter(f"{r['reason']}/{r['proposed']}" for r in rows).items()))
    load = os.getloadavg()[0]
    title = (f"proxy decision phases, {'cold (fresh process per run)' if a.cold else 'warm (one process)'}, "
             f"runs={a.runs}, python {sys.version.split()[0]}, load1={load:.2f}")
    print(render(title, s))
    if a.json:
        Path(a.json).write_text(json.dumps({"mode": "cold" if a.cold else "warm", "runs": a.runs,
                                            "python": sys.version.split()[0], "load1": load,
                                            "summary": s}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
