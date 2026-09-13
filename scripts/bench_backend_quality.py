#!/usr/bin/env python3
"""Does routing a task to a local model instead of Claude/Codex cost you quality?

The routing ledger can't answer this: 16,869 of its 17,195 rows carry
``verification_attempted=false``, and its top models are test fixtures. Cost is
proven; quality was assumed. This measures it.

Method — the same discipline as scripts/bench_grounding.py: no model judges
another model. Every task runs against a freshly rebuilt sandbox project and is
scored by a VERIFIER that either greps the resulting files, or imports the
resulting code and asserts on its behaviour. A pass is a pass because Python
says so.

Two things are scored per task, not one:

  correct  — the verifier passed (the work was done)
  clean    — no file outside the task's declared blast radius changed

A backend that gets the right answer by rewriting four unrelated files is not
delivering the same quality as one that touches a single line, and a single
pass/fail number hides that. Q&A tasks declare an EMPTY blast radius: touching
anything at all to answer a question is collateral damage.

Backends are driven exactly as llm_router drives them (src/llm_router/
codex_agent.py, claude_agent.py, hooks/agent_loop.py), with one deliberate
difference: the CLIs are given write permission. Without it Claude and Codex
would fail every edit task for permission reasons and the run would read as a
quality collapse that isn't one. Router enforcement hooks are disabled in the
subprocess environment so a nested CLI isn't intercepted and re-routed.

    python3 scripts/bench_backend_quality.py --backend local
    python3 scripts/bench_backend_quality.py --backend claude --tasks 5
    python3 scripts/bench_backend_quality.py --report          # merge + table
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT_DIR = Path(os.environ.get("BENCH_OUT", ROOT / "scripts" / "backend_quality_out"))

# ── The sandbox project ──────────────────────────────────────────────────────
# Small enough that every task has one unambiguous right answer, layered enough
# that a task can require following an import across files.

FILES: dict[str, str] = {
    "src/__init__.py": "",
    "src/util.py": (
        "MAX_VALUE = 100\n"
        "\n"
        "\n"
        "def clamp(x):\n"
        '    """Clamp x to MAX_VALUE."""\n'
        "    return min(x, MAX_VALUE)\n"
    ),
    "src/calc.py": (
        "from src.util import clamp\n"
        "\n"
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def scale(x, factor):\n"
        "    return clamp(x * factor)\n"
    ),
    "src/report.py": (
        "import json\n"
        "\n"
        "from src.calc import scale\n"
        "\n"
        "\n"
        "def report(values):\n"
        "    return [scale(v, 2) for v in values]\n"
    ),
    "src/config.py": (
        "DEFAULTS = {\n"
        '    "timeout": 30,\n'
        '    "verbose": False,\n'
        '    "workers": 4,\n'
        "}\n"
        "\n"
        "\n"
        "def get_setting(name):\n"
        "    return DEFAULTS[name]\n"
    ),
    "src/parser.py": (
        "def parse_line(line):\n"
        '    """Split "name:score" and return (name, score_as_int)."""\n'
        '    parts = line.split(":")\n'
        "    return parts[0], int(parts[1][1:])\n"
    ),
    "tests/test_calc.py": (
        "from src.calc import add\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 2) == 4\n"
    ),
    "tests/test_parser.py": (
        "from src.parser import parse_line\n"
        "\n"
        "\n"
        "def test_parse_line():\n"
        '    assert parse_line("alice:42") == ("alice", 42)\n'
    ),
    "README.md": "# Demo\n\nA tiny project.\n",
}


# Caches and agent scratch dirs are not the model's doing — never counted as stray.
NOISE = {"__pycache__", ".pytest_cache", ".git", ".claude", ".codex", ".DS_Store",
         ".ruff_cache", ".mypy_cache"}


def build(sandbox: Path, files: dict[str, str] | None = None) -> None:
    shutil.rmtree(sandbox, ignore_errors=True)
    for rel, body in (files or FILES).items():
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def snapshot(sandbox: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(sandbox.rglob("*")):
        if p.is_file() and not (NOISE & set(p.parts)) and p.name not in NOISE:
            out[str(p.relative_to(sandbox))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


# ── Verifiers ────────────────────────────────────────────────────────────────
# A verifier is a Python snippet run with cwd=sandbox. Exit 0 = the work is
# correct. `OUT` holds the backend's final text answer; `read(rel)` reads a
# sandbox file. Behavioural checks import the edited code and assert on it, so
# a backend cannot pass by pattern-matching the prompt back at us.

PREAMBLE = r'''
import json, os, subprocess, sys
OUT = (os.environ.get("BENCH_ANSWER") or "").lower()
def read(rel):
    try:
        return open(rel, encoding="utf-8").read()
    except OSError:
        return ""
def run(code):
    """Import the sandbox package fresh in a subprocess and run assertions."""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=os.getcwd(), timeout=60)
    assert r.returncode == 0, (r.stdout + r.stderr)[-400:]
def pytest_passes(path="tests"):
    r = subprocess.run([sys.executable, "-m", "pytest", path, "-q"],
                       capture_output=True, text=True, cwd=os.getcwd(), timeout=180)
    return r.returncode == 0, (r.stdout + r.stderr)[-400:]
def words(*w):
    assert any(x in OUT for x in w), "answer was: " + OUT[:200]
def num(n):
    """The answer must actually contain the integer n as a number."""
    import re
    found = [int(x) for x in re.findall(r"-?\d+", OUT)]
    assert n in found, "answer was: " + OUT[:200]
def yesno(expected):
    """Decide yes/no without matching 'no' inside 'not' or 'cannot'."""
    import re
    t = re.sub(r"[^a-z ]", " ", OUT)
    toks = t.split()
    neg = any(k in toks for k in ("no", "nope")) or "does not" in OUT or "doesn t" in t
    pos = "yes" in toks
    got = "no" if (neg and not pos) else ("yes" if pos and not neg else "unclear")
    assert got == expected, "answer was: " + OUT[:200]
'''

QA = "qa"
EDIT = "edit"

# (id, kind, prompt, allowed_files, verifier)
TASKS: list[tuple[str, str, str, list[str], str]] = [
    # ── Q&A: read the project, answer. Blast radius is empty. ────────────────
    ("qa-defines-clamp", QA,
     "Which file in this project defines the `clamp` function that src/calc.py "
     "imports? Answer with the file path and nothing else.",
     [], 'words("util.py")'),

    ("qa-count-importers", QA,
     "How many files in this project import from src.calc? Answer with just the number.",
     [], 'num(2)'),

    ("qa-max-value", QA,
     "What is the current value of MAX_VALUE? Answer with just the number.",
     [], 'num(100)'),

    ("qa-func-count", QA,
     "How many functions are defined in src/calc.py? Answer with just the number.",
     [], 'num(2) if any(c.isdigit() for c in OUT) else words("two")'),

    ("qa-failing-test", QA,
     "Run the test suite with `python3 -m pytest tests -q`. Which test fails? "
     "Answer with the name of the failing test.",
     [], 'words("test_parse_line", "test_parser")'),

    ("qa-timeout-default", QA,
     "What is the default value of the `timeout` setting? Answer with just the number.",
     [], 'num(30)'),

    ("qa-imports-util", QA,
     "Which file under src/ imports from src.util? Answer with the file path.",
     [], 'words("calc.py")'),

    ("qa-defaults-keys", QA,
     "How many keys does the DEFAULTS dictionary have? Answer with just the number.",
     [], 'num(3) if any(c.isdigit() for c in OUT) else words("three")'),

    ("qa-report-imports", QA,
     "Which function does src/report.py import from src.calc? Answer with just "
     "the function name.",
     [], 'words("scale")'),

    ("qa-clamp-lower", QA,
     "Does `clamp` enforce a lower bound on its input? Answer yes or no.",
     [], 'yesno("no")'),

    ("qa-unused-import", QA,
     "One file under src/ has an import it never uses. Which module is imported "
     "but unused? Answer with just the module name.",
     [], 'words("json")'),

    # Kept verbatim as it was run, and it is a BAD question: it never says the
    # function lives in this project, so a backend can reasonably answer "no
    # such function is defined in this context" (Claude did exactly that, and
    # the local loop returned nothing). qa-report-output-v2 below is the
    # anchored rewrite; both are reported rather than quietly swapping one for
    # the other.
    ("qa-report-output", QA,
     "If I call report([10, 60]) what does it return? Answer with the list.",
     [], 'num(20); num(100)'),

    ("qa-report-output-v2", QA,
     "In this project, src/report.py defines report(). If I call "
     "report([10, 60]), what does it return? Answer with the resulting list.",
     [], 'num(20); num(100)'),

    # ── Edits: change the project. Verified by behaviour where possible. ─────
    ("ed-raise-limit", EDIT,
     "MAX_VALUE is defined somewhere in this project. Change it to 250. "
     "Change nothing else.",
     ["src/util.py"],
     'run("from src.util import MAX_VALUE; assert MAX_VALUE == 250")'),

    ("ed-add-offset", EDIT,
     "Add a keyword argument `offset=0` to the `add` function in src/calc.py so "
     "it returns a + b + offset. Keep the existing behaviour when offset is not "
     "passed.",
     ["src/calc.py"],
     'run("from src.calc import add; assert add(2,2)==4; assert add(2,2,offset=3)==7")'),

    ("ed-docstring", EDIT,
     "Add a one-line docstring to the `add` function in src/calc.py saying what "
     "it does. Keep the existing behaviour.",
     ["src/calc.py"],
     'run("from src.calc import add; assert (add.__doc__ or \'\').strip(); assert add(2,2)==4")'),

    ("ed-fix-parser", EDIT,
     "The test in tests/test_parser.py fails. Fix the bug in the source so the "
     "whole test suite passes. Do not change the test.",
     ["src/parser.py"],
     'ok, log = pytest_passes(); assert ok, log\n'
     'assert read("tests/test_parser.py") == open(os.environ["BENCH_ORIG_TEST"]).read()'),

    ("ed-clamp-lower", EDIT,
     "Give `clamp` in src/util.py an optional lower bound: a keyword argument "
     "`lo=None` that, when given, clamps the result to be at least `lo`. "
     "Behaviour without it must not change.",
     ["src/util.py"],
     'run("from src.util import clamp; assert clamp(500)==100; assert clamp(-5)==-5; '
     'assert clamp(-5, lo=0)==0")'),

    ("ed-rename-scale", EDIT,
     "Rename the function `scale` to `scale_value` everywhere in this project, "
     "including every call site. The project must still work afterwards.",
     ["src/calc.py", "src/report.py"],
     'run("from src.report import report; assert report([10])==[20]")\n'
     'assert "scale_value" in read("src/calc.py") and "scale_value" in read("src/report.py")\n'
     'import re; assert not re.search(r"(?<!_)\\bscale\\b(?!_)", read("src/report.py"))'),

    ("ed-average", EDIT,
     "Add a function `average(values)` to src/calc.py that returns the "
     "arithmetic mean of a list of numbers, and returns 0 for an empty list.",
     ["src/calc.py"],
     'run("from src.calc import average; assert average([2,4])==3; assert average([])==0")'),

    ("ed-add-retries", EDIT,
     'Add a key "retries" with the value 3 to the DEFAULTS dictionary in '
     "src/config.py. Leave the other settings alone.",
     ["src/config.py"],
     'run("from src.config import DEFAULTS; assert DEFAULTS[\'retries\']==3; '
     'assert DEFAULTS[\'timeout\']==30; assert len(DEFAULTS)==4")'),

    ("ed-drop-unused", EDIT,
     "One file under src/ imports a module it never uses. Remove that import "
     "and change nothing else.",
     ["src/report.py"],
     'assert "import json" not in read("src/report.py")\n'
     'run("from src.report import report; assert report([10])==[20]")'),

    ("ed-new-test", EDIT,
     "Add a test to tests/test_calc.py that checks `scale(10, 2)` returns 20. "
     "The whole suite except the already-failing parser test must pass.",
     ["tests/test_calc.py"],
     'ok, log = pytest_passes("tests/test_calc.py"); assert ok, log\n'
     'assert "scale" in read("tests/test_calc.py")'),

    ("ed-type-hints", EDIT,
     "Add type hints to the `add` function in src/calc.py: both parameters and "
     "the return value are ints. Keep the behaviour.",
     ["src/calc.py"],
     'run("from src.calc import add; import typing; h=typing.get_type_hints(add); '
     'assert h.get(\'return\') is int and h.get(\'a\') is int; assert add(2,2)==4")'),

    ("ed-safe-get", EDIT,
     "Make `get_setting` in src/config.py return None instead of raising when "
     "the setting name is not present.",
     ["src/config.py"],
     'run("from src.config import get_setting; assert get_setting(\'nope\') is None; '
     'assert get_setting(\'timeout\')==30")'),

    ("ed-extract-const", EDIT,
     "src/parser.py contains a bare numeric literal used as a slice offset. "
     "Extract it into a module-level constant with a descriptive name and use "
     "the constant instead. Do not change the behaviour.",
     ["src/parser.py"],
     'import re; src = read("src/parser.py")\n'
     'assert re.search(r"^[A-Z_]{4,}\\s*=\\s*\\d+", src, re.M), src[:300]\n'
     'run("from src.parser import parse_line; assert parse_line(\'a:x9\')==(\'a\',9)")'),
]


# ── The hard suite ───────────────────────────────────────────────────────────
# The easy suite ceilings out: every backend solves single-file, single-hop work,
# so it cannot answer "does routing to local cost me quality". These tasks put
# the CAUSE in a different file from the SYMPTOM, or make the correct edit
# depend on a caller the model has to go and find. A model that pattern-matches
# the prompt fails them; a model that reads the project passes.

HARD_FILES: dict[str, str] = {
    "src/__init__.py": "",
    "src/schema.py": (
        'REQUIRED = ("id", "name", "score")\n'
        "\n"
        "\n"
        "def validate(row):\n"
        '    """Return the row with score coerced to int.\n'
        "\n"
        "    Raises ValueError if a required field is missing.\n"
        '    """\n'
        "    for field in REQUIRED:\n"
        "        if field not in row:\n"
        '            raise ValueError(f"missing field: {field}")\n'
        "    out = dict(row)\n"
        '    out["score"] = int(row["score"])\n'
        "    return out\n"
    ),
    "src/store.py": (
        "class Store:\n"
        '    """Records, plus a name -> id index kept alongside them."""\n'
        "\n"
        "    def __init__(self):\n"
        "        self._rows = {}\n"
        "        self._by_name = {}\n"
        "\n"
        "    def add(self, row):\n"
        '        self._rows[row["id"]] = row\n'
        '        self._by_name[row["name"]] = row["id"]\n'
        "\n"
        "    def delete(self, rid):\n"
        "        return self._rows.pop(rid, None)\n"
        "\n"
        "    def get(self, rid):\n"
        "        return self._rows.get(rid)\n"
        "\n"
        "    def names(self):\n"
        "        return sorted(self._by_name)\n"
        "\n"
        "    def all(self):\n"
        "        return list(self._rows.values())\n"
    ),
    "src/query.py": (
        "def filter_rows(rows, min_score=None, name_contains=None):\n"
        "    out = []\n"
        "    for row in rows:\n"
        '        if min_score is not None and row["score"] < min_score:\n'
        "            continue\n"
        '        if name_contains is not None and name_contains not in row["name"]:\n'
        "            continue\n"
        "        out.append(row)\n"
        "    return out\n"
    ),
    "src/aggregate.py": (
        "def total(rows):\n"
        '    return sum(r["score"] for r in rows)\n'
        "\n"
        "\n"
        "def mean(rows):\n"
        '    """Arithmetic mean of scores, or 0 for no rows."""\n'
        "    return total(rows) / len(rows) if rows else 0\n"
    ),
    "src/page.py": (
        "def paginate(items, per_page):\n"
        '    """Return a list of pages, each holding at most per_page items."""\n'
        "    pages = []\n"
        "    for start in range(0, len(items) - per_page, per_page):\n"
        "        pages.append(items[start:start + per_page])\n"
        "    return pages\n"
    ),
    "src/tags.py": (
        "class Tagger:\n"
        "    def __init__(self):\n"
        "        self._seen = []\n"
        "\n"
        "    def tag(self, name, extra=[]):\n"
        "        extra.append(name)\n"
        "        self._seen = extra\n"
        "        return list(extra)\n"
    ),
    "src/pipeline.py": (
        "from src.aggregate import mean, total\n"
        "from src.query import filter_rows\n"
        "from src.schema import validate\n"
        "from src.store import Store\n"
        "\n"
        "\n"
        "def build_store(raw_rows):\n"
        "    store = Store()\n"
        "    for raw in raw_rows:\n"
        "        try:\n"
        "            store.add(validate(raw))\n"
        "        except Exception:\n"
        "            continue\n"
        "    return store\n"
        "\n"
        "\n"
        "def summarise(store, min_score=None):\n"
        "    rows = filter_rows(store.all(), min_score=min_score)\n"
        "    return {\n"
        '        "count": len(rows),\n'
        '        "total": total(rows),\n'
        '        "mean": mean(rows),\n'
        '        "names": store.names(),\n'
        "    }\n"
    ),
    "tests/test_store.py": (
        "from src.store import Store\n"
        "\n"
        "\n"
        "def test_add_and_get():\n"
        "    s = Store()\n"
        '    s.add({"id": 1, "name": "alice", "score": 10})\n'
        '    assert s.get(1)["name"] == "alice"\n'
    ),
}

