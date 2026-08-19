"""G-F class C7 — the guards are tested AT their boundary value.

THE CLASS
---------
33 mutants flip a comparison by one notch: `<= 0` becomes `< 0`, `>= threshold` becomes
`> threshold`, `> 100` becomes `>= 100`. Each survives for the same reason — every
existing test passes a value comfortably inside or outside the range, and only the value
sitting exactly ON the boundary can tell `<=` from `<`.

This is the smallest class in the G-F set and the one with the highest density of real
defects: off-by-one in a guard is not a style issue, it is the difference between
rejecting a zero-cost reservation and accepting it, or between "$1.00" and "$1.0000" on a
dashboard.

HOW EACH TEST IS BUILT
----------------------
One assertion at the boundary value itself, plus one on each side where the sides
disambiguate. A test that only checks 5 and -5 cannot distinguish `<= 0` from `< 0`; the
case that matters is exactly 0.

Assertions are on observable behaviour — the dict the function maintains, the string it
returns, the value it computes — never on the comparison's source text.
"""

from __future__ import annotations

import pytest

from llm_router import budget, cost, router


class TestReserveForRejectsZero:
    """`if cost_usd <= 0: return` — the boundary is 0 exactly.

    Mutated to `< 0`, a zero-cost reservation would be RECORDED instead of ignored,
    creating a dict entry for a reservation of nothing. The docstring's contract is
    "negative costs are clamped to zero"; zero itself must also be a no-op.
    """

    @pytest.fixture(autouse=True)
    def _isolated_pending(self, monkeypatch):
        monkeypatch.setattr(budget, "_pending_spend_by_key", {})

    def test_zero_is_not_reserved(self):
        budget.reserve_for("k", 0.0)
        assert budget._pending_spend_by_key == {}, (
            "a zero-cost reservation must not create an entry"
        )

    def test_negative_is_not_reserved(self):
        budget.reserve_for("k", -5.0)
        assert budget._pending_spend_by_key == {}

    def test_a_positive_cost_is_reserved(self):
        budget.reserve_for("k", 0.25)
        assert budget._pending_spend_by_key == {"k": 0.25}

    def test_the_smallest_positive_value_still_reserves(self):
        """Guards the other direction: a mutant widening the guard to `<= 0.0001`
        would swallow small-but-real reservations, and only a tiny positive value
        distinguishes that from the correct behaviour."""
        budget.reserve_for("k", 1e-9)
        assert budget._pending_spend_by_key == {"k": 1e-9}


class TestReleaseForRejectsZero:
    """Same boundary, inverse operation. Zero must not disturb an existing reservation."""

    @pytest.fixture(autouse=True)
    def _isolated_pending(self, monkeypatch):
        monkeypatch.setattr(budget, "_pending_spend_by_key", {"k": 1.0})

    def test_releasing_zero_leaves_the_reservation_untouched(self):
        budget.release_for("k", 0.0)
        assert budget._pending_spend_by_key == {"k": 1.0}

    def test_releasing_a_negative_leaves_the_reservation_untouched(self):
        budget.release_for("k", -1.0)
        assert budget._pending_spend_by_key == {"k": 1.0}

    def test_a_partial_release_reduces_it(self):
        budget.release_for("k", 0.25)
        assert budget._pending_spend_by_key == {"k": 0.75}

    def test_a_full_release_removes_the_entry_entirely(self):
        """The entry is garbage-collected at exactly zero, not left at 0.0.

        `new_value == 0.0` is its own boundary: leaving a 0.0 entry behind would grow
        the dict without bound as identities come and go, which is what the code
        comment says the pop is for.
        """
        budget.release_for("k", 1.0)
        assert budget._pending_spend_by_key == {}

    def test_an_over_release_floors_at_zero_and_removes(self):
        budget.release_for("k", 99.0)
        assert budget._pending_spend_by_key == {}

    def test_releasing_zero_against_a_zero_entry_does_not_delete_it(self, monkeypatch):
        """The input that actually distinguishes `<= 0` from `< 0`.

        With an entry of 1.0, releasing 0.0 leaves it at 1.0 under BOTH spellings —
        the guard returns early, or the arithmetic subtracts nothing and rewrites the
        same value. The behaviours only diverge on an entry of exactly 0.0:

            `<= 0`  returns early          -> {"k": 0.0} survives
            `< 0`   falls through, computes max(0, 0-0) == 0.0 -> the key is POPPED

        The first four tests in this class all passed against the mutant. Only this
        one fails, and it took reading the mutant to find the input that shows it.
        """
        monkeypatch.setattr(budget, "_pending_spend_by_key", {"k": 0.0})
        budget.release_for("k", 0.0)
        assert budget._pending_spend_by_key == {"k": 0.0}, (
            "releasing zero must be a no-op, not a delete"
        )


class TestPendingPressureOffsetAtZero:
    """`if pending <= 0: return 0.0` — zero pending tokens is no pressure."""

    @pytest.fixture(autouse=True)
    def _isolated_tokens(self, monkeypatch):
        monkeypatch.setattr(budget, "_pending_tokens", {})

    def test_zero_pending_tokens_is_zero_offset(self, monkeypatch):
        monkeypatch.setitem(budget._pending_tokens, "openai", 0)
        assert budget._get_pending_pressure_offset("openai") == 0.0

    def test_a_provider_with_no_entry_is_zero_offset(self):
        assert budget._get_pending_pressure_offset("never-seen") == 0.0

    def test_positive_pending_tokens_produce_positive_pressure(self, monkeypatch):
        monkeypatch.setitem(budget._pending_tokens, "openai", 2_500)
        assert budget._get_pending_pressure_offset("openai") > 0.0

    def test_a_single_pending_token_is_still_pending(self, monkeypatch):
        """The input that distinguishes `<= 0` from `<= 1`.

        A mutant widening the guard by one swallows the smallest real backlog and
        reports zero pressure for a provider that has work outstanding. Zero and 2,500
        both pass under either spelling; only 1 separates them.
        """
        monkeypatch.setitem(budget._pending_tokens, "openai", 1)
        assert budget._get_pending_pressure_offset("openai") > 0.0


