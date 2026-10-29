"""W1-1 / UN-05: the privacy wording in SECURITY.md names exactly the doors the UN-04 matrix proves.

The door table in SECURITY.md (between the ``door-table`` markers) is compared, row for row, with the rows derived from
``tests/test_w11_secret_door_matrix.py`` (``MATRIX``, ``SINK_FUNCS``, ``SINK_FILES``). A door that gains, loses or changes
class in the matrix fails here until the document says so. ``engine.guard()`` is documented as not wired; a call to it
from ``src`` fails here until that wording is revised.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from tests import test_w11_secret_door_matrix as m

ROOT = Path(__file__).resolve().parents[1]
SECURITY = ROOT / "SECURITY.md"
START, END = "<!-- door-table:start -->", "<!-- door-table:end -->"

ENFORCED_ROUTER = "enforced: router door"
ENFORCED_OWN = "enforced: own check"
LOCAL = "local by construction"
NOT_UNREACHABLE = "NOT enforced: unreachable"
NOT_LEAK = "NOT enforced"
NOT_OPTIN = "NOT enforced: opt-in"
NOT_PROXY = "NOT enforced: log-only"


def derive_rows(matrix=None, funcs=None, files=None) -> dict[str, str]:
    """{door key: status}. Router-door sites are one row per file; every other classified site is one row per
    ``file::function``. Probes, fixed prompts, primitives and the Ollama-local sinks carry no secret prompt off the
    machine (the matrix says why) and are not doors."""
    matrix = m.MATRIX if matrix is None else matrix
    funcs = m.SINK_FUNCS if funcs is None else funcs
    files = m.SINK_FILES if files is None else files
    rows: dict[str, str] = {}
    for (f, fn, _callee), (_n, cls, _why) in matrix.items():
        if cls in (m.ROUTER, m.PROVIDER):
            rows[f] = ENFORCED_ROUTER
        elif cls == m.OWN:
            rows[f"{f}::{fn}"] = ENFORCED_OWN
        elif cls == m.LOCAL_ONLY:
            rows[f"{f}::{fn}"] = LOCAL
        elif cls == m.DEAD:
            rows[f"{f}::{fn}"] = NOT_UNREACHABLE
    by_class = {"guarded": ENFORCED_OWN, "LEAK": NOT_LEAK}
    for (f, fn, _callee), (_n, cls, _why) in funcs.items():
        if cls in by_class and fn != "<module>":
            rows[f"{f}::{fn}"] = by_class[cls]
    for f, (_n, cls, _why) in files.items():
        if cls == "opt-in":
            rows[f] = NOT_OPTIN
        elif cls == "proxy":
            rows[f] = NOT_PROXY
    return rows


def doc_rows(text: str | None = None) -> dict[str, str]:
    text = SECURITY.read_text(encoding="utf-8") if text is None else text
    assert START in text and END in text, "SECURITY.md has no door table between the door-table markers"
    block = text.split(START, 1)[1].split(END, 1)[0]
    rows: dict[str, str] = {}
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        mt = re.fullmatch(r"`([^`]+)`", cells[0]) if len(cells) >= 2 else None
        if mt:
            assert mt.group(1) not in rows, f"door {mt.group(1)} listed twice"
            rows[mt.group(1)] = cells[1]
    return rows


def test_security_door_table_matches_the_matrix():
    derived, documented = derive_rows(), doc_rows()
    assert len(derived) >= 30, "the matrix yielded too few doors: an empty derivation passes everything"
    assert {v for v in derived.values()} == {ENFORCED_ROUTER, ENFORCED_OWN, LOCAL, NOT_UNREACHABLE, NOT_LEAK,
                                              NOT_OPTIN, NOT_PROXY}, "every status must occur at least once"
    assert sorted(set(derived) - set(documented)) == [], "doors in the matrix missing from SECURITY.md"
    assert sorted(set(documented) - set(derived)) == [], "SECURITY.md names doors the matrix does not have"
    assert {k: (derived[k], documented[k]) for k in derived if derived[k] != documented[k]} == {}


def test_a_changed_matrix_is_caught():
    """Negative controls: a new leak, a fixed leak and a removed door each make the comparison differ."""
    documented = doc_rows()
    funcs = dict(m.SINK_FUNCS)
    funcs[("hooks/new-door.py", "send", "urlopen")] = (1, "LEAK", "x")
    assert derive_rows(funcs=funcs) != documented
    leak = next(k for k, v in m.SINK_FUNCS.items() if v[1] == "LEAK" and k[1] != "<module>")
    funcs = dict(m.SINK_FUNCS)
    funcs[leak] = (1, "guarded", "x")
    assert derive_rows(funcs=funcs) != documented
    matrix = {k: v for k, v in m.MATRIX.items() if k[0] != "tools/media.py"}
    assert derive_rows(matrix=matrix) != documented


def guard_calls(source: str) -> list[int]:
    """Line numbers of calls to ``llm_router.engine.guard`` in ``source``, resolving import aliases per module:
    ``import llm_router.engine as e``, ``from llm_router import engine as x``, ``from llm_router.engine import guard
    as g`` and the plain forms. Other ``.guard`` (``_writes.guard``) is not matched."""
    tree = ast.parse(source)
    modules: set[str] = set()   # names bound to the engine module
    funcs: set[str] = set()     # names bound to engine.guard itself
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "llm_router.engine" and a.asname:
                    modules.add(a.asname)
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            for a in n.names:
                if a.name == "engine" and (mod == "llm_router" or (n.level and mod == "")):
                    modules.add(a.asname or a.name)
                elif a.name == "guard" and (mod == "llm_router.engine" or (n.level and mod == "engine")):
                    funcs.add(a.asname or a.name)
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id in funcs:
            hits.append(n.lineno)
        elif isinstance(f, ast.Attribute) and f.attr == "guard":
            v = f.value
            if isinstance(v, ast.Name) and v.id in modules:
                hits.append(n.lineno)
            elif isinstance(v, ast.Attribute) and v.attr == "engine" and isinstance(v.value, ast.Name) \
                    and v.value.id == "llm_router":
                hits.append(n.lineno)   # llm_router.engine.guard() after `import llm_router.engine`
    return hits


def test_engine_guard_is_not_wired_into_src():
    """SECURITY.md says ``engine.guard()`` is a test seam, not a door. If any code under src calls it, that sentence is
    wrong: wire it into the matrix and the table instead."""
    callers = [f"{p.relative_to(m.SRC)}:{ln}" for p in sorted(m.SRC.rglob("*.py"))
               for ln in guard_calls(p.read_text())]
    assert callers == [], callers
    assert "engine.guard()" in SECURITY.read_text(encoding="utf-8")


def test_guard_scan_sees_every_import_alias_form():
    """Negative controls: each way of reaching ``engine.guard`` is detected; an unrelated ``.guard`` is not."""
    forms = [
        "import llm_router.engine as e\ne.guard('x')\n",
        "from llm_router.engine import guard as g\ng('x')\n",
        "from llm_router import engine as x\nx.guard('x')\n",
        "from llm_router import engine\nengine.guard('x')\n",
        "from llm_router.engine import guard\nguard('x')\n",
        "import llm_router.engine\nllm_router.engine.guard('x')\n",
        "from . import engine\nengine.guard('x')\n",
        "from .engine import guard as g\ng('x')\n",
    ]
    for src in forms:
        assert guard_calls(src), src
    for src in ["from llm_router.hooks import agent_writes as _writes\n_writes.guard(1)\n",
                "def guard(): pass\nguard()\n",
                "import llm_router.engine as e\ne.decide('x')\n"]:
        assert guard_calls(src) == [], src


def test_readme_privacy_paragraph_points_at_the_door_table():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "SECURITY.md#data-handling--storage-mapping" in readme