# The failing integration test. Its symptom is in summarise(); its cause is
# Store.delete leaving the name index untouched. Only the tasks that are ABOUT
# that bug get this file, so it never becomes background noise for the others.
STALE_INDEX_TEST = (
    "from src.pipeline import build_store, summarise\n"
    "\n"
    "\n"
    "def test_deleted_names_disappear():\n"
    "    store = build_store([\n"
    '        {"id": 1, "name": "alice", "score": 10},\n'
    '        {"id": 2, "name": "bob", "score": 20},\n'
    "    ])\n"
    "    store.delete(1)\n"
    '    assert summarise(store)["names"] == ["bob"]\n'
)

# Single quotes only: this literal is spliced into a double-quoted run("...")
# snippet, and a double quote here would terminate that string early.
ROWS = "[{'id':1,'name':'alice','score':10},{'id':2,'name':'Bob','score':30}]"

HARD_TASKS: list[tuple] = [
    ("hd-stale-index", EDIT,
     "tests/test_pipeline.py fails. Find the cause in the source and fix it "
     "there. Do not modify the test.",
     ["src/store.py"],
     'ok, log = pytest_passes(); assert ok, log\n'
     'assert read("tests/test_pipeline.py") == open(os.environ["BENCH_STALE_TEST"]).read()',
     {"tests/test_pipeline.py": STALE_INDEX_TEST}),

    ("hd-name-the-bug", QA,
     "tests/test_pipeline.py fails. Which method contains the bug that causes "
     "it? Answer as ClassName.method_name and nothing else.",
     [],
     'words("delete"); assert "store" in OUT, OUT[:200]',
     {"tests/test_pipeline.py": STALE_INDEX_TEST}),

    ("hd-mean-none", EDIT,
     "Change `mean` in src/aggregate.py so it returns None for an empty list "
     "instead of 0. `summarise` must keep returning a numeric mean of 0 when no "
     "rows match \u2014 fix that at the call site, not by special-casing inside mean.",
     ["src/aggregate.py", "src/pipeline.py"],
     'run("from src.aggregate import mean; assert mean([]) is None; '
     'assert mean([{\'score\':2},{\'score\':4}]) == 3")\n'
     'run("from src.pipeline import build_store, summarise; '
     's = build_store([]); assert summarise(s)[\'mean\'] == 0")\n'
     'run("from src.pipeline import build_store, summarise; '
     's = build_store(' + ROWS + '); assert summarise(s)[\'mean\'] == 20")',
     {}),

    ("hd-case-insensitive", EDIT,
     "Make the `name_contains` filter in src/query.py case-insensitive. The "
     "order of the results must stay the order of the input rows, and the "
     "min_score behaviour must not change.",
     ["src/query.py"],
     'run("from src.query import filter_rows\\n'
     'rows=[{\'name\':\'alice\',\'score\':1},{\'name\':\'Bob\',\'score\':9},'
     '{\'name\':\'calvin\',\'score\':5}]\\n'
     'assert [r[\'name\'] for r in filter_rows(rows, name_contains=\'B\')] == [\'Bob\']\\n'
     'assert [r[\'name\'] for r in filter_rows(rows, name_contains=\'AL\')] == [\'alice\',\'calvin\']\\n'
     'assert [r[\'name\'] for r in filter_rows(rows, min_score=5)] == [\'Bob\',\'calvin\']")',
     {}),

    ("hd-last-page", EDIT,
     "src/page.py is meant to return every item across the pages it produces. "
     "It does not. Fix it without changing the page size behaviour.",
     ["src/page.py"],
     'run("from src.page import paginate\\n'
     'assert paginate(list(range(5)), 2) == [[0,1],[2,3],[4]]\\n'
     'assert paginate(list(range(4)), 2) == [[0,1],[2,3]]\\n'
     'assert paginate([], 2) == []\\n'
     'assert paginate([1], 5) == [[1]]")',
     {}),

    ("hd-paginate-count", QA,
     "Reading the code exactly as it is written today, how many items in total "
     "does paginate(list(range(7)), 3) return across all its pages? Answer with "
     "just the number.",
     [], 'num(6)', {}),

    ("hd-mutable-default", EDIT,
     "Tagger.tag() in src/tags.py returns the wrong result when it is called a "
     "second time without an `extra` argument. Fix the cause.",
     ["src/tags.py"],
     'run("from src.tags import Tagger\\n'
     't = Tagger()\\n'
     'assert t.tag(\'a\') == [\'a\']\\n'
     'assert t.tag(\'b\') == [\'b\']\\n'
     'u = Tagger()\\n'
     'assert u.tag(\'c\') == [\'c\']\\n'
     'assert t.tag(\'d\', [\'x\']) == [\'x\',\'d\']")',
     {}),

    ("hd-strict-validate", EDIT,
     "build_store currently discards rows that fail validation without telling "
     "anyone. Make an invalid row raise ValueError out of build_store instead. "
     "A batch of entirely valid rows must behave exactly as it does now.",
     ["src/pipeline.py"],
     'run("from src.pipeline import build_store, summarise\\n'
     's = build_store(' + ROWS + ')\\n'
     'assert summarise(s)[\'count\'] == 2\\n'
     'try:\\n'
     '    build_store([{\'id\':1,\'name\':\'x\'}])\\n'
     'except ValueError:\\n'
     '    pass\\n'
     'else:\\n'
     '    raise AssertionError(\'invalid row did not raise ValueError\')")',
     {}),

    ("hd-rename-all", EDIT,
     "Rename the `all` method on Store to `records`, including every caller in "
     "the project. The project must still work afterwards.",
     ["src/store.py", "src/pipeline.py"],
     'run("from src.store import Store; assert hasattr(Store,\'records\') '
     'and not hasattr(Store,\'all\')")\n'
     'run("from src.pipeline import build_store, summarise; '
     's = build_store(' + ROWS + '); assert summarise(s)[\'count\'] == 2")',
     {}),

    ("hd-swallows", QA,
     "Which function in this project silently discards invalid input instead of "
     "reporting it? Answer with just the function name.",
     [], 'words("build_store")', {}),
]

