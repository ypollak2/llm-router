"""G-F Group B — `coverage.snapshot`, and the difference between empty and unreadable.

19 mutants survived, concentrated on the paths that decide whether a store is REPORTED
or declared unreadable:

    5  malformed += 1
    4  with path.open("r", encoding="utf-8") as fh
    3  _cached_snapshot = Coverage(readable=False)
    3  name = str(event.get("d", "")) or "UNKNOWN"
    2  if malformed and observed == 0 and unobserved == 0

THE FAILURE THIS FUNCTION EXISTS TO PREVENT
-------------------------------------------
Its own docstring: "an unreadable store is not a quiet period, and reporting it as one
is the RED2-02 failure shape." WP-07 added coverage precisely because a rate without its
denominator "silently redefines itself when routing degrades".

So `readable=False` and `observed_n=0` mean completely different things — *"we cannot
tell you"* versus *"we saw nothing"* — and every mutant here erodes that distinction.
The comment is equally explicit about the partial case: malformed lines "are not simply
skipped when they are all we have", because a partial count beats no count only as long
as the total is not silently understated.
"""

from __future__ import annotations

import pytest

from llm_router import coverage
from llm_router.paths import is_isolated


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """An isolated coverage store, proven isolated, cache reset around every test."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert is_isolated(), "LLM_ROUTER_HOME did not take effect — refusing to write"
    monkeypatch.setattr(coverage, "_cached_snapshot", None)
    yield coverage.store_path()
    monkeypatch.setattr(coverage, "_cached_snapshot", None)


def _write(path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")


class TestEmptyIsNotUnreadable:
    """The distinction the whole module exists to preserve."""

    def test_a_missing_store_is_readable_and_zero(self, store):
        """No store yet is a genuine 'we saw nothing', not a failure."""
        snap = coverage.snapshot()
        assert snap.readable is True
        assert (snap.observed_n, snap.unobserved_n) == (0, 0)

    def test_an_empty_file_is_readable_and_zero(self, store):
        _write(store)
        snap = coverage.snapshot()
        assert snap.readable is True
        assert (snap.observed_n, snap.unobserved_n) == (0, 0)

    def test_blank_lines_are_skipped_not_counted_as_malformed(self, store):
        """`if not line: continue` — whitespace is not corruption."""
        _write(store, '{"k":"o"}', "", "   ", '{"k":"o"}')
        snap = coverage.snapshot()
        assert snap.readable is True
        assert snap.observed_n == 2


class TestEntirelyUnparseableIsUnreadable:
    """`if malformed and observed == 0 and unobserved == 0` — all three conjuncts."""

    def test_a_wholly_corrupt_store_reports_unreadable_NOT_zero(self, store):
        _write(store, "not json at all", "{oops")
        snap = coverage.snapshot()
        assert snap.readable is False, (
            "an unreadable store reported as zero is the RED2-02 shape this module "
            "was written to prevent"
        )

    def test_ONE_good_line_is_enough_to_report_rather_than_refuse(self, store):
        """The `observed == 0` conjunct. Partial corruption still reports, because
        "a partial count beats no count as long as the total is not understated"."""
        _write(store, "garbage", '{"k":"o"}', "more garbage")
        snap = coverage.snapshot()
        assert snap.readable is True
        assert snap.observed_n == 1

    def test_one_UNOBSERVED_line_is_also_enough(self, store):
        """The `unobserved == 0` conjunct, which the observed-only test cannot reach."""
        _write(store, "garbage", '{"k":"u","d":"reason"}', "junk")
        snap = coverage.snapshot()
        assert snap.readable is True
        assert snap.unobserved_n == 1

    def test_no_malformed_lines_means_readable_even_at_zero(self, store):
        """The `malformed and ...` conjunct: an empty-but-valid store is readable."""
        _write(store, "", "  ")
        assert coverage.snapshot().readable is True


class TestEventClassification:
    """`k == "o"` observed, `k == "u"` unobserved, anything else malformed."""

    def test_observed_and_unobserved_are_counted_separately(self, store):
        _write(store, '{"k":"o"}', '{"k":"o"}', '{"k":"o"}', '{"k":"u","d":"x"}')
        snap = coverage.snapshot()
        assert (snap.observed_n, snap.unobserved_n) == (3, 1), (
            "distinct counts — equal fixtures would hide a swap of the two branches"
        )

    def test_an_unrecognised_kind_counts_as_malformed(self, store):
        """`else: malformed += 1` — valid JSON with an unknown `k` is not silently
        dropped. Skipping it would understate the total, which the comment forbids."""
        _write(store, '{"k":"z"}', '{"k":"z"}')
        snap = coverage.snapshot()
        assert snap.readable is False, "two unknown kinds and nothing else is unreadable"

    def test_valid_json_that_is_not_an_object_is_malformed(self, store):
        """Was xfail(strict=True) while the defect stood; the guard is now in place.

        `json.loads` succeeds on a JSON array or string. The parse is hoisted out of the
        try because `event` is read twice, so before the fix `event.get("k")` raised
        AttributeError on a non-dict and escaped the loop entirely.
        """
        _write(store, "[1,2,3]", '"a string"')
        assert coverage.snapshot().readable is False

    def test_a_non_dict_line_does_not_discard_the_lines_around_it(self, store):
        """The regression that actually mattered, which the xfail could not reach.

        Before the fix the bad line aborted the loop and BOTH good lines were lost —
        understating the total, which this module's own comment forbids.
        """
        _write(store, '{"k":"o"}', "[1,2,3]", '{"k":"o"}')
        snap = coverage.snapshot()
        assert snap.readable is True
        assert snap.observed_n == 2, "the good lines on BOTH sides must survive"
        assert snap.malformed_n == 1


class TestUnobservedReasons:
    """`name = str(event.get("d", "")) or "UNKNOWN"` — 3 mutants on one line."""

    def test_a_reason_is_recorded_under_its_own_name(self, store):
        _write(store, '{"k":"u","d":"no_hook"}', '{"k":"u","d":"no_hook"}',
               '{"k":"u","d":"timeout"}')
        by = coverage.snapshot().by_reason
        assert by == {"no_hook": 2, "timeout": 1}

    def test_a_missing_reason_becomes_UNKNOWN_not_an_empty_key(self, store):
        """The `or "UNKNOWN"` fallback. An empty-string key renders as a blank row —
        indistinguishable from a formatting bug rather than a named cause."""
        _write(store, '{"k":"u"}')
        assert coverage.snapshot().by_reason == {"UNKNOWN": 1}

    def test_an_empty_reason_string_also_becomes_UNKNOWN(self, store):
        _write(store, '{"k":"u","d":""}')
        assert coverage.snapshot().by_reason == {"UNKNOWN": 1}

    def test_a_non_string_reason_is_coerced_not_dropped(self, store):
        """`str(...)` — a numeric reason code still needs a bucket."""
        _write(store, '{"k":"u","d":404}')
        assert coverage.snapshot().by_reason == {"404": 1}

    def test_reasons_accumulate_rather_than_overwrite(self, store):
        _write(store, *['{"k":"u","d":"same"}' for _ in range(5)])
        assert coverage.snapshot().by_reason == {"same": 5}


class TestTheCache:
    """`if _cached_snapshot is not None: return _cached_snapshot`."""

    def test_a_second_call_returns_the_same_object(self, store):
        _write(store, '{"k":"o"}')
        assert coverage.snapshot() is coverage.snapshot()

    def test_the_cache_hides_later_writes_until_cleared(self, store):
        """Documents the actual contract rather than an assumed one: the snapshot is
        memoised, so a caller that writes after reading sees the OLD value. `clear()`
        is what makes a fresh read possible."""
        _write(store, '{"k":"o"}')
        assert coverage.snapshot().observed_n == 1
        _write(store, '{"k":"o"}', '{"k":"o"}', '{"k":"o"}')
        assert coverage.snapshot().observed_n == 1, "still cached"
        coverage.clear()
        _write(store, '{"k":"o"}', '{"k":"o"}', '{"k":"o"}')
        assert coverage.snapshot().observed_n == 3