class TestSpendDisplayAtExactlyOneDollar:
    """`f"${x:.2f}" if spend_usd >= 1.0 else f"${x:.4f}"` — the boundary is 1.0 exactly.

    Mutated to `> 1.0`, a spend of exactly one dollar renders as "$1.0000" instead of
    "$1.00". Only the value 1.0 itself separates the two.
    """

    def test_exactly_one_dollar_uses_two_decimals(self):
        assert cost.format_spend_for_display(1.0) == "$1.00"

    def test_just_below_one_dollar_uses_four_decimals(self):
        assert cost.format_spend_for_display(0.9999) == "$0.9999"

    def test_well_above_one_dollar_uses_two_decimals(self):
        assert cost.format_spend_for_display(12.345) == "$12.35"

    def test_zero_uses_four_decimals(self):
        assert cost.format_spend_for_display(0.0) == "$0.0000"


class TestStableTaskOffsetAtZeroModulus:
    """`if modulus <= 0: return 0` — a modulus of zero must not reach the `%`.

    Mutated to `< 0`, `modulus == 0` falls through to `crc32(...) % 0` and raises
    ZeroDivisionError. The function exists to be deterministic and total; a modulus of
    zero is the one input that turns it into a crash.
    """

    def test_zero_modulus_returns_zero_rather_than_dividing(self):
        assert router._stable_task_offset("query", 0) == 0

    def test_negative_modulus_returns_zero(self):
        assert router._stable_task_offset("query", -1) == 0

    def test_a_modulus_of_one_is_in_range(self):
        assert router._stable_task_offset("query", 1) == 0

    def test_a_positive_modulus_stays_in_range_and_is_stable(self):
        first = router._stable_task_offset("analyze", 7)
        assert 0 <= first < 7
        assert router._stable_task_offset("analyze", 7) == first


class TestPreciseAnswerPromptLengthCap:
    """`if not prompt or len(prompt) > 400` — the boundary is 400 characters exactly.

    Mutated to `>= 400`, a prompt of exactly 400 characters is rejected as "long" when
    the contract says long means MORE than 400. The prompt below carries a computation
    signal so the length is the only thing under test.
    """

    @staticmethod
    def _prompt_of(length: int) -> str:
        # Ends with a computation signal; padded with a filler that carries none.
        tail = " what is 2+2?"
        return "x" * (length - len(tail)) + tail

    def test_exactly_400_characters_is_still_short_enough(self):
        p = self._prompt_of(400)
        assert len(p) == 400
        assert router._needs_precise_answer(p) is True

    def test_401_characters_is_too_long(self):
        p = self._prompt_of(401)
        assert len(p) == 401
        assert router._needs_precise_answer(p) is False

    def test_an_empty_prompt_is_rejected(self):
        assert router._needs_precise_answer("") is False


class TestSubprocessErrorTruncationCap:
    """`elif len(first_line) > CAP` — the boundary is the cap exactly.

    Mutated to `>= CAP`, a line of exactly CAP characters is truncated when it fits.
    The truncated form is `first_line[:CAP-1] + "…"`, so an off-by-one here silently
    drops a character from every message at the boundary.
    """

    CAP = router._SUBPROCESS_ERROR_CONTENT_CAP

    def test_a_line_of_exactly_the_cap_is_not_truncated(self):
        line = "e" * self.CAP
        out = router._format_subprocess_chain_error("codex", 1, line)
        assert out == f"codex exited 1: {line}"
        assert "…" not in out

    def test_one_character_over_the_cap_is_truncated(self):
        line = "e" * (self.CAP + 1)
        out = router._format_subprocess_chain_error("codex", 1, line)
        assert out.endswith("…")
        assert len(out.split(": ", 1)[1]) == self.CAP

    def test_empty_content_keeps_the_diagnostic_shape(self):
        assert router._format_subprocess_chain_error("codex", 2, "") == (
            "codex exited 2: <no stderr captured>"
        )

    def test_none_content_keeps_the_diagnostic_shape(self):
        assert router._format_subprocess_chain_error("codex", 2, None) == (
            "codex exited 2: <no stderr captured>"
        )


class TestRoutingInsertCostPlausibilityCap:
    """`if cost_usd < 0 or cost_usd > 100` — the upper boundary is 100 exactly.

    Mutated to `>= 100`, a cost of exactly $100 is rejected as implausible. The error
    message itself says the expected range, so the boundary is documented behaviour.
    """

    def test_exactly_one_hundred_is_accepted(self):
        cost._validate_routing_insert("openai/gpt-4o", "openai", 100.0)

    def test_just_over_one_hundred_is_rejected(self):
        with pytest.raises(ValueError, match="implausible"):
            cost._validate_routing_insert("openai/gpt-4o", "openai", 100.01)

    def test_zero_is_accepted(self):
        cost._validate_routing_insert("openai/gpt-4o", "openai", 0.0)

    def test_a_negative_cost_is_rejected(self):
        with pytest.raises(ValueError, match="implausible"):
            cost._validate_routing_insert("openai/gpt-4o", "openai", -0.01)