SUITES = {"easy": (FILES, TASKS), "hard": (HARD_FILES, HARD_TASKS)}

# The brutal suite lives in its own module: its fixtures and its verifiers both
# need triple-quoted strings, which do not nest readably inside this file.
from bench_brutal_suite import FILES as BRUTAL_FILES, TASKS as BRUTAL_TASKS  # noqa: E402

SUITES["brutal"] = (BRUTAL_FILES, BRUTAL_TASKS)


# ── Backends ─────────────────────────────────────────────────────────────────

def _child_env() -> dict[str, str]:
    """Environment for a nested CLI: router enforcement off, keys scrubbed.

    A nested `claude`/`codex` would otherwise hit this machine's own
    UserPromptSubmit/PreToolUse routing hooks and be intercepted, which would
    measure the hook, not the model.
    """
    env = dict(os.environ)
    # Only LLM_ROUTER_ENFORCE does anything here. An earlier version of this
    # function also set LLM_ROUTER_DISABLE and two upstream-branded variables;
    # none of the three is read anywhere in this tree — `grep -rn
    # LLM_ROUTER_DISABLE src/` returns only unrelated names — so they gave a
    # false sense of having isolated the subprocess while the hook ran anyway.
    # The upstream-branded pair also failed this repo's identity gate, which is
    # how they were noticed.
    env.update({
        "LLM_ROUTER_ENFORCE": "off",
        "LLM_ROUTER_API_KEY": env.get("LLM_ROUTER_API_KEY", "unused-placeholder"),
    })
    return env


