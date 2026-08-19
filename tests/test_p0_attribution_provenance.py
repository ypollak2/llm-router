"""P0 — a routing row records its own origin, and unrecorded origin is its own answer.

WHAT WENT WRONG
---------------
`routing_decisions.provenance` defaults to NULL and **nothing in the codebase ever wrote
it**. `log_routing_decision`'s INSERT listed 25 columns and `provenance` was not among
them. `0aab32f` later marked one known-bad population `unattributed` retroactively.

That made `provenance IS NULL` *look* like "real traffic" when it actually meant "not yet
cleaned up" — and a second synthetic population (2,373 rows sharing one prompt_hash, one
task_type, no session_id, and an exactly 3.200:1 model split) was sitting inside that NULL
set, counted as routing on the dashboard.

The first version of the attribution rule keyed on `provenance IS NULL` and called it
attributed. It reproduced the historical numbers, so it looked right. It was reproducing
the bug.

WHAT THESE TESTS PIN
--------------------
1. The writer stamps origin at insert time. A reader cannot recover a fact the writer
   never stored, and a cleanup pass can always be out of date.
2. Unrecorded provenance is reported as UNKNOWN — never promoted into attributed or
   unattributed. Guessing manufactures a fact nobody recorded.
3. An UNRECOGNISED provenance value is also UNKNOWN, so a future marker added by another
   component cannot silently land in the wrong bucket.
"""

from __future__ import annotations

import pytest

from llm_router.attribution import (
    ATTRIBUTED_PROVENANCE,
    UNATTRIBUTED_PROVENANCE,
    AttributionStatus,
    attribution_from_rows,
)
from llm_router.cost import (
    PROVENANCE_RUNTIME,
    PROVENANCE_TEST,
    _write_provenance,
)


def _row(model: str, provenance: str | None, classifier: str | None = "heuristic") -> dict:
    return {"final_model": model, "provenance": provenance, "classifier_type": classifier}


class TestWriterStampsOrigin:
    """The fix for the root cause: the row knows where it came from."""

    def test_ordinary_runtime_is_marked_runtime(self, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("LLM_ROUTER_ALLOW_STUBS", raising=False)
        assert _write_provenance() == PROVENANCE_RUNTIME

    def test_writing_under_pytest_is_marked_test(self, monkeypatch):
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "some_test")
        assert _write_provenance() == PROVENANCE_TEST

    def test_the_stub_escape_hatch_is_marked_test(self, monkeypatch):
        """`LLM_ROUTER_ALLOW_STUBS=1` is the documented way to write stub data on purpose.

        Data written through an escape hatch is not user traffic — the flag says so —
        so it must not be counted as routing even when no test runner is present.
        """
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
        assert _write_provenance() == PROVENANCE_TEST

    def test_the_two_markers_are_different_values(self):
        assert PROVENANCE_RUNTIME != PROVENANCE_TEST
        assert PROVENANCE_RUNTIME in ATTRIBUTED_PROVENANCE
        assert PROVENANCE_TEST in UNATTRIBUTED_PROVENANCE


class TestUnrecordedProvenanceIsUnknown:
    """The correction: NULL is a third state, not a synonym for real."""

    def test_null_provenance_is_unknown_not_attributed(self):
        r = attribution_from_rows([_row("openai/gpt-4o", None)] * 5)
        assert r.unknown_decisions == 5
        assert r.attributed_decisions == 0
        assert r.unattributed_decisions == 0

    def test_empty_string_provenance_is_also_unknown(self):
        r = attribution_from_rows([_row("openai/gpt-4o", "")])
        assert r.unknown_decisions == 1

    def test_an_unrecognised_marker_is_unknown_not_guessed(self):
        """A value this version does not know must stop, not pick a side.

        If another component starts writing 'replay' or 'imported', it lands in UNKNOWN
        and becomes visible, rather than quietly inflating whichever bucket the string
        happened to fall into.
        """
        r = attribution_from_rows([_row("openai/gpt-4o", "some-future-marker")])
        assert r.unknown_decisions == 1
        assert r.attributed_decisions == 0
        assert r.unattributed_decisions == 0

    def test_runtime_rows_are_attributed(self):
        r = attribution_from_rows([_row("openai/gpt-4o", PROVENANCE_RUNTIME)] * 3)
        assert r.attributed_decisions == 3
        assert r.unknown_decisions == 0

    def test_test_rows_are_unattributed(self):
        r = attribution_from_rows([_row("openai/gpt-4o", PROVENANCE_TEST)] * 4)
        assert r.unattributed_decisions == 4
        assert r.attributed_decisions == 0


class TestReportabilityGate:
    """A share over a partial denominator is a wrong answer stated confidently."""

    def test_a_mixed_set_is_not_reportable(self):
        r = attribution_from_rows(
            [_row("a", PROVENANCE_RUNTIME)] * 10 + [_row("b", None)] * 90
        )
        assert r.attributed_decisions == 10
        assert r.unknown_decisions == 90
        assert r.is_reportable is False, (
            "10 attributed rows beside 90 of unknown origin cannot produce a share"
        )

    def test_a_fully_marked_set_is_reportable(self):
        r = attribution_from_rows(
            [_row("a", PROVENANCE_RUNTIME)] * 6 + [_row("b", PROVENANCE_TEST)] * 4
        )
        assert r.is_reportable is True
        assert r.by_model[0].share == pytest.approx(1.0)

    def test_eligible_counts_all_three_states(self):
        r = attribution_from_rows(
            [_row("a", PROVENANCE_RUNTIME)] * 2
            + [_row("b", PROVENANCE_TEST)] * 3
            + [_row("c", None)] * 5
        )
        assert r.eligible_decisions == 10
        assert (r.attributed_decisions, r.unattributed_decisions, r.unknown_decisions) == (2, 3, 5)


class TestInvariantsHold:
    def test_shares_sum_to_one_over_attributed_only(self):
        r = attribution_from_rows(
            [_row("a", PROVENANCE_RUNTIME)] * 3
            + [_row("b", PROVENANCE_RUNTIME)] * 1
            + [_row("c", None)] * 50          # must not enter the denominator
        )
        assert sum(m.share for m in r.by_model) == pytest.approx(1.0)
        assert r.by_model[0].share == pytest.approx(0.75)

    def test_classifier_breakdown_covers_attributed_only(self):
        r = attribution_from_rows(
            [_row("a", PROVENANCE_RUNTIME, "heuristic")] * 2
            + [_row("b", PROVENANCE_TEST, "heuristic")] * 7
        )
        assert sum(r.classifier_breakdown.values()) == r.attributed_decisions == 2

    def test_a_missing_classifier_on_a_real_row_is_still_a_decision(self):
        r = attribution_from_rows([_row("a", PROVENANCE_RUNTIME, None)])
        assert r.attributed_decisions == 1
        assert r.classifier_breakdown == {"unrecorded": 1}

    def test_the_three_status_values_are_distinct(self):
        assert len({s.value for s in AttributionStatus}) == 3
