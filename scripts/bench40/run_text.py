"""Run the 32 operational cases through the local agent loop."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, "/Users/yaliandrona/Projects/llm-router/src")
sys.path.insert(0, str(Path(__file__).parent))
from llm_router.hooks.agent_loop import run_agent_loop
import fixture, cases

model = sys.argv[1] if len(sys.argv) > 1 else "qwen3-coder:30b"
rows = []
for cat, name, prompt, check in cases.TEXT_CASES:
    fixture.build()
    t0 = time.time()
    try:
        out = run_agent_loop(prompt=prompt, model=model, project_root=fixture.ROOT,
                             timeout_per_call=90, deadline_s=180)
        err = None
    except Exception as e:
        out, err = None, f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    try:
        ok = bool(check(out))
    except Exception as e:
        ok, err = False, f"check:{e}"
    rows.append({"cat": cat, "case": name, "ok": ok, "s": round(dt, 1),
                 "err": err, "out": (out or "")[:160]})
    print(f"{cat:6s} {name:18s} {'PASS' if ok else 'FAIL'} {dt:6.1f}s"
          f"{'  ' + err if err else ''}", flush=True)

from collections import defaultdict
agg = defaultdict(lambda: [0, 0, 0.0])
for r in rows:
    a = agg[r["cat"]]; a[0] += r["ok"]; a[1] += 1; a[2] += r["s"]
print(f"\n{'category':10s}{'pass':>8s}{'rate':>8s}{'total s':>10s}")
for cat, (ok, n, s) in agg.items():
    print(f"{cat:10s}{ok:>3d}/{n:<4d}{ok/n:>8.0%}{s:>10.0f}")
tot_ok = sum(r["ok"] for r in rows)
print(f"\nTOTAL {tot_ok}/{len(rows)} = {tot_ok/len(rows):.0%}   "
      f"({sum(r['s'] for r in rows):.0f}s)")
json.dump(rows, open(Path(__file__).parent / "text_results.json", "w"), indent=2)
