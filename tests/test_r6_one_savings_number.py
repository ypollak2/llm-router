"""R6/R7 — twenty surfaces, one database, twenty different answers.

`dashboard_data.py` records the consequence in its own source: three hand-rolled
savings queries once reported **$73.97, $102.31 and $205.19 for the same day**.
The lesson was written down. A fourth query was added afterwards.

The 2026-09-22 audit found twenty user-facing savings surfaces diverging for
four independent reasons:

    PROVENANCE   12 of 20 query SQL directly with no `production_only(...)`.
                 `dashboard_data.py` — which feeds `llm-router status` and
                 session-end's 14-day panel — contains ZERO references to
                 `production_only`, `is_simulated` or `provenance`.
    BASELINE     Opus / Sonnet / a hardcoded per-model multiplier table.
    "NET"        Net of ROUTING OVERHEAD, or net of ACTUAL SPEND. Both printed
                 under the word "net".
    MEMBERSHIP   savings_stats only / routing_decisions only / usage only / a
                 union of four. A free DIRECT route in one is invisible to the
                 surfaces reading the others.

Two things nobody had noticed:

* `cost.get_realized_savings` is the only accessor that is BOTH provenance-
  filtered and a true net. It reaches exactly ONE surface.
* `dashboard_data.query_realized_savings` — the only accessor reading the
  execution ledger, carrying INV-COST-004 and a docstring explaining why it
  must never become a fourth independent calculation — has NO CALLER AT ALL.
  The same CLASS-A shape as the 58 fail-open writers with zero readers.

This file does not pretend the migration is finished. It makes the population
visible and bounded, which is the thing that was missing while it grew from
three to twenty: every surface is named with its exact divergence, every
MIGRATED surface must agree to the cent, and a twenty-first cannot be added
without joining the list.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from llm_router.savings import (
    SURFACES,
    CanonicalSavings,
    canonical_savings,
    label_money,
    net_saved,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


# ── The registry is honest about itself ───────────────────────────────────

def test_every_unmigrated_surface_states_its_divergence():
    """A surface listed as not-canonical with no stated reason is one nobody
    has actually looked at, and the registry would then be a list of names
    rather than a record of what is wrong."""
    vague = [
        s.id for s in SURFACES
        if not s.canonical and len(s.divergence) < 30
    ]
    assert not vague, f"unmigrated surface(s) with no real divergence note: {vague}"


def test_the_registry_has_not_silently_emptied():
    """Anti-vacuity. Every assertion here is trivially satisfied by an empty
    registry, and an empty registry is exactly what a careless refactor leaves
    behind."""
    assert len(SURFACES) >= 20, (
        f"the surface registry holds {len(SURFACES)} entries; the audit found "
        "20. Surfaces are not supposed to disappear — if one was deleted, "
        "delete its entry deliberately and lower this number in the same "
        "commit."
    )
    assert any(s.canonical for s in SURFACES), "no surface reads the canonical figure"


def test_surface_ids_are_unique():
    ids = [s.id for s in SURFACES]
    assert len(ids) == len(set(ids)), "duplicate surface id"


def test_every_registered_surface_points_at_a_file_that_exists():
    missing = []
    for s in SURFACES:
        path = SRC / "llm_router" / s.where.split(":", 1)[0]
        if not path.exists():
            missing.append(f"{s.id} -> {s.where}")
    assert not missing, (
        f"registry entries pointing at files that are gone: {missing}. A "
        "registry of things that are not there is worse than no registry."
    )


# ── Discovery: a new savings surface must join the registry ───────────────

#: Modules allowed to compute a savings figure without being a SURFACE: the
#: accessors themselves, and the module that defines the canonical one.
_ACCESSOR_MODULES = {
    "llm_router/cost.py",
    "llm_router/savings.py",
    "llm_router/dashboard_data.py",
    "llm_router/execution_ledger.py",
    "llm_router/quota_savings.py",
    "llm_router/tiers.py",
}


def _files_summing_savings() -> set[str]:
    """Files containing a SQL SUM over a savings column.

    Deliberately narrow: `SUM(cost_saved_usd)` / `SUM(saved_usd)` and friends
    inside a string literal. It is the shape that produces an independent
    money figure, and it is what every one of the twelve unfiltered surfaces
    does. A broader scan (any mention of the word "saved") would fire on
    hundreds of sites, get allowlisted wholesale, and the allowlist would be
    the blind spot — the same reasoning that narrowed R11's provider scan and
    R4's subprocess scan.
    """
    wanted = ("sum(cost_saved_usd", "sum(saved_usd",
              "sum(estimated_claude_cost_saved")
    # A31: savings_stats sums now go through savings.VERIFIED_SAVED_SQL /
    # UNVERIFIED_SAVED_SQL, so the literal needle is gone from those files. A
    # reference to either constant is the same shape — an independent SUM over
    # the savings column — and must stay visible to this scan.
    predicate_names = {"VERIFIED_SAVED_SQL", "UNVERIFIED_SAVED_SQL",
                       "savings_split_sql"}
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                low = node.value.lower().replace(" ", "")
                if any(w in low for w in wanted):
                    found.add(rel)
                    break
            if isinstance(node, ast.Name) and node.id in predicate_names:
                found.add(rel)
                break
    return found


def test_the_savings_sql_scan_still_finds_the_known_sites():
    """The check on the check. A scan matching nothing passes everything."""
    found = _files_summing_savings()
    assert len(found) >= 5, (
        f"the scan found only {len(found)} file(s) summing a savings column. "
        "It has stopped matching the source tree and the discovery test below "
        "is no longer protecting anything."
    )


def test_a_file_computing_its_own_savings_figure_is_registered():
    registered_files = {s.where.split(":", 1)[0] for s in SURFACES}
    unaccounted = sorted(
        f for f in _files_summing_savings()
        if f.replace("llm_router/", "") not in registered_files
        and f not in _ACCESSOR_MODULES
    )
    assert not unaccounted, (
        "file(s) computing their own savings figure with no entry in "
        "savings.SURFACES:\n  " + "\n  ".join(unaccounted)
        + "\n\nThis is how three surfaces became twenty. Either call "
        "`savings.canonical_savings()`, or add the file to SURFACES with the "
        "exact way its number differs."
    )


# ── The canonical figure itself ───────────────────────────────────────────

def _seeded(monkeypatch, tmp_path, *, rows, subscription=False):
    """One database, one moment. Every surface must agree on it."""
    import sqlite3

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    if subscription:
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    else:
        monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)

    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE claude_usage (timestamp TEXT, cost_saved_usd REAL, "
        "routing_overhead_usd REAL, is_simulated INTEGER)"
    )
    for saved, overhead, simulated in rows:
        conn.execute(
            "INSERT INTO claude_usage VALUES (datetime('now'), ?, ?, ?)",
            (saved, overhead, simulated),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    return db


def test_the_canonical_figure_excludes_simulated_rows(monkeypatch, tmp_path):
    """The defect that separates the 12 unfiltered surfaces from the 8 filtered
    ones: one benchmark row inflates exactly the unfiltered set."""
    _seeded(monkeypatch, tmp_path, rows=[
        (1.00, 0.10, 0),      # production
        (2.00, 0.20, 0),      # production
        (999.00, 0.00, 1),    # a benchmark run
    ])
    s = asyncio.run(canonical_savings(period="all", platform="claude"))
    assert s.baseline_equivalent_avoided_usd == pytest.approx(3.00), (
        f"the synthetic $999 row reached the figure: "
        f"{s.baseline_equivalent_avoided_usd}"
    )
    assert s.n_rows == 2, f"n_rows counted the simulated row: {s.n_rows}"
    assert s.provenance_filtered is True


def test_net_is_net_of_routing_overhead(monkeypatch, tmp_path):
    """The distinction two surfaces both call 'net'."""
    _seeded(monkeypatch, tmp_path, rows=[(10.00, 2.50, 0)])
    s = asyncio.run(canonical_savings(period="all", platform="claude"))
    assert s.baseline_equivalent_avoided_usd == pytest.approx(10.00)
    assert s.routing_overhead_usd == pytest.approx(2.50)
    assert s.net_avoided_usd == pytest.approx(7.50)


def test_a_net_loss_is_not_clamped(monkeypatch, tmp_path):
    """AUD-06's invariant, on the new path.

    Routing that cost more than it saved must render as a loss. The clamp is
    precisely what stopped users finding out.
    """
    _seeded(monkeypatch, tmp_path, rows=[(1.00, 4.00, 0)])
    s = asyncio.run(canonical_savings(period="all", platform="claude"))
    assert s.net_avoided_usd == pytest.approx(-3.00), s.net_avoided_usd
    assert s.real_dollars_avoided_usd == pytest.approx(-3.00)


def test_under_a_subscription_the_real_dollars_are_zero(monkeypatch, tmp_path):
    """R7. Printing '$47.20 saved' to a subscriber is a different claim, not a
    rounding error — the plan is paid either way. The gate existed on one of
    four surfaces."""
    _seeded(monkeypatch, tmp_path, rows=[(47.20, 0.00, 0)], subscription=True)
    s = asyncio.run(canonical_savings(period="all", platform="claude"))
    assert s.under_subscription is True
    assert s.real_dollars_avoided_usd == 0.0
    # The baseline-equivalent figure is still available and still labelled.
    assert s.baseline_equivalent_avoided_usd == pytest.approx(47.20)
    head = s.headline()
    assert "$0.00 real dollars avoided" in head
    assert "subscription" in head


def test_an_empty_window_reports_its_denominator(monkeypatch, tmp_path):
    """A savings total over zero rows is not a $0.00 saving."""
    _seeded(monkeypatch, tmp_path, rows=[])
    s = asyncio.run(canonical_savings(period="all", platform="claude"))
    assert s.n_rows == 0
    assert "n=0" in s.headline()


# ── R7: no bare money strings ─────────────────────────────────────────────

def test_every_rendered_figure_carries_its_qualifier():
    s = CanonicalSavings(
        window="today", baseline_equivalent_avoided_usd=5.0,
        routing_overhead_usd=1.0, real_dollars_avoided_usd=4.0,
        baseline_model="claude-opus-4", n_rows=12,
        provenance_filtered=True, under_subscription=False, source="test",
    )
    for rendered in (s.headline(), label_money(4.0, s)):
        assert "claude-opus-4" in rendered, f"no baseline named: {rendered}"
        assert "n=12" in rendered, f"no denominator: {rendered}"
        assert "avoided" in rendered, f"no qualifier: {rendered}"


def test_the_subtraction_is_delegated_not_reimplemented():
    """AST on the property, not a substring.

    `net_saved` exists so the subtraction cannot be clamped in one place while
    staying signed in another. A `CanonicalSavings` that did its own arithmetic
    would be the twelfth surface, inside the module built to stop there being
    a twelfth surface.
    """
    src = SRC / "llm_router" / "savings.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "net_avoided_usd"
    )
    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "net_saved" in calls, (
        "CanonicalSavings.net_avoided_usd no longer delegates to net_saved()"
    )
    assert net_saved(1.0, 4.0) == -3.0, "net_saved itself started clamping"
