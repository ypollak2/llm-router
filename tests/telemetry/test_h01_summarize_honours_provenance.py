"""H-01 — provenance was computed and then not read.

`is_evaluable` exists to answer one question: may this row feed a published
quality number? `summarize()` produces every published quality number and
contained **zero** references to it, or to `synthetic`.

Demonstrated in the audit on an isolated ledger: a single synthetic row, alone,
produced `quality_escalation_rate: 1.0`.

This was not the only instance. Four provenance mechanisms existed and three did
not work:

    synthetic / is_evaluable   correct -- and had ONE consumer, not summarize()
    is_simulated              never written; its filter excluded nothing, ever
    is_real                   defaults 1, maintained heuristically
    _is_test_model()          name matching, blind to 1,813 fixture rows

and `attribution.py` -- written expressly to end that fragmentation, docstring
"one definition, consumed by every surface" -- had zero production callers.

So the tests here check two things: that the filter is applied, and that it says
what it removed. A denominator that shrinks silently is the failure this repo's
own CLAUDE.md documents twice.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from llm_router.routing_quality import summarize


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "routing_quality.jsonl"
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(path))
    return path


def _write(path: pathlib.Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _base_row(**over) -> dict:
    row = {
        "schema_version": 4,
        "route_id": "r",
        "route_kind": "completion",
        "task_type": "code",
        "parent_route_id": None,
        "route_outcome": "success",
        "route_succeeded": True,
        "verification_attempted": False,
        "quality_escalation_occurred": False,
        "mis_route": None,
        "synthetic": False,
    }
    row.update(over)
    return row


def test_a_lone_synthetic_row_no_longer_drives_the_rate(ledger):
    """The audit's exact demonstration, inverted into a regression test."""
    _write(ledger, _base_row(synthetic=True, quality_escalation_occurred=True, mis_route=True))

    out = summarize(str(ledger))
    assert out["evaluable_rows"] == 0
    assert out["excluded_unevaluable_rows"] == 1
    assert out["quality_escalation_rate"] is None, (
        f"a single synthetic row still produced a rate: {out['quality_escalation_rate']}"
    )


def test_rows_predating_provenance_are_excluded_not_assumed_real(ledger):
    """`is_evaluable` fails closed. UNKNOWN is not production.

    Treating unknown as production is how 29% test traffic reached every
    historical figure.
    """
    legacy = _base_row()
    del legacy["synthetic"]
    _write(ledger, legacy)

    out = summarize(str(ledger))
    assert out["evaluable_rows"] == 0, "a row with no provenance field was counted"
    assert out["excluded_unevaluable_rows"] == 1


def test_real_rows_still_count(ledger):
    """Anti-over-correction: a filter that excludes everything is not a filter."""
    for i in range(3):
        _write(ledger, _base_row(route_id=f"r{i}", synthetic=False))

    out = summarize(str(ledger))
    assert out["evaluable_rows"] == 3
    assert out["excluded_unevaluable_rows"] == 0
    assert out["schema_v2_rows"] == 3


def test_mixed_population_counts_only_the_real_rows(ledger):
    """The realistic case, and the one the audit measured wrong."""
    for i in range(4):
        _write(ledger, _base_row(route_id=f"real{i}", synthetic=False))
    for i in range(6):
        _write(ledger, _base_row(route_id=f"fake{i}", synthetic=True,
                                 quality_escalation_occurred=True, mis_route=True))

    out = summarize(str(ledger))
    assert out["total_rows"] == 10
    assert out["evaluable_rows"] == 4
    assert out["excluded_unevaluable_rows"] == 6
    assert out["quality_escalation_rate"] == 0.0, (
        "synthetic escalations leaked into the published rate"
    )


def test_exclusions_are_reported_not_silent(ledger):
    """A denominator that shrinks without saying so is the failure mode itself."""
    _write(ledger, _base_row(synthetic=True))
    out = summarize(str(ledger))
    assert "excluded_unevaluable_rows" in out, (
        "summarize drops rows without reporting how many — the reader cannot tell "
        "a clean measurement from an empty one"
    )
    assert out["total_rows"] != out["evaluable_rows"]


def test_summarize_actually_references_the_guard():
    """The defect was an absence, so pin the presence.

    `summarize` had zero references to `is_evaluable` or `synthetic`; a future
    refactor that drops the call would otherwise only show up as numbers that
    look slightly better.
    """
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "llm_router" / "routing_quality.py").read_text(encoding="utf-8")
    body = src.split("def summarize(")[1]
    assert "is_evaluable" in body, "summarize() no longer filters on provenance"


def test_this_suite_is_not_vacuous(ledger):
    """The fixtures must be capable of producing a non-None rate at all.

    If `_base_row` were malformed, every rate would be None and every assertion
    above would pass for the wrong reason.
    """
    _write(ledger, _base_row(synthetic=False, quality_escalation_occurred=True, mis_route=True))
    out = summarize(str(ledger))
    assert out["evaluable_rows"] == 1
    assert out["quality_escalation_rate"] == 1.0, (
        "a real escalating row does not produce a rate — the fixture shape is wrong "
        "and the exclusion tests prove nothing"
    )