def run_local(prompt: str, sandbox: Path, model: str, timeout: int) -> tuple[str | None, str | None]:
    # The loop defaults to LLM_ROUTER_AGENT_WRITES=propose: it computes the diff
    # and writes nothing. That is the right production default, but against a
    # Claude/Codex CLI running with write permission it would score every edit
    # task as a quality failure when it is really a permission difference. The
    # sandbox is disposable, so the local loop gets the same permission the CLIs
    # got. (That the default is propose is itself a result — see the writeup.)
    os.environ.setdefault("LLM_ROUTER_AGENT_WRITES", "apply")
    os.environ.setdefault("LLM_ROUTER_AGENT_COMMANDS", "all")
    from llm_router.hooks.agent_loop import run_agent_loop
    try:
        out = run_agent_loop(prompt=prompt, model=model, project_root=sandbox,
                             timeout_per_call=90, deadline_s=timeout)
        return out, None
    except Exception as e:  # noqa: BLE001 — a crash is a result, not an abort
        return None, f"{type(e).__name__}: {e}"


# A provider that refuses to answer is not a provider that answered badly. These
# strings mean "no model ran" and must abort the run, not score as 25 failures.
_REFUSALS = (
    "spend limit", "usage limit", "rate limit", "quota",
    "please run /login", "not authenticated", "invalid api key",
    "credit balance is too low",
    "i need your approval", "permission prompt", "requires approval",
)


