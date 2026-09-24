"""Ratchet for A31: every SUM over savings_stats.estimated_claude_cost_saved in
src/ goes through savings.VERIFIED_SAVED_SQL / UNVERIFIED_SAVED_SQL.

Kept apart from test_a31_router_savings_unverified.py because a source-scanning
helper makes its whole file ineligible for mutation testing (see
scripts/gf_excluded_tests.py); the behavioural A31 tests must stay eligible.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


# ── Ratchet: a seventh query site cannot skip the predicate ───────────────

def _raw_savings_sums(root: pathlib.Path) -> list[str]:
    needle = "sum(estimated_claude_cost_saved"
    hits = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "savings.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and needle in node.value.lower().replace(" ", "")):
                hits.append(f"{path.relative_to(root)}:{node.lineno}")
    return hits


def test_the_ratchet_detects_a_raw_sum(tmp_path):
    """Check the check on a synthetic positive — a zero from a broken scan and
    a zero from a clean tree are the same output."""
    (tmp_path / "bad.py").write_text(
        'q = "SELECT COALESCE(SUM(estimated_claude_cost_saved),0) FROM savings_stats"\n')
    (tmp_path / "ok.py").write_text('# SUM(estimated_claude_cost_saved) in a comment\n')
    assert _raw_savings_sums(tmp_path) == ["bad.py:1"]


def test_no_query_site_sums_the_savings_column_raw():
    hits = _raw_savings_sums(SRC)
    assert not hits, (
        "raw SUM over savings_stats.estimated_claude_cost_saved — use "
        f"savings.VERIFIED_SAVED_SQL / UNVERIFIED_SAVED_SQL: {hits}")
