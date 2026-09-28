"""SPIKE (2026-09-28): which levers make a local model fast enough for
Claude Code "continuation" steps? Measurement only -- nothing deployed.

Companion to docs/spikes/local-speed-2026-09-28.md and the parent spike
docs/spikes/per-call-proxy-2026-09-28.md.

Why replay instead of live isolated Claude Code sessions
----------------------------------------------------------
Isolated Claude Code (empty HOME, no ~/.claude.json) cannot authenticate
without symlinking ~/Library/Keychains, which is out of bounds for this spike
(the per-call-proxy spike did that and it was reverted). Confirmed by hand:
`HOME=<empty> claude -p "..."` -> "Not logged in". Per the task's own
fallback rule, this harness instead replays the REAL request bodies the
per-call-proxy spike captured on 2026-09-28 against a live local Ollama, for
each lever under test. Those 24 dumps are the golden-fixture continuation
calls from routing-golden-v1 cases c001-c006 (rsi-engine-probe), the same
calls docs/spikes/per-call-proxy-2026-09-28.md reports on. Nothing here
touches Anthropic or needs a subscription.

What this measures, and what it does not
------------------------------------------
- Per-call latency, validation pass/fail, and tool-choice AGREEMENT with the
  tool the original successful routed run actually used at that decision
  point (ground truth for the 19 calls that were served locally in the
  parent spike; the 5 calls the classifier kept on Claude have no local
  ground truth and are reported separately).
- A wall-clock ESTIMATE per lever, reconstructed by replaying the 2026-09-28
  session timeline (cases.jsonl + routed.jsonl) and substituting this
  lever's latency for each of the 19 originally-served calls, keeping every
  Anthropic-forwarded call's ORIGINAL recorded latency unchanged. This is a
  reconstruction from real per-call data, not a fresh live run: a genuinely
  different local reply could in principle change what Claude Code does
  next. Flagged wherever it is used.
- It does NOT re-measure "tasks passing" live for every lever (that needs a
  live agent loop, which needs auth). The one number this harness cannot
  produce is a fresh live pass/fail; docs/spikes/local-speed-2026-09-28.md
  says so plainly for every lever row.

Run:
  python3 scripts/spikes/local_speed_replay.py --lever <name> \
      --dumps-dir <fixture>/dumps --ref-log <fixture>/routed.jsonl \
      --cases-log <fixture>/cases.jsonl --out <out>.jsonl [lever flags]

See LEVERS at the bottom for the exact flag sets used per row of the report.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import per_call_proxy as pcp  # noqa: E402  (sibling module, same dir)

# ── the 4 tools this fixture's tasks actually need (of 24 total) ───────────
TRIM_TOOLS = ["Read", "Edit", "Write", "Bash"]
CONDENSED_SYSTEM = (
    "You are continuing an autonomous coding-fix session. You will be shown "
    "the result of your most recent tool call. Decide the single next step: "
    "call exactly one tool if more work is needed (use the exact tool name "
    "and argument schema given), or reply with a short final text summary "
    "if the task is already done. Do not repeat a step you already "
    "completed successfully."
)


def _prev_tool(shape: dict) -> str | None:
    pt = shape.get("prev_tool_uses") or []
    return pt[0] if pt else None


def trim_body(body: dict) -> dict:
    """Condensed system + tool subset (exact names/schemas kept) + shorter
    history: first user turn + the last exchange only."""
    b = copy.deepcopy(body)
    b["system"] = CONDENSED_SYSTEM
    b["tools"] = [t for t in (b.get("tools") or []) if t.get("name") in TRIM_TOOLS]
    b["tools"].sort(key=lambda t: TRIM_TOOLS.index(t["name"]))
    msgs = b.get("messages") or []
    non_sys = [m for m in msgs if m.get("role") != "system"]
    if len(non_sys) > 3:
        b["messages"] = [non_sys[0]] + non_sys[-2:]
    return b


async def call_ollama(client: httpx.AsyncClient, url: str, payload: dict,
                       hedge_s: float | None, overall_timeout: float):
    """Returns (resp_json_or_None, err_or_None, total_s, first_token_s_or_None)."""
    t0 = time.time()
    if not hedge_s:
        try:
            r = await client.post(url + "/api/chat", json=payload, timeout=overall_timeout)
            r.raise_for_status()
            return r.json(), None, time.time() - t0, None
        except Exception as e:  # noqa: BLE001 - spike: any failure is a fallback
            return None, f"{type(e).__name__}: {e}"[:200], time.time() - t0, None

    payload = dict(payload, stream=True)
    try:
        async with client.stream("POST", url + "/api/chat", json=payload,
                                  timeout=overall_timeout) as resp:
            resp.raise_for_status()
            agen = resp.aiter_lines()
            try:
                first = await asyncio.wait_for(agen.__anext__(), timeout=hedge_s)
            except asyncio.TimeoutError:
                return None, "hedge_timeout", time.time() - t0, None
            first_token_s = time.time() - t0
            lines = [first]
            async for line in agen:
                if line:
                    lines.append(line)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"[:200], time.time() - t0, None

    objs = [json.loads(x) for x in lines if x]
    content = "".join(o.get("message", {}).get("content", "") for o in objs)
    tool_calls = []
    for o in objs:
        tc = o.get("message", {}).get("tool_calls")
        if tc:
            tool_calls.extend(tc)
    merged = dict(objs[-1], message={"content": content, "tool_calls": tool_calls})
    return merged, None, time.time() - t0, first_token_s


async def run(args: argparse.Namespace) -> None:
    ref = {Path(json.loads(l)["dump"]).name: json.loads(l)
           for l in open(args.ref_log) if json.loads(l).get("dump")}
    dumps = sorted(Path(args.dumps_dir).glob("*.json"))
    client = httpx.AsyncClient(timeout=httpx.Timeout(args.overall_timeout, connect=10.0))

    if args.warmup:
        pcp.ARGS = argparse.Namespace(num_ctx=args.num_ctx, local_timeout=args.overall_timeout)
        warm_body = json.load(open(dumps[0]))
        payload = pcp.to_ollama(warm_body, "ollama/" + args.model)
        payload["keep_alive"] = args.keep_alive
        t0 = time.time()
        await call_ollama(client, args.ollama_url, payload, None, args.overall_timeout)
        print(f"# warm-up call: {time.time() - t0:.1f}s", file=sys.stderr)

    results = []
    for dpath in dumps:
        key = dpath.name
        r = ref.get(key)
        if not r or not r.get("route"):
            continue  # not a continuation candidate in the original run
        body = json.load(open(dpath))
        shape = pcp._shape(body)
        pcp.ARGS = argparse.Namespace(num_ctx=args.num_ctx, local_timeout=args.overall_timeout)
        use_body = trim_body(body) if args.trim else body
        payload = pcp.to_ollama(use_body, "ollama/" + args.model)
        payload["keep_alive"] = args.keep_alive
        easy = _prev_tool(shape) in ("Read", "Bash", None)
        payload["options"]["num_predict"] = (args.cap_easy if easy else args.cap_hard)

        resp, err, total_s, first_token_s = await call_ollama(
            client, args.ollama_url, payload, args.hedge_s, args.overall_timeout)

        rec = {"dump": key, "ts": r["ts"], "orig_outcome": r["route"]["outcome"],
               "orig_target": r["route"]["target"], "orig_latency_s": r["route"].get("latency_s"),
               "orig_resp_blocks": r["route"].get("resp_blocks"), "prev_tool": _prev_tool(shape),
               "total_s": round(total_s, 2), "first_token_s": round(first_token_s, 2) if first_token_s else None,
               "err": err}
        if resp is not None:
            rec["prompt_eval_count"] = resp.get("prompt_eval_count")
            rec["prompt_eval_duration_s"] = round((resp.get("prompt_eval_duration") or 0) / 1e9, 2)
            rec["eval_count"] = resp.get("eval_count")
            rec["eval_duration_s"] = round((resp.get("eval_duration") or 0) / 1e9, 2)
            rec["load_duration_s"] = round((resp.get("load_duration") or 0) / 1e9, 2)
            m, verr = pcp.from_ollama(resp, use_body, "ollama/" + args.model)
            rec["valid"] = verr is None
            rec["validation_error"] = verr
            if m:
                rec["resp_blocks"] = [b["type"] + (":" + b["name"] if b.get("name") else "")
                                       for b in m["content"]]
                orig_names = [x.split(":")[1] for x in (r["route"].get("resp_blocks") or [])
                              if x.startswith("tool_use:")]
                new_names = [x.split(":")[1] for x in rec["resp_blocks"] if x.startswith("tool_use:")]
                if r["route"]["outcome"] == "served":
                    rec["agrees_with_original"] = (orig_names == new_names) if orig_names or new_names \
                        else (not orig_names and not new_names)
        else:
            rec["valid"] = False
            rec["validation_error"] = err
        results.append(rec)
        print(f"  {dpath.name} prev={rec['prev_tool']} orig={rec['orig_outcome']}"
              f"({rec['orig_latency_s']}s) -> {total_s:.1f}s valid={rec.get('valid')}"
              f" err={err}", file=sys.stderr)

    with open(args.out, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    lat = [r["total_s"] for r in results if r["err"] != "hedge_timeout"]
    hedged = sum(1 for r in results if r["err"] == "hedge_timeout")
    served_considered = [r for r in results if r["orig_outcome"] == "served"]
    invalid = [r for r in results if not r["valid"]]
    agree = [r for r in served_considered if "agrees_with_original" in r]
    agreeing = [r for r in agree if r["agrees_with_original"]]
    summary = {
        "lever": args.lever, "n": len(results), "n_served_class": len(served_considered),
        "median_s": round(statistics.median(lat), 2) if lat else None,
        "p90_s": round(statistics.quantiles(lat, n=10)[8], 2) if len(lat) >= 2 else (lat[0] if lat else None),
        "min_s": round(min(lat), 2) if lat else None, "max_s": round(max(lat), 2) if lat else None,
        "validation_failures": len(invalid), "hedged_to_fallback": hedged,
        "tool_choice_agreement": f"{len(agreeing)}/{len(agree)}" if agree else "n/a",
    }
    print(json.dumps(summary, indent=2))
    with open(args.out + ".summary.json", "w") as f:
        json.dump(summary, f, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lever", required=True)
    ap.add_argument("--dumps-dir", required=True)
    ap.add_argument("--ref-log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="qwen3-coder:30b")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--keep-alive", default="5m",
                     help='e.g. "5m" or "-1" (forever); bare integers are sent as JSON numbers, '
                          "since Ollama's keep_alive rejects a numeric string with no duration unit")
    ap.add_argument("--trim", action="store_true")
    ap.add_argument("--cap-easy", type=int, default=4096)
    ap.add_argument("--cap-hard", type=int, default=4096)
    ap.add_argument("--hedge-s", type=float, default=None)
    ap.add_argument("--overall-timeout", type=float, default=180.0)
    ap.add_argument("--warmup", action="store_true")
    args = ap.parse_args()
    try:
        args.keep_alive = int(args.keep_alive)
    except ValueError:
        pass  # duration string, e.g. "5m"
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