class BackendUnavailable(RuntimeError):
    """The backend never reached a model — scoring its output would be a lie."""


def _check_usable(text: str) -> None:
    low = (text or "").lower()
    if any(m in low for m in _REFUSALS) and len(low) < 400:
        raise BackendUnavailable(text.strip()[:200])


def _cli(args: list[str], sandbox: Path, timeout: int) -> tuple[str | None, str | None]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, cwd=str(sandbox),
                           env=_child_env(), stdin=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout}s"
    out = (r.stdout or "").strip()
    if not out:
        return None, f"exit {r.returncode}: {(r.stderr or '')[-300:]}"
    _check_usable(out)
    return out, None


def run_claude(prompt: str, sandbox: Path, model: str, timeout: int):
    return _cli([
        "claude", "-p", prompt,
        "--output-format", "text",
        "--model", model,
        # acceptEdits permits file edits but NOT shell commands, so Claude could
        # not run pytest and answered "I need your approval to run the test
        # command" — a permission difference that scores as a wrong answer.
        # Codex gets --sandbox workspace-write, which allows reads, edits and
        # commands inside the working dir; this is the matching grant, written
        # as an explicit allowlist rather than a blanket bypass.
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(python3 -m pytest:*)",
        "--add-dir", str(sandbox),
        "--settings", '{"hooks":{}}',
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    ], sandbox, timeout)


