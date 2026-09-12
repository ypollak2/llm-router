"""Harder multi-step tasks — the 5-task harness is saturated at 5/5.

Stage 4 (split planning from editing) cannot be evaluated against a benchmark
with no headroom. These need several tool calls AND a correct edit, and every
check is mechanical: file content, or a string that must appear.
"""
import json, shutil, sys, time
from pathlib import Path
sys.path.insert(0, "/Users/yaliandrona/Projects/llm-router/src")
from llm_router.hooks.agent_loop import run_agent_loop

BASE = Path(__file__).parent / "hardrepo"
PRISTINE = Path(__file__).parent / "hardrepo_pristine"

FILES = {
    "src/calc.py": (
        "from src.util import clamp\n\n\n"
        "def add(a, b):\n    return a + b\n\n\n"
        "def scale(x, factor):\n    return clamp(x * factor)\n"),
    "src/util.py": (
        "MAX_VALUE = 100\n\n\n"
        "def clamp(x):\n"
        "    \"\"\"Clamp x to MAX_VALUE.\"\"\"\n"
        "    return min(x, MAX_VALUE)\n"),
    "src/report.py": (
        "from src.calc import scale\n\n\n"
        "def report(values):\n"
        "    return [scale(v, 2) for v in values]\n"),
    "tests/test_calc.py": (
        "from src.calc import add\n\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n"),
    "README.md": "# Demo\n\nA tiny project.\n",
}


def build():
    shutil.rmtree(BASE, ignore_errors=True)
    for rel, body in FILES.items():
        p = BASE / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def read(rel):
    try:
        return (BASE / rel).read_text()
    except OSError:
        return ""


TASKS = [
    ("raise-limit",
     "MAX_VALUE is defined somewhere in this project. Change it to 250.",
     lambda out: "MAX_VALUE = 250" in read("src/util.py")),

    ("trace-import",
     "Which file defines the `clamp` function that src/calc.py imports? "
     "Answer with the file path.",
     lambda out: "util.py" in (out or "")),

    ("add-param",
     "Add a keyword argument `offset=0` to the `add` function in src/calc.py "
     "and make it return a + b + offset. Do not change anything else.",
     lambda out: ("offset=0" in read("src/calc.py")
                  and "a + b + offset" in read("src/calc.py"))),

    ("count-users",
     "How many files in this project import from src.calc? Answer with the number.",
     lambda out: "2" in (out or "")),

    ("docstring",
     "Add a one-line docstring to the `add` function in src/calc.py saying what "
     "it does. Keep the existing behaviour.",
     lambda out: '"""' in read("src/calc.py").split("def add")[-1][:200]),

    ("run-tests",
     "Run the test suite with `python3 -m pytest tests -q` and tell me whether "
     "it passed.",
     lambda out: any(w in (out or "").lower() for w in ("pass", "ok", "1 passed"))),
]

model = sys.argv[1] if len(sys.argv) > 1 else "qwen3-coder:30b"
rows = []
for name, prompt, check in TASKS:
    build()
    t0 = time.time()
    try:
        out = run_agent_loop(prompt=prompt, model=model, project_root=BASE,
                             timeout_per_call=90, deadline_s=180)
        err = None
    except Exception as e:
        out, err = None, f"{type(e).__name__}: {e}"
    dt = time.time() - t0
    try:
        ok = bool(check(out))
    except Exception as e:
        ok, err = False, f"check: {e}"
    rows.append({"task": name, "ok": ok, "s": round(dt, 1), "err": err,
                 "out": (out or "")[:200]})
    print(f"{name:14s} {'PASS' if ok else 'FAIL'}  {dt:6.1f}s"
          f"{'  ' + err if err else ''}", flush=True)

print(f"\n{model}: {sum(r['ok'] for r in rows)}/{len(rows)} passed  "
      f"(total {sum(r['s'] for r in rows):.0f}s)")
json.dump(rows, open(Path(__file__).parent / "hard_results.json", "w"), indent=2)
