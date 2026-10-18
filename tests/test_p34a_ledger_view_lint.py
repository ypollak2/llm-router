"""P3.4-A (D-194): only ``ledger_view`` reads the ``usage`` and ``routing_decisions`` tables on
the rewired surfaces.

Evidence: the P3.4 survey found twenty surfaces each owning a private ``FROM usage`` / ``FROM
routing_decisions`` with its own window, provenance filter and row exclusions; that is how one
question got several answers. This lint keeps the three rewired surfaces and their delegates from
growing a private read again. SCOPE (part A): ``dashboard_data.query_window`` / ``summary``,
``routing_report.executed_routes``, ``cost.get_savings_by_period``, all of
``observability/summary.py`` and ``commands/kpi.py``. B2 adds ``statusline_segments.py``; the
status-bar and session-end hooks join in B3/B4.
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
)


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
    # the hook surfaces are part B: not flagged yet
    assert not {s.id for s in SURFACES if s.id.startswith("hook_") and s.ledger_view}


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