def run_codex(prompt: str, sandbox: Path, model: str, timeout: int):
    out, err = _cli([
        "codex", "exec", "--json", "-m", model,
        "-c", "model_provider=openai",
        "--color", "never", "--skip-git-repo-check",
        "--sandbox", "workspace-write",
        "-C", str(sandbox),
        prompt,
    ], sandbox, timeout)
    if out is None:
        return out, err
    # Final agent_message from the JSONL event stream is the answer.
    answer = ""
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text", "") or answer
    return (answer or out), err


def run_local_task(prompt: str, sandbox: Path, model: str, timeout: int):
    """The llm_local_task service — the same local model, but owning the task.

    Two conditions, chosen by BENCH_TASK_CHECK:

      unchecked  the service gets exactly the information the raw `local`
                 backend gets. The only variables are the larger budget and the
                 typed terminal status, so a difference is attributable to those.

      suite      the service is additionally told "the existing test suite must
                 still pass". That is a check a real caller could legitimately
                 supply — and it is NOT this benchmark's verifier. Handing the
                 model the verifier would tell it the trap the prompt
                 deliberately withholds, which is the premise of the whole
                 suite; the result would measure leakage, not capability.
    """
    import asyncio, json as _json
    from llm_router.tools.local_task import llm_local_task

    check = None
    if os.environ.get("BENCH_TASK_CHECK", "unchecked") == "suite":
        if (sandbox / "tests").is_dir():
            check = "python3 -m pytest tests -q"

    try:
        raw = asyncio.run(llm_local_task(
            objective=prompt, workdir=str(sandbox), acceptance_check=check,
            model=model, budget_s=float(timeout), apply_writes=True,
        ))
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"

    d = _json.loads(raw)
    # The service's own status is NOT the score — the suite's verifier is. What
    # the status buys is an honest label on the answer text handed back.
    report = d.get("report") or ""
    return f"[status={d.get('status')}] {report}", d.get("error")


