#!/usr/bin/env python3
"""Per-step-class quality benchmark for the per-call proxy (llm_router.proxy).

Replays golden fixture tasks through the REAL proxy app, with a recorded
Anthropic upstream and the real local serving model, and reports per arm:
tasks passing, calls (extra vs baseline), validation failures and other
fallbacks, per step class and per previous tool.

WHY REPLAY AND NOT LIVE CLAUDE CODE
-----------------------------------
An isolated Claude Code session (empty HOME) cannot authenticate without
reaching the user's keychain, which this benchmark must not do. So:

* the agent loop is this script: it sends Claude Code's own recorded request
  (system prompt, 24 tools, thinking/context_management fields) to the proxy,
  executes the returned tool calls in a scratch copy of the fixture repo with
  Claude Code's recorded tool-result formats, and loops;
* "Anthropic" is an oracle upstream that answers with the reply Claude actually
  gave at the same step of the same task in the spike recordings (keyed on the
  task and the tool calls + results of the newest turn; if that exact key is
  missing it falls back to the same tool calls alone). A step Claude never saw
  is an ORACLE MISS: the task stops and is reported as unscored, never as a
  pass or a fail;
* the serving model is real (Ollama through the proxy's own backend), so
  validation failures, latency and routed tool calls are measured, not mocked.

What this therefore measures: whether routed steps keep golden tasks passing,
how many extra calls they add, and how often served replies fail validation or
the latency budget. What it does NOT measure: Claude's own behaviour after a
routed step it never saw (oracle misses), Claude Code's real permission and
tool-execution layer, or the extra cache writes a mixed history costs on real
Anthropic (the oracle replays recorded usage; the spike measured about +600
cache-creation tokens per call after a routed turn).

Arms: ``baseline`` (pass-through), ``routed`` (local serving of continuation
steps) and ``tiered`` (no local serving; the Claude-tier rewrite on, with
``--tier-policy`` or the bundled policy). The oracle answers whatever model a
request names, so ``tiered`` measures the decisions (tier mix, switches,
estimated cost), not how a cheaper tier would have answered.

Recordings: produced by the spike (branch ``spike/per-call-proxy``,
``scripts/spikes/per_call_proxy.py --dump-dir``). They contain request bodies
with user identity fields and stay out of the repository. Expected layout::

    DIR/dumps/*.json  DIR/dumps-routed/*.json  DIR/logs/{baseline,routed}.jsonl

Run (state isolated from the real ~/.llm-router)::

    LLM_ROUTER_HOME=$SCRATCH/home uv run python scripts/bench_proxy_steps.py \\
        --recordings $SPIKE --golden ~/Projects/rsi-engine-probe/holdouts-src/routing-golden-v1 \\
        --out $SCRATCH/bench
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Benchmark traffic declares itself (M-02): routing_quality.detect_synthetic
# reads this, so nothing this run records is counted as production.
os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

from llm_router.proxy import ledger  # noqa: E402
from llm_router.proxy import server as ps  # noqa: E402
from llm_router.proxy.backends import OllamaBackend  # noqa: E402
from llm_router.proxy.steps import non_system  # noqa: E402

RUN_DIR_RE = re.compile(r"/[^\s\"']*?/runs/(?:base|routed)-(c\d{3})")
EDIT_OK = ("The file {p} has been updated successfully. "
           "(file state is current in your context — no need to Read it back)")
ALLOWED_BASH = ("python3 tests/*", "python tests/*")  # the spike's --allowedTools


# ── recordings -> oracle ─────────────────────────────────────────────────────


def _norm(text: str, workdir: str | None = None) -> str:
    text = RUN_DIR_RE.sub("{WD}", text)
    return text.replace(workdir, "{WD}") if workdir else text


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text", "") for x in c or [] if isinstance(x, dict))


def step_key(body: dict, workdir: str | None = None) -> tuple[str, str]:
    """(action_key, exact_key) of the newest turn: the tool calls that produced
    the newest tool results, with and without those results."""
    msgs = non_system(body.get("messages") or [])
    if len(msgs) < 3:
        return "FIRST", "FIRST"
    calls = [(b["name"], json.dumps(b.get("input"), sort_keys=True))
             for b in msgs[-2].get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
    results = [(b.get("is_error") or False, _result_text(b))
               for b in msgs[-1].get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_result"]
    action = _norm(json.dumps(calls), workdir)
    return action, action + "|" + _norm(json.dumps(results), workdir)


def _case_of(ts: float, windows: list[tuple[str, str, float, float]], tag: str) -> str | None:
    for case, t, start, end in windows:
        if t == tag and start - 1 <= ts <= end + 1:
            return case
    return None


def load_oracle(rec_dir: Path) -> tuple[dict, dict]:
    """(oracle, first_requests). oracle[case][key] = {"content", "usage", "stop_reason"}."""
    starts: dict[tuple[str, str], float] = {}
    windows = []
    for line in (rec_dir / "logs" / "cases.jsonl").read_text().splitlines():
        r = json.loads(line)
        if "start" in r:
            starts[(r["case"], r["tag"])] = r["start"]
        elif (r["case"], r["tag"]) in starts:
            windows.append((r["case"], r["tag"], starts[(r["case"], r["tag"])], r["end"]))

    oracle: dict[str, dict] = {}
    first: dict[str, dict] = {}
    for tag, log in (("base", "baseline.jsonl"), ("routed", "routed.jsonl")):
        calls: dict[str, list[dict]] = {}
        for line in (rec_dir / "logs" / log).read_text().splitlines():
            r = json.loads(line)
            if r.get("path") != "/v1/messages" or not r.get("dump"):
                continue
            case = _case_of(r["ts"], windows, tag)
            dump = rec_dir / Path(r["dump"]).parent.name / Path(r["dump"]).name
            if case and dump.exists():
                calls.setdefault(case, []).append(dict(r, body=json.loads(dump.read_text())))
        for case, seq in calls.items():
            seq.sort(key=lambda r: r["ts"])
            if tag == "base":
                first[case] = seq[0]["body"]
            for i, r in enumerate(seq):
                if (r.get("route") or {}).get("outcome") == "served":
                    continue  # a local reply, not Claude's
                if i + 1 < len(seq):
                    n_prev = len(r["body"]["messages"])
                    new = [m for m in seq[i + 1]["body"]["messages"][n_prev:] if m["role"] == "assistant"]
                    if not new:
                        continue
                    content = new[0]["content"]
                    content = content if isinstance(content, list) else [{"type": "text", "text": content}]
                    stop = "tool_use" if any(b.get("type") == "tool_use" for b in content) else "end_turn"
                else:
                    content, stop = [{"type": "text", "text": "Done. (final answer not recorded)"}], "end_turn"
                entry = {"content": content, "usage": r.get("usage") or {}, "stop_reason": stop, "tag": tag}
                action, exact = step_key(r["body"])
                slot = oracle.setdefault(case, {})
                slot.setdefault(exact, entry)
                slot.setdefault("A:" + action, entry)
                slot.setdefault("ALL:" + exact, []).append(entry)
    return oracle, first


class Oracle:
    """The mocked Anthropic upstream: recorded Claude replies, as SSE."""

    def __init__(self, table: dict, case: str, workdir: str):
        self.table, self.case, self.workdir = table.get(case, {}), case, workdir
        self.calls = 0
        self.misses = 0
        self.match_kinds: list[str] = []
        self.usage: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/messages":
            return httpx.Response(200, json={})
        self.calls += 1
        body = json.loads(request.content)
        action, exact = step_key(body, self.workdir)
        entry = self.table.get(exact)
        kind = "exact"
        if entry is None:
            entry, kind = self.table.get("A:" + action), "action"
        if entry is None:
            self.misses += 1
            self.match_kinds.append("miss")
            miss = json.dumps({"type": "error", "error": {"type": "oracle_miss", "message": action}}).encode()
            return httpx.Response(599, stream=httpx.ByteStream(miss), headers={"content-type": "application/json"})
        self.match_kinds.append(kind)
        self.usage.append(entry["usage"])
        content = json.loads(RUN_DIR_RE.sub(self.workdir, json.dumps(entry["content"])))
        msg = {"id": f"msg_oracle{self.calls:04d}", "type": "message", "role": "assistant",
               "model": body.get("model"), "content": content, "stop_reason": entry["stop_reason"],
               "stop_sequence": None, "usage": dict(entry["usage"])}
        return httpx.Response(200, stream=httpx.ByteStream(_sse(msg)), headers={"content-type": "text/event-stream"})


def _sse(m: dict) -> bytes:
    from llm_router.proxy.translate import sse_from_message

    safe = dict(m, content=[b for b in m["content"] if b.get("type") in ("text", "tool_use")])
    return sse_from_message(safe)


def tool_names(content: list) -> list[str]:
    return [b.get("name") for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_use"]


def claude_self_agreement(oracle: dict) -> dict:
    """The noise floor: at steps where Claude answered the SAME request twice
    (base and routed recordings), how often it chose the same tools."""
    agree = total = 0
    for slot in oracle.values():
        for key, entries in slot.items():
            if not key.startswith("ALL:") or len(entries) < 2:
                continue
            total += 1
            agree += tool_names(entries[0]["content"]) == tool_names(entries[1]["content"])
    return {"agree": agree, "n": total}


def message_from_sse(raw: bytes) -> dict:
    msg, blocks, partial = {}, {}, {}
    for chunk in raw.decode("utf-8", "replace").split("\n\n"):
        data = [line[6:] for line in chunk.splitlines() if line.startswith("data: ")]
        if not data:
            continue
        d = json.loads(data[0])
        t = d.get("type")
        if t == "message_start":
            msg = d["message"]
        elif t == "content_block_start":
            blocks[d["index"]] = dict(d["content_block"])
        elif t == "content_block_delta":
            delta = d["delta"]
            if delta.get("type") == "text_delta":
                blocks[d["index"]]["text"] = blocks[d["index"]].get("text", "") + delta["text"]
            elif delta.get("type") == "input_json_delta":
                partial[d["index"]] = partial.get(d["index"], "") + delta["partial_json"]
        elif t == "content_block_stop" and d["index"] in partial:
            blocks[d["index"]]["input"] = json.loads(partial[d["index"]] or "{}")
        elif t == "message_delta":
            msg["stop_reason"] = d["delta"].get("stop_reason")
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    return msg


# ── tool execution, in Claude Code's recorded result formats ────────────────


def run_tool(name: str, inp: dict, wd: Path) -> tuple[str, bool]:
    def path_of(p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else wd / q

    try:
        if name == "Read":
            p = path_of(inp["file_path"])
            if not p.exists():
                return "File does not exist.", True
            lines = p.read_text().split("\n")
            off = int(inp.get("offset") or 1)
            lim = int(inp.get("limit") or 2000)
            sel = lines[off - 1: off - 1 + lim]
            return "\n".join(f"{i}\t{line}" for i, line in enumerate(sel, start=off)), False
        if name == "Edit":
            p = path_of(inp["file_path"])
            if not p.exists():
                return "<tool_use_error>File does not exist.</tool_use_error>", True
            text, old, new = p.read_text(), inp["old_string"], inp["new_string"]
            n = text.count(old)
            if old == new:
                return "<tool_use_error>No changes to make: old_string and new_string are exactly the same.</tool_use_error>", True
            if n == 0:
                return f"<tool_use_error>String to replace not found in file.\nString: {old}</tool_use_error>", True
            if n > 1 and not inp.get("replace_all"):
                return (f"<tool_use_error>Found {n} matches of the string to replace, but replace_all is false."
                        "</tool_use_error>"), True
            p.write_text(text.replace(old, new) if inp.get("replace_all") else text.replace(old, new, 1))
            return EDIT_OK.format(p=p), False
        if name == "Write":
            p = path_of(inp["file_path"])
            existed = p.exists()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(inp["content"])
            return (EDIT_OK.format(p=p) if existed else f"File created successfully at: {p}"), False
        if name == "Bash":
            cmd = inp["command"].strip()
            if not any(fnmatch.fnmatch(cmd, pat) for pat in ALLOWED_BASH):
                return "This command requires approval", True
            r = subprocess.run(["/bin/zsh", "-c", cmd], cwd=wd, capture_output=True, text=True, timeout=60)
            out = (r.stdout + r.stderr).strip()
            return (out, False) if r.returncode == 0 else (f"Exit code {r.returncode}\n{out}", True)
        if name == "Glob":
            base = path_of(inp.get("path") or str(wd))
            hits = sorted(str(p) for p in base.glob(inp["pattern"]))
            return ("\n".join(hits) or "No files found"), False
        if name == "Grep":
            base = path_of(inp.get("path") or str(wd))
            rx = re.compile(inp["pattern"])
            hits = sorted({str(p) for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                           and rx.search(p.read_text(errors="replace"))})
            return (f"Found {len(hits)} files\n" + "\n".join(hits)) if hits else "No files found", False
        if name == "ReportFindings":
            return "No findings reported.", False
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return f"<tool_use_error>{type(exc).__name__}: {exc}</tool_use_error>", True
    return f"<tool_use_error>No such tool available: {name}</tool_use_error>", True


# ── one task ─────────────────────────────────────────────────────────────────


def _rewrite(obj, workdir: str):
    return json.loads(RUN_DIR_RE.sub(workdir, json.dumps(obj)))


async def run_task(case: dict, arm: str, args, oracle_table: dict, first: dict, rows_path: Path) -> dict:
    cid = case["id"]
    wd = Path(args.out) / "runs" / f"{arm}-{cid}"
    shutil.rmtree(wd, ignore_errors=True)
    shutil.copytree(Path(args.golden) / "fixture_repo", wd)
    subprocess.run(["find", str(wd), "-name", "__pycache__", "-prune", "-exec", "rm", "-rf", "{}", ";"], check=False)
    workdir = str(wd)
    body = _rewrite(first[cid], workdir)
    body["stream"] = True
    if args.requested_model:
        body["model"] = args.requested_model

    oracle = Oracle(oracle_table, cid, workdir)
    local = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    # "tiered": no local serving, Claude-tier rewrite on (proxy.tiers).
    steps = frozenset() if arm in ("baseline", "tiered") else frozenset({"continuation"})
    cfg = ps.ProxyConfig(steps=steps, step_budget_s=args.step_budget_s, model=args.model, trim=args.trim,
                         num_ctx=args.num_ctx, upstream="http://127.0.0.1:9", ledger_path=rows_path,
                         tiers=arm == "tiered", tier_policy=args.tier_policy)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(oracle)),
                       backend_factory=lambda m: OllamaBackend(m, local, base_url=args.ollama_url,
                                                               num_ctx=args.num_ctx, hedge_s=args.hedge_s))
    calls, status, t0 = 0, "max_calls", time.time()
    agreement: list = []
    before = len(ledger.read_rows(rows_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787",
                                 timeout=httpx.Timeout(900.0)) as client:
        while calls < args.max_calls:
            calls += 1
            r = await client.post("/v1/messages?beta=true", content=json.dumps(body),
                                  headers={"authorization": "Bearer bench-no-credential",
                                           "anthropic-version": "2023-06-01", "content-type": "application/json"})
            if r.status_code == 599:
                status = "oracle_miss"
                break
            if r.status_code != 200:
                status = f"http_{r.status_code}"
                break
            msg = message_from_sse(r.content)
            if str(msg.get("id", "")).startswith("msg_lr"):  # served by the local model
                _action, exact = step_key(body, workdir)
                ref = oracle_table.get(cid, {}).get(exact)
                agreement.append(None if ref is None else tool_names(ref["content"]) == tool_names(msg["content"]))
            body["messages"].append({"role": "assistant", "content": msg["content"]})
            uses = [b for b in msg["content"] if b.get("type") == "tool_use"]
            if msg.get("stop_reason") != "tool_use" or not uses:
                status = "finished"
                break
            results = []
            for u in uses:
                text, err = run_tool(u["name"], u.get("input") or {}, wd)
                block = {"tool_use_id": u["id"], "type": "tool_result", "content": text}
                if err:
                    block["is_error"] = True
                results.append(block)
            body["messages"].append({"role": "user", "content": results})
    await local.aclose()

    test = subprocess.run([sys.executable, case["grader"]["test_file"]], cwd=wd, capture_output=True, text=True)
    target = f"pkg/{case['fixture_module']}.py"
    diff = subprocess.run(["diff", "-rq", "-x", "__pycache__", str(Path(args.golden) / "fixture_repo"), str(wd)],
                          capture_output=True, text=True).stdout
    changed = sorted(set(re.findall(r"Files .*?fixture_repo/(\S+) and", diff))
                     | {m for m in re.findall(r"Only in (\S+)", diff)})
    only_target = changed == [target]
    rows = ledger.read_rows(rows_path)[before:]
    return {"case": cid, "arm": arm, "status": status, "calls": calls,
            "anthropic_calls": oracle.calls, "oracle_misses": oracle.misses, "oracle_match": oracle.match_kinds,
            "test_rc": test.returncode, "changed": changed,
            "passed": status != "oracle_miss" and test.returncode == 0 and only_target,
            "scored": status != "oracle_miss", "wall_s": round(time.time() - t0, 1),
            "anthropic_usage": oracle.usage, "rows": rows, "agreement": agreement}


# ── report ───────────────────────────────────────────────────────────────────


def summarize(results: list[dict]) -> dict:
    arms: dict[str, dict] = {}
    for arm in sorted({r["arm"] for r in results}):
        rs = [r for r in results if r["arm"] == arm]
        rows = [row for r in rs for row in r["rows"]]
        cont = [row for row in rows if row.get("step_class") == "continuation"]
        by_prev: dict[str, dict] = {}
        for r in rs:
            for row in r["rows"]:
                if row.get("step_class") != "continuation":
                    continue
                key = "+".join(row.get("prev_tools") or []) or "?"
                d = by_prev.setdefault(key, {"considered": 0, "served": 0, "fallback": 0, "policy_kept": 0})
                d["considered"] += 1
                if row.get("decision") == "served":
                    d["served"] += 1
                elif row.get("decision") == "fallback":
                    d["fallback"] += 1
                elif row.get("reason") == "policy_kept":
                    d["policy_kept"] += 1
        fallbacks: dict[str, int] = {}
        for row in rows:
            if row.get("decision") == "fallback":
                fallbacks[row.get("reason")] = fallbacks.get(row.get("reason"), 0) + 1
        served_lat = [row["route_latency_s"] for row in rows if row.get("decision") == "served"]
        agree = [a for r in rs for a in r.get("agreement", []) if a is not None]
        arms[arm] = {
            "tasks": len(rs), "scored": sum(r["scored"] for r in rs), "passed": sum(r["passed"] for r in rs),
            "calls": sum(r["calls"] for r in rs), "anthropic_calls": sum(r["anthropic_calls"] for r in rs),
            "oracle_misses": sum(r["oracle_misses"] for r in rs),
            "continuation_considered": len(cont),
            "served": sum(1 for row in rows if row.get("decision") == "served"),
            "validation_failures": fallbacks.get("validation", 0),
            "fallbacks_by_reason": fallbacks,
            "served_latency_median_s": round(statistics.median(served_lat), 1) if served_lat else None,
            "served_latency_max_s": max(served_lat) if served_lat else None,
            "served_latency_p90_s": (round(statistics.quantiles(served_lat, n=10)[8], 1)
                                     if len(served_lat) >= 2 else None),
            "tool_choice_agreement_with_claude": {"agree": sum(agree), "n": len(agree),
                                                  "unscored_served": sum(1 for r in rs for a in r.get("agreement", [])
                                                                         if a is None)},
            "added_latency_total_s": round(sum(row.get("added_latency_s") or 0 for row in rows), 1),
            "wall_s": round(sum(r["wall_s"] for r in rs), 1),
            "by_prev_tool": by_prev,
            "tiers": ledger.tier_stats(rows),
            "anthropic_cost_usd_recorded_usage": round(sum(
                ledger.anthropic_cost({"requested_model": "claude-sonnet-5", "usage": u}) or 0
                for r in rs for u in r["anthropic_usage"]), 4),
        }
    base = arms.get("baseline")
    for name, a in arms.items():
        if base and name != "baseline":
            a["extra_calls_vs_baseline"] = a["calls"] - base["calls"]
    return arms


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--recordings", required=True)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cases", default="", help="comma list; default: every case with a recording")
    ap.add_argument("--arms", default="baseline,routed")
    ap.add_argument("--step-budget-s", type=float, default=ps.DEFAULT_STEP_BUDGET_S)
    ap.add_argument("--model", default=None)
    ap.add_argument("--trim", default=None)
    ap.add_argument("--num-ctx", type=int, default=ps.DEFAULT_NUM_CTX)
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    ap.add_argument("--max-calls", type=int, default=14)
    ap.add_argument("--hedge-s", type=float, default=ps.DEFAULT_HEDGE_S)
    ap.add_argument("--no-warm-up", action="store_true")
    ap.add_argument("--tier-policy", default=None, help="tier policy YAML for the `tiered` arm")
    ap.add_argument("--requested-model", default=None,
                    help="replace the recorded request's model (e.g. to replay an Opus session)")
    args = ap.parse_args()

    oracle, first = load_oracle(Path(args.recordings))
    cases = [json.loads(line) for line in (Path(args.golden) / "constructed_cases.jsonl").read_text().splitlines()]
    wanted = set(args.cases.split(",")) if args.cases else set(first)
    cases = [c for c in cases if c["id"] in wanted and c["id"] in first]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    warm = None
    if "routed" in args.arms and not args.no_warm_up:
        async def _warm():
            async with httpx.AsyncClient() as c:
                model = args.model or "ollama/qwen3-coder:30b"
                return {"model": model, "seconds": await OllamaBackend(
                    model, c, base_url=args.ollama_url, num_ctx=args.num_ctx).warm_up()}
        warm = asyncio.run(_warm())
        print(f"warm-up (cold start): {warm}", flush=True)
    results = []
    for arm in args.arms.split(","):
        rows_path = out / f"proxy_calls-{arm}.jsonl"
        rows_path.unlink(missing_ok=True)
        for case in cases:
            r = asyncio.run(run_task(case, arm, args, oracle, first, rows_path))
            print(f"{arm:9s} {r['case']} {r['status']:11s} calls={r['calls']:2d} anthropic={r['anthropic_calls']} "
                  f"passed={r['passed']} changed={r['changed']} oracle={r['oracle_match']} wall={r['wall_s']}s",
                  flush=True)
            results.append(r)
    summary = summarize(results)
    conditions = {"cases": [c["id"] for c in cases], "step_budget_s": args.step_budget_s,
                  "model_pin": args.model, "trim": args.trim or "default", "num_ctx": args.num_ctx,
                  "max_calls": args.max_calls, "upstream": "recorded-oracle (spike 2026-09-28)",
                  "hedge_s": args.hedge_s, "ollama_url": args.ollama_url, "warm_up": warm,
                  "tier_policy": args.tier_policy or ("bundled" if "tiered" in args.arms else None),
                  "requested_model": args.requested_model,
                  "claude_self_agreement_floor": claude_self_agreement(oracle)}
    (out / "report.json").write_text(json.dumps({"conditions": conditions, "summary": summary,
                                                 "results": results}, indent=1, default=str))
    print(json.dumps({"conditions": conditions, "summary": summary}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
