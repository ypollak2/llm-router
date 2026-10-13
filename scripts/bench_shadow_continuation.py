#!/usr/bin/env python3
"""P1.7-c: what the classifier shadow adds to a continuation call's latency.

Each iteration posts one turn-first call with ``H`` earlier messages to the REAL proxy
app (``build_app``; Anthropic is an ``httpx.MockTransport``), which schedules the shadow
classification (``assemble`` in a worker thread, then the classifier), and then at once
posts a continuation call. The continuation's wall time through the proxy is the
measurement: with the shadow on, that call shares the GIL with ``assemble``.

The classifier is an instant fake (no Ollama, no network): the measurement isolates the
proxy-side cost. The rules classifier is a constant (moderate/code). Arms ``off`` and
``shadow`` (``LLM_ROUTER_LOCAL_CLASSIFIER``) run interleaved per iteration so load drift
hits both. Output: JSON with p50 / p95 / max per (fixture, H, arm), the shadow-minus-off
p95, the ``assemble`` time per fixture, load average and free memory.

History fixtures (no real prompt text; synthetic strings only):

* ``agentic``: Claude Code's shape. Blocks of 20 messages: a human prompt (a 2,000-char
  ``<system-reminder>`` text block, then 400 chars typed), an assistant text + tool_use,
  then 9 tool_use / tool_result pairs whose results carry a 600-char reminder text block.
* ``dense``: every message is text (the shape of
  ``test_a_long_history_is_assembled_off_the_request_path``): user = 3,000-char reminder +
  3,600 chars typed, assistant = 3,000 chars.
* ``one_prompt``: the worst case for a backward scan: one human prompt at message 0, then
  only tool_use / tool_result pairs whose results carry reminder text blocks.

Decision-ledger arm (P1.7 task 3): in both arms the harness also runs ``engine.decide`` (which
appends a ledger row) in a worker thread, started just before the continuation, so the
continuation shares the GIL and the disk with the ledger write. In the ``shadow`` arm
``decision_ledger._force_shadow`` (a private module attribute; no environment value can set it)
with the rate at the 10% top of the 5-10% band makes :mod:`llm_router.decision_ledger` mark sampled
rows, so the arm difference is the whole shadow.
The run writes its ledger under a temp ``LLM_ROUTER_HOME``, never the real state dir.

Verdict (P1.7-c): shadow-on minus shadow-off continuation p95 <= 5 ms on every fixture at
>= 1,800 messages, n >= 100 per arm, free memory >= 20% and load1 <= 4.0 before and after
(``decision_ledger.gate_verdict``). ``--gate`` writes ``p17c_gate.json`` into the REAL state dir
only when that verdict passes; ``LLM_ROUTER_DECISION_SHADOW=on`` stays inert without it. That gate
governs the decision-ledger ``shadow`` flag only. The same run also measures the pre-existing
classifier shadow (``LLM_ROUTER_LOCAL_CLASSIFIER=shadow``, on since 2026-10-08): its added p95 is
reported as evidence and gates nothing. A
harness run is never proof that the live proxy is fast: it is synthetic, single-process.

Run: ``uv run python scripts/bench_shadow_continuation.py --n 100 --out result.json [--gate]``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")  # benchmark traffic, never production spend
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

try:
    from llm_router import decision_ledger as DL  # noqa: E402
    from llm_router import engine  # noqa: E402
    from llm_router import local_classifier as lc  # noqa: E402
    from llm_router.proxy import backends as pb  # noqa: E402
    from llm_router.proxy.cls_input import assemble  # noqa: E402
    from tests.test_proxy_tiers import Upstream, _app, _first, _post, _req  # noqa: E402
except Exception as _e:  # an uncaught ImportError would exit 1 == "verdict failed"
    _msg = "harness error (import): %s: %s" % (type(_e).__name__, _e)
    print(_msg, file=sys.stderr)
    if "--out" in sys.argv[:-1]:
        Path(sys.argv[sys.argv.index("--out") + 1]).write_text(json.dumps(
            {"rows": [], "meta": {"error": traceback.format_exc()},
             "verdict": {"passed": False, "reason": _msg}}, indent=1))
    sys.exit(2)

SID = "11111111-2222-3333-4444-555555555555"
REMINDER = "<system-reminder>" + "r" * 2000 + "</system-reminder>"
SMALL_REMINDER = "<system-reminder>" + "s" * 600 + "</system-reminder>"


def _tool_pair(j: int) -> list[dict]:
    return [
        {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{j}", "name": "Read",
                                           "input": {"file_path": f"/x/f{j}.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{j}", "content": "c" * 3000},
                                     {"type": "text", "text": SMALL_REMINDER}]},
    ]


def history(kind: str, h: int) -> list[dict]:
    msgs: list[dict] = []
    j = 0
    if kind == "dense":
        while len(msgs) < h:
            msgs.append({"role": "user", "content": [{"type": "text", "text": "<system-reminder>" + "r" * 3000
                                                      + "</system-reminder>\n" + "hello world " * 300}]})
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok " * 1000}]})
    elif kind == "agentic":
        while len(msgs) < h:
            msgs.append({"role": "user", "content": [{"type": "text", "text": REMINDER},
                                                     {"type": "text", "text": f"prompt {j} " + "p" * 400}]})
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "a" * 300},
                                                          {"type": "tool_use", "id": f"t{j}", "name": "Bash",
                                                           "input": {"command": "ls"}}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{j}",
                                                      "content": "c" * 3000}]})
            j += 1
            for _ in range(8):
                msgs += _tool_pair(j)
                j += 1
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "done " * 60}]})
    elif kind == "one_prompt":
        msgs.append({"role": "user", "content": [{"type": "text", "text": "the only prompt " + "p" * 400}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "a" * 300}]})
        while len(msgs) < h:
            msgs += _tool_pair(j)
            j += 1
    else:
        raise ValueError(kind)
    msgs = msgs[:h]
    # the request must end on an assistant turn so the new human prompt follows it
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1] = {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
    return msgs


def turn_first(kind: str, h: int, i: int) -> dict:
    body = _first()
    body["messages"] = history(kind, h) + [
        {"role": "user", "content": [{"type": "text", "text": f"final prompt number {i}: change file{i}.py"}]}]
    body["metadata"] = {"user_id": json.dumps({"session_id": SID})}
    return body


async def _fake_classify(assembled, *, session_id, text_sha, **kw):
    return lc.parse_verdict(json.dumps({
        "scope": 2, "ambiguity": 2, "repo_knowledge": 2, "reasoning_depth": 2, "execution_load": 2, "risk": 2,
        "needs_tools": True, "tier": "sonnet", "task_type": "code", "qa": False, "needs_repo_context": True,
        "local_eligible": False, "reason": "x"}), ms=1.0)


async def _choose(text, pinned, *, anthropic=False):
    return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(-(-q * len(xs) // 1)) - 1))]


def machine() -> dict:
    out = {"loadavg": os.getloadavg(), "ncpu": os.cpu_count()}
    try:
        txt = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
        line = [x for x in txt.splitlines() if "free percentage" in x]
        out["free_pct"] = int(line[0].rsplit(":", 1)[1].strip().rstrip("%")) if line else None
    except (OSError, subprocess.SubprocessError, ValueError):
        out["free_pct"] = None
    return out


async def run(kinds: list[str], sizes: list[int], n: int, warmup: int) -> dict:
    lc.classify_async = _fake_classify
    pb.choose_model = _choose
    os.environ["LLM_ROUTER_OLLAMA_URL"] = "http://127.0.0.1:9"
    results: dict = {"meta": {"n": n, "warmup": warmup, "python": sys.version.split()[0],
                              "machine_before": machine(), "started": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                     time.gmtime())},
                     "rows": []}
    for kind in kinds:
        for h in sizes:
            sample = turn_first(kind, h, 0)
            t = time.perf_counter()
            for _ in range(5):
                assemble(sample)
            asm_ms = (time.perf_counter() - t) * 1000 / 5
            lat = {"off": [], "shadow": []}
            scheduled = 0
            with tempfile.TemporaryDirectory() as d:
                apps = {}
                for arm in ("off", "shadow"):
                    sub = Path(d) / arm
                    sub.mkdir()
                    apps[arm] = _app(sub, Upstream(), classifier_shadow_path=sub / "classifier_shadow.jsonl")
                for i in range(n + warmup):
                    for arm in (("off", "shadow") if i % 2 == 0 else ("shadow", "off")):
                        os.environ["LLM_ROUTER_LOCAL_CLASSIFIER"] = arm
                        DL._force_shadow = arm == "shadow"  # private harness switch, not an env value
                        lc._reset_state()
                        app = apps[arm]
                        body = turn_first(kind, h, i)
                        assert (await _post(app, body)).status_code == 200
                        decide_task = asyncio.create_task(asyncio.to_thread(
                            engine.decide, f"final prompt number {i}: change file{i}.py",
                            door="proxy", session_id=SID))
                        t0 = time.perf_counter()
                        r = await _post(app, _req())
                        dt = (time.perf_counter() - t0) * 1000
                        await decide_task
                        assert r.status_code == 200
                        sched = app.state.cls_shadow
                        if arm == "shadow":
                            scheduled += len(sched._tasks)
                        await sched.drain()
                        if i >= warmup:
                            lat[arm].append(dt)
                recs = (Path(d) / "shadow" / "classifier_shadow.jsonl")
                n_recs = len(recs.read_text().splitlines()) if recs.exists() else 0
            row = {"fixture": kind, "messages": h, "assemble_ms": round(asm_ms, 2),
                   "shadow_records": n_recs, "pending_at_continuation": scheduled}
            for arm in ("off", "shadow"):
                xs = lat[arm]
                row[arm] = {"n": len(xs), "p50": round(pct(xs, .5), 2), "p95": round(pct(xs, .95), 2),
                            "max": round(max(xs), 2)}
            row["added_p95_ms"] = round(row["shadow"]["p95"] - row["off"]["p95"], 2)
            results["rows"].append(row)
            print(json.dumps(row), flush=True)
    results["meta"]["machine_after"] = machine()
    results["verdict"] = DL.gate_verdict(results)
    DL._force_shadow = False
    results["meta"]["shadow_drain_ok"] = engine.shadow_drain()
    if not results["meta"]["shadow_drain_ok"]:
        results["meta"]["shadow_drain_error"] = "engine shadow queue not drained within timeout"
        results["verdict"]["passed"] = False  # never write a gate on a run whose shadow writes were still pending
        results["verdict"]["reason"] = results["meta"]["shadow_drain_error"]
    rows = DL.read_ledger()
    results["meta"]["decision_rows"] = len(rows)
    results["meta"]["decision_rows_shadow"] = sum(1 for r in rows if r.get("shadow"))
    return results


# Exit codes: 0 verdict passed, 1 verdict FAILED (out written), 2 harness error (out written, passed=false).
EXIT_VERDICT_FAILED = 1
EXIT_ERROR = 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--fixtures", default="agentic,dense,one_prompt")
    ap.add_argument("--sizes", default="0,600,1800")
    ap.add_argument("--out", default=None)
    ap.add_argument("--gate", action="store_true",
                    help="write p17c_gate.json into the real state dir if the P1.7-c verdict passes")
    a = ap.parse_args()
    final_drain_ok = True
    real_gate = DL.gate_path()  # resolved BEFORE the run redirects the state dir
    try:
        with tempfile.TemporaryDirectory() as home:
            os.environ["LLM_ROUTER_HOME"] = home
            os.environ["LLM_ROUTER_DECISION_SHADOW_RATE"] = str(DL.RATE_MAX)
            engine.clear_cache(persistent=False)
            try:
                res = asyncio.run(run(a.fixtures.split(","), [int(x) for x in a.sizes.split(",")], a.n,
                                      a.warmup))
            finally:  # also on error: a late shadow write must not re-create the removed home
                final_drain_ok = engine.shadow_drain()
    except BaseException as e:  # harness error: an uncaught one would exit 1 == "verdict failed"
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        err = "harness error: %s: %s" % (type(e).__name__, e)
        res = {"rows": [], "meta": {"error": traceback.format_exc()},
               "verdict": {"passed": False, "reason": err}}
        res["meta"]["shadow_drain_ok"] = final_drain_ok
        print(err, file=sys.stderr)
        if a.out:
            Path(a.out).write_text(json.dumps(res, indent=1))
        if a.gate:
            print("gate NOT written: harness error")
        sys.exit(EXIT_ERROR)
    print(json.dumps({"verdict": res["verdict"]}))
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1))
    if a.gate and res["verdict"]["passed"]:
        real_gate.parent.mkdir(parents=True, exist_ok=True)
        real_gate.write_text(json.dumps({"passed": True, **res}, indent=1))
        print("gate written:", real_gate)
    elif a.gate:
        print("gate NOT written: P1.7-c did not pass")
    sys.exit(0 if res["verdict"]["passed"] else EXIT_VERDICT_FAILED)


if __name__ == "__main__":
    main()