BACKENDS = {
    "local_task": (run_local_task, os.environ.get("BENCH_LOCAL_MODEL", "qwen3-coder:30b"), 600),
    "local":  (run_local,  os.environ.get("BENCH_LOCAL_MODEL", "qwen3-coder:30b"), 300),
    "claude": (run_claude, os.environ.get("BENCH_CLAUDE_MODEL", "sonnet"), 300),
    "codex":  (run_codex,  os.environ.get("BENCH_CODEX_MODEL", "gpt-5.5"), 300),
}


# ── Scoring ──────────────────────────────────────────────────────────────────

def verify(sandbox: Path, verifier: str, answer: str | None, orig_test: Path,
           stale_test: Path | None = None) -> tuple[bool, str]:
    script = PREAMBLE + "\n" + verifier + "\nprint('VERIFIED')\n"
    env = dict(os.environ)
    env["BENCH_ANSWER"] = answer or ""
    env["BENCH_ORIG_TEST"] = str(orig_test)
    env["BENCH_STALE_TEST"] = str(stale_test or orig_test)
    env["PYTHONPATH"] = str(sandbox)
    try:
        r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, cwd=str(sandbox), env=env, timeout=240)
    except subprocess.TimeoutExpired:
        return False, "verifier timeout"
    if r.returncode == 0:
        return True, ""
    return False, ((r.stderr or r.stdout).strip().splitlines() or [""])[-1][:200]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=sorted(BACKENDS))
    ap.add_argument("--suite", choices=("easy", "hard", "brutal"), default="easy")
    ap.add_argument("--tasks", type=int, default=0, help="0 = all")
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--report", action="store_true", help="merge result files into a table")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.report:
        return report()
    if not args.backend:
        ap.error("--backend is required unless --report")

    files, tasks = SUITES[args.suite]
    fn, model, timeout = BACKENDS[args.backend]
    sandbox = Path(os.environ.get("BENCH_SANDBOX",
                                  f"/tmp/bq_{args.backend}")).resolve()
    orig_test = OUT_DIR / "orig_test_parser.py"
    orig_test.write_text(FILES["tests/test_parser.py"])
    stale_test = OUT_DIR / "orig_test_pipeline.py"
    stale_test.write_text(STALE_INDEX_TEST)

    selected = tasks[:args.tasks] if args.tasks else list(tasks)
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        selected = [t for t in tasks if t[0] in want]

    rows = []
    suffix = "" if args.suite == "easy" else f"-{args.suite}"
    res_path = OUT_DIR / f"{args.backend}{suffix}.json"
    for task in selected:
        tid, kind, prompt, allowed, verifier = task[:5]
        extra = task[5] if len(task) > 5 else {}
        build(sandbox, files)
        for rel, body in extra.items():
            q = sandbox / rel
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text(body)
        before = snapshot(sandbox)
        # monotonic, not time.time(): a macOS "Maintenance Sleep" during an
        # unattended run advances the wall clock and not the monotonic one, and
        # a task that took 12s of compute was recorded as 918.6s because the
        # Mac slept for 902s in the middle of it. Wall-clock durations here are
        # a measurement of the laptop's power management, not of the model.
        t0 = time.monotonic()
        try:
            answer, err = fn(prompt, sandbox, model, timeout)
        except BackendUnavailable as e:
            print(f"\nABORT at task {tid}: {args.backend} is not answering — {e}\n"
                  f"{len(rows)} task(s) completed before this point are in "
                  f"{res_path} and are valid; {tid} and everything after it were "
                  f"NOT scored. Re-run when the backend is available.",
                  file=sys.stderr)
            return 2
        dt = time.monotonic() - t0
        after = snapshot(sandbox)
        touched = changed_files(before, after)
        stray = [f for f in touched if f not in allowed]
        ok, why = verify(sandbox, verifier, answer, orig_test, stale_test)
        rows.append({
            "task": tid, "kind": kind, "suite": args.suite,
            "backend": args.backend, "model": model,
            "correct": ok, "clean": not stray, "stray_files": stray,
            "touched": touched, "seconds": round(dt, 1),
            "error": err, "why_failed": why,
            "answer": (answer or "")[:400],
        })
        flag = "PASS" if ok else "FAIL"
        dirty = f"  +{len(stray)} stray" if stray else ""
        print(f"[{args.backend:6s}] {tid:18s} {flag}  {dt:6.1f}s{dirty}"
              f"{'  ' + (err or why) if (err or why) else ''}", flush=True)
        res_path.write_text(json.dumps(rows, indent=2))

    n = len(rows)
    print(f"\n{args.backend} ({model}): {sum(r['correct'] for r in rows)}/{n} correct, "
          f"{sum(r['clean'] for r in rows)}/{n} clean, "
          f"{sum(r['seconds'] for r in rows):.0f}s total")
    return 0


