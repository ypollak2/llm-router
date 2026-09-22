"""C-02 — reported savings had the wrong sign once fixtures were removed.

Measured by the 2026-09-21 audit over the live `usage.db`:

    raw, as the code reported it                       +$83.49
    after `_is_test_model()` -- the code's own filter   +$87.96
    after removing stub-signature / fixture rows        **-$1.15**

Three things are wrong there, and only one is "the number is off".

**The filter is anti-protective.** Applying the project's own test-model filter
moved the figure $4.47 FURTHER from the truth. It matches on model *name*, and
1,813 fixture rows carry real model names, so no name-based rule can ever see
them.

**`is_simulated` was decorative.** The column was declared (ALTER TABLE), and
filtered on (`get_savings_by_period`: `AND is_simulated IS NOT 1`), and never
written -- the INSERT omitted it. A filter that excludes nothing, ever, while
reading as protective.

**There were two writers, not one.** The audit said "the single INSERT INTO
usage". `hooks/cc-usage-track.py` also inserts, and also omitted the column, so a
benchmark run through the Claude Code tracker was recorded as production spend.

The fix is provenance stamped at WRITE time by the same `detect_synthetic()` the
routing ledger uses. A name heuristic applied at read time cannot be made
correct; this can.
"""

from __future__ import annotations

import pathlib


REPO = pathlib.Path(__file__).resolve().parents[2]
COST = REPO / "src" / "llm_router" / "cost.py"
CC_TRACK = REPO / "src" / "llm_router" / "hooks" / "cc-usage-track.py"


def _insert_columns(source: str) -> list[list[str]]:
    """Column lists of every `INSERT INTO usage (...)` in *source*."""
    out = []
    for chunk in source.split("INSERT INTO usage")[1:]:
        head = chunk.split(")")[0]
        cols = [c.strip() for c in head.strip().lstrip("(").replace("\n", " ").split(",")]
        out.append([c for c in cols if c])
    return out


def test_every_usage_writer_stamps_provenance():
    """Both inserts, not just the one the audit named."""
    writers = {
        "cost.py": COST.read_text(encoding="utf-8"),
        "hooks/cc-usage-track.py": CC_TRACK.read_text(encoding="utf-8"),
    }
    missing = []
    total_inserts = 0
    for name, src in writers.items():
        for cols in _insert_columns(src):
            total_inserts += 1
            if "is_simulated" not in cols:
                missing.append(f"{name}: INSERT omits is_simulated ({cols})")

    assert total_inserts >= 2, (
        f"only found {total_inserts} INSERT INTO usage statements — the parser "
        f"missed one and this check would be vacuous"
    )
    assert not missing, "\n".join(missing)


def test_the_savings_query_still_filters_on_the_column():
    """The filter only became real once the column was written. Both must hold."""
    src = COST.read_text(encoding="utf-8")
    assert "is_simulated IS NOT 1" in src, (
        "get_savings_by_period no longer excludes simulated rows"
    )


def test_provenance_detection_is_shared_not_reimplemented():
    """Four provenance schemes existed and three did not work.

    Each surface deciding for itself what counts as real is the mechanism. The
    spend path must delegate to the same `detect_synthetic` the ledger uses.
    """
    src = COST.read_text(encoding="utf-8")
    assert "from llm_router.routing_quality import detect_synthetic" in src, (
        "cost.py reimplements provenance instead of delegating"
    )


def test_cost_provenance_fails_closed(monkeypatch):
    """Unknown provenance is marked synthetic, not admitted as production.

    Matching `is_evaluable`: the cost of wrongly excluding a real row is a
    smaller sample; the cost of wrongly including a fixture is a number that is
    quietly false.
    """
    from llm_router import cost
    import llm_router.routing_quality as rq

    def _boom():
        raise RuntimeError("provenance unavailable")

    monkeypatch.setattr(rq, "detect_synthetic", _boom)
    assert cost._detect_synthetic() is True, (
        "a provenance failure admitted the row as production"
    )


def test_under_pytest_a_row_is_marked_synthetic():
    """The property that makes the gate meaningful: this suite's own writes."""
    from llm_router import cost

    assert cost._detect_synthetic() is True, (
        "rows written by the test suite are not marked synthetic, so a full "
        "suite run would still inflate reported savings"
    )


def test_hook_detector_matches_the_canonical_one():
    """The hook inlines the check (it must run without llm_router importable).

    Inlining is correct here and drift is the risk, so pin them together.
    """
    from llm_router.routing_quality import detect_synthetic

    src = CC_TRACK.read_text(encoding="utf-8")
    assert "_is_synthetic_run" in src
    assert "PYTEST_CURRENT_TEST" in src, "hook detector lost the pytest signal"
    assert "LLM_ROUTER_SYNTHETIC" in src, "hook detector lost the explicit flag"
    # and the canonical one still uses both, or the mirror is mirroring nothing
    canon = (REPO / "src" / "llm_router" / "routing_quality.py").read_text(encoding="utf-8")
    body = canon.split("def detect_synthetic")[1].split("def ")[0]
    assert "PYTEST_CURRENT_TEST" in body and "LLM_ROUTER_SYNTHETIC" in body
    assert detect_synthetic() is True  # we are under pytest


def test_name_based_filtering_is_not_used_on_the_savings_path():
    """`_is_test_model` moved the figure FURTHER from truth (+$83.49 -> +$87.96).

    It is blind to 1,813 fixture rows wearing real model names. It may still be
    used by display surfaces, but it must not be the thing standing between a
    fixture and a published savings number.
    """
    src = COST.read_text(encoding="utf-8")
    savings_fn = src.split("async def get_savings_by_period")[1].split("\nasync def ")[0]
    assert "_is_test_model" not in savings_fn, (
        "the savings query relies on model-name matching, which cannot see "
        "fixture rows that carry real model names"
    )


def test_this_check_is_not_vacuous():
    """The parser must genuinely find columns, or every assertion above is empty."""
    cols = _insert_columns(COST.read_text(encoding="utf-8"))
    assert cols, "no INSERT INTO usage found in cost.py"
    assert any("model" in c and "cost_usd" in c for c in cols), (
        f"parsed column lists look wrong: {cols}"
    )
