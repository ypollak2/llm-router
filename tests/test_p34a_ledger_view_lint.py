"""P3.4-A (D-194): only ``ledger_view`` reads the ``usage`` and ``routing_decisions`` tables on
the rewired surfaces.

Evidence: the P3.4 survey found twenty surfaces each owning a private ``FROM usage`` / ``FROM
routing_decisions`` with its own window, provenance filter and row exclusions; that is how one
question got several answers. This lint keeps the three rewired surfaces and their delegates from
growing a private read again. SCOPE (part A): ``dashboard_data.query_window`` / ``summary``,
``routing_report.executed_routes``, ``cost.get_savings_by_period``, all of
``observability/summary.py`` and ``commands/kpi.py``. B2 adds ``statusline_segments.py``, B3 the status-bar hook;
B5 adds the session-end functions B4 migrated; B4b moves the rest of the file, so all of
``hooks/session-end.py`` is in scope. ``SESSION_END_B4B`` is the (empty) ratchet of private reads that
may remain there, as a per-function COUNT: a new read inside a pinned function fails it too (F95).
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"

_TABLE_READ = re.compile(r"\bFROM\s+(usage|routing_decisions)\b", re.I)
_TABLE_NAME_REF = {"_LEGACY_TABLE"}

#: (file, function or None for the whole file)
SCOPE = (
    ("dashboard_data.py", "query_window"),
    ("dashboard_data.py", "summary"),
    ("routing_report.py", "executed_routes"),
    ("cost.py", "get_savings_by_period"),
    ("observability/summary.py", None),
    ("commands/kpi.py", None),
    ("statusline_segments.py", None),          # B2: the statusline's mix and money (D-218)
    ("hooks/status-bar.py", None),             # B3: the status-bar's D:/W:/M: and session calls
    ("hooks/session-end.py", None),            # B4/B5/B4b: every figure the Stop and its child print
)

#: Session-end functions allowed a private ``usage`` / ``routing_decisions`` read, with how many
#: reads each may hold. P3.4-B4b moved every one onto ``ledger_view``: nothing is kept. Adding an
#: entry is refused by review; a pinned function growing a second read fails the count (F95).
SESSION_END_B4B: dict[str, int] = {}


def _violations(source: str, function: str | None) -> list[str]:
    tree = ast.parse(source)
    nodes = [tree]
    if function is not None:
        nodes = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function]
        assert nodes, f"function {function} not found: the lint scope is stale"
    found: list[str] = []
    for root in nodes:
        for n in ast.walk(root):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _TABLE_READ.search(n.value):
                found.append(f"line {n.lineno}: {n.value.strip()[:70]!r}")
            if isinstance(n, ast.Name) and n.id in _TABLE_NAME_REF:
                found.append(f"line {n.lineno}: {n.id}")
    return found


def test_no_surface_module_reads_the_ledger_tables_outside_ledger_view():
    bad = {}
    for rel, fn in SCOPE:
        v = _violations((SRC / rel).read_text(encoding="utf-8"), fn)
        if v:
            bad[f"{rel}:{fn or '*'}"] = v
    assert not bad, (
        "private reads of usage / routing_decisions on a ledger_view surface; call "
        f"ledger_view instead: {bad}")


def _private_read_counts(source: str) -> dict[str | None, int]:
    """Private table reads per innermost function (None = module level)."""
    found: dict[str | None, int] = {}

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.Constant) and isinstance(child.value, str) \
                    and _TABLE_READ.search(child.value):
                found[inner] = found.get(inner, 0) + len(_TABLE_READ.findall(child.value))
            if isinstance(child, ast.Name) and child.id in _TABLE_NAME_REF:
                found[inner] = found.get(inner, 0) + 1
            walk(child, inner)

    walk(ast.parse(source), None)
    return found


def test_session_end_private_reads_are_exactly_the_pinned_counts():
    """Per function, not per name: a new read inside a pinned function is refused (F95)."""
    found = _private_read_counts((SRC / "hooks" / "session-end.py").read_text(encoding="utf-8"))
    assert found == SESSION_END_B4B, (
        f"private reads differ from the pinned counts: found {found}, pinned {SESSION_END_B4B}")


def test_the_count_pin_sees_a_second_read_in_a_pinned_function():
    """Anti-vacuity for the ratchet: the old name-set check passed a second read in the same function."""
    one = "def f():\n    return 'SELECT 1 FROM usage'\n"
    two = "def f():\n    a = 'SELECT 1 FROM usage'\n    return a + ' JOIN (SELECT 2 FROM routing_decisions)'\n"
    assert _private_read_counts(one) == {"f": 1}
    assert _private_read_counts(two) == {"f": 2}
    assert _private_read_counts("X = 'SELECT 1 FROM usage'\n") == {None: 1}
    assert _private_read_counts("def f():\n    return _LEGACY_TABLE\n") == {"f": 1}
    assert _private_read_counts("def f():\n    return 1\n") == {}


def test_the_lint_finds_a_violation_and_the_one_place_that_may_read():
    """Anti-vacuity: an empty scan passes everything."""
    assert _violations("def f():\n    return 'SELECT 1 FROM usage WHERE 1'\n", "f")
    assert _violations("def f():\n    return f'SELECT 1 FROM routing_decisions WHERE {w}'\n", "f")
    assert _violations("def f():\n    return _LEGACY_TABLE\n", "f")
    lv = (SRC / "ledger_view.py").read_text(encoding="utf-8")
    assert _violations(lv, None), "ledger_view should be the module that reads the tables"


def test_every_scoped_delegate_calls_ledger_view():
    for rel, fn in SCOPE:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        nodes = [tree] if fn is None else [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn]
        names = {a.name for r in nodes for n in ast.walk(r)
                 if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        names |= {n.id for r in nodes for n in ast.walk(r) if isinstance(n, ast.Name)}
        names |= {n.attr for r in nodes for n in ast.walk(r) if isinstance(n, ast.Attribute)}
        assert "ledger_view" in names or "lv" in names or "_lv" in names, f"{rel}:{fn} does not use ledger_view"


def test_ledger_view_imports_no_pydantic():
    """Stdlib-only, so a hook may import it (part B)."""
    code = ("import sys; import llm_router.ledger_view; "
            "bad = [m for m in sys.modules if m.split('.')[0] == 'pydantic']; "
            "print('PYDANTIC' if bad else 'clean')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "clean", out.stdout + out.stderr


def test_provider_class_copies_equal_their_sources():
    """ledger_view keeps stdlib copies (those modules are not stdlib-only); equality is pinned here."""
    from llm_router import ledger_view, qa_policy, types

    assert ledger_view.LOCAL_PROVIDERS == types.LOCAL_PROVIDERS
    assert ledger_view.LOCAL_BY_CAUSE_PROVIDERS == qa_policy.QA_STRIP_PROVIDERS
    # D-205: subscription providers are discovered (seats), not listed in ledger_view.
    assert not hasattr(ledger_view, "SUBSCRIPTION_PROVIDERS")


def test_surfaces_flagged_for_ledger_view():
    from llm_router.savings import SURFACES

    flagged = {s.id for s in SURFACES if s.ledger_view}
    assert {"cli_status_premium", "terminal_session_summary", "cli_kpi_o1",
            "mcp_llm_savings"} <= flagged
    # part B: the hooks whose figures come from ledger_view (B2 statusline, B3 status-bar, B4
    # session-end Stop, B4b the session-end headline's rows).
    assert {"statusline_segments", "hook_status_bar", "hook_session_end_cumulative",
            "hook_session_end_headline"} <= flagged


def test_flagged_hook_surfaces_point_at_a_named_function_that_exists():
    """rv-618: ``session-end.py:985`` was off by two lines within a day. A flagged hook surface
    names its function (``file:function``) and the function must exist in that file."""
    from llm_router.savings import SURFACES

    named = {"hook_session_end_headline": "_net_session_line",
             "hook_session_end_cumulative": "_compute_ledger_aggregates",
             "statusline_segments": "_ledger"}
    by_id = {s.id: s for s in SURFACES}
    for sid, fn in named.items():
        rel, _, ref = by_id[sid].where.partition(":")
        assert ref == fn, f"{sid}: {by_id[sid].where!r} should name {fn}"
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        defs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert fn in defs, f"{rel} has no function {fn}"


def test_every_surface_ref_names_a_function_that_exists():
    """F95: a line number drifts (rv-618: two lines in a day). EVERY ``SURFACES`` entry is
    ``file:function`` and the function is defined in that file; a numeric ref fails."""
    from llm_router.savings import SURFACES

    assert len(SURFACES) >= 20                        # an empty registry would pass the loop
    for s in SURFACES:
        rel, _, ref = s.where.partition(":")
        assert ref.isidentifier(), f"{s.id}: {s.where!r} must be file:function, not a line number"
        path = SRC / rel
        assert path.is_file(), f"{s.id}: no file {rel}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert ref in defs, f"{s.id}: {rel} has no function {ref}"


def test_named_windows_table():
    from llm_router import ledger_view as lv

    assert lv.WINDOWS == ("session", "today", "6h", "24h", "7d", "14d", "month", "lifetime")
    assert lv.resolve("week").name == "7d" and lv.resolve("day").name == "today"
    try:
        lv.resolve("fortnight")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown window must raise, never fall back")