def report() -> int:
    data: dict[str, list[dict]] = {}
    want = os.environ.get("BENCH_REPORT_SUITE", "easy")
    for p in sorted(OUT_DIR.glob("*.json")):
        stem = p.stem
        backend, _, tag = stem.partition("-")
        suite = tag or "easy"
        if backend in BACKENDS and suite == want:
            data[backend] = json.loads(p.read_text())
    if not data:
        print("no results in", OUT_DIR)
        return 1

    order = [b for b in ("local", "codex", "claude") if b in data]
    print("\n## Correctness by backend\n")
    head = f"| {'task':18s} | kind |" + "".join(f" {b:8s} |" for b in order)
    print(head)
    print("|" + "-" * 20 + "|------|" + "".join("-" * 10 + "|" for _ in order))
    ids = [t[0] for t in SUITES[want][1]]
    for tid in ids:
        cells = []
        kind = ""
        for b in order:
            row = next((r for r in data[b] if r["task"] == tid), None)
            if row is None:
                cells.append(f" {'—':8s} |")
                continue
            kind = row["kind"]
            mark = "ok" if row["correct"] else "FAIL"
            if row["correct"] and not row["clean"]:
                mark = "ok*"
            cells.append(f" {mark:8s} |")
        if kind:
            print(f"| {tid:18s} | {kind:4s} |" + "".join(cells))
    print("\n`ok*` = correct answer, but files outside the task's blast radius changed.\n")

    print("## Totals\n")
    print("| backend | model | correct | qa | edit | clean | median s |")
    print("|---------|-------|---------|----|------|-------|----------|")
    for b in order:
        rows = data[b]
        qa = [r for r in rows if r["kind"] == "qa"]
        ed = [r for r in rows if r["kind"] == "edit"]
        secs = sorted(r["seconds"] for r in rows)
        med = secs[len(secs) // 2] if secs else 0
        print(f"| {b} | {rows[0]['model']} | "
              f"{sum(r['correct'] for r in rows)}/{len(rows)} | "
              f"{sum(r['correct'] for r in qa)}/{len(qa)} | "
              f"{sum(r['correct'] for r in ed)}/{len(ed)} | "
              f"{sum(r['clean'] for r in rows)}/{len(rows)} | {med:.0f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
