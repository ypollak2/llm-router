"""G-F Group B — `cost._validate_routing_insert`, the provider allowlist and its guards.

19 mutants survived. Twelve of them mutate individual entries in `VALID_PROVIDERS`, a
frozenset literal spread over four source lines — every entry is a separate mutation
target and no test enumerated the set.

THIS IS FINDING #30's OTHER GUARD
---------------------------------
This function exists to stop contaminated test data reaching the production database.
It is the plausibility check; `_refuse_unisolated_test_write` is the isolation check.
The audit already established that plausibility was NOT enough — 28,536 synthetic rows
named a real model with realistic costs and walked straight past it.

That does not make this guard worthless; it makes its exact boundaries worth pinning.
A mutant dropping a provider from the allowlist REJECTS REAL TRAFFIC — the routing
decision is lost and the dashboard undercounts, silently. A mutant widening the guard
lets more test data in. Both directions are failures, in opposite directions.
"""

from __future__ import annotations

import pytest

from llm_router.cost import _validate_routing_insert


def _ok(provider: str = "openai", model: str = "openai/gpt-4o", cost: float = 0.01):
    """Call the validator; raises ValueError if it rejects."""
    _validate_routing_insert(model, provider, cost)


class TestEveryAllowlistedProviderIsAccepted:
    """Twelve mutants live in this frozenset. Each entry needs its own assertion.

    A single "openai is accepted" test kills none of the others: dropping 'groq' from
    the set is invisible unless something passes 'groq'.
    """

    @pytest.mark.parametrize("provider", [
        "ollama", "openai", "gemini", "codex",
        "claude_subscription", "subscription", "anthropic",
        "perplexity", "groq", "deepseek", "cc", "claude",
    ])
    def test_a_real_provider_is_accepted(self, provider):
        _ok(provider=provider)

    def test_an_unlisted_provider_is_rejected(self):
        with pytest.raises(ValueError, match="invalid provider"):
            _ok(provider="not-a-real-provider")

    def test_the_empty_provider_is_rejected(self):
        with pytest.raises(ValueError, match="invalid provider"):
            _ok(provider="")

    def test_the_rejection_names_the_offending_provider(self):
        """The message is the operator's only clue about which insert was dropped."""
        with pytest.raises(ValueError, match="mystery-provider"):
            _ok(provider="mystery-provider")

    def test_the_rejection_lists_the_valid_providers(self):
        """`f"Valid providers: {sorted(VALID_PROVIDERS)}"` — a rejection that does not
        say what WOULD be accepted leaves the caller guessing."""
        with pytest.raises(ValueError) as exc:
            _ok(provider="nope")
        assert "openai" in str(exc.value)
        assert "ollama" in str(exc.value)

    def test_the_allowlist_has_no_duplicate_entries(self):
        """`'anthropic'` used to appear twice in the literal, on the second and fourth
        lines. In a frozenset the second occurrence is a no-op, so mutating EITHER copy
        left the other and the set was unchanged — three mutants unkillable by
        construction, and no behavioural test could reach them.

        The owner approved removing it. This replaces the test that asserted the
        duplicate AS THE CONTRACT, which is the "a test can encode a defect as the
        contract" shape. A duplicate also hides a typo'd entry: two lines that look
        different but collapse to one member.

        Source-inspecting by necessity — set membership cannot observe a duplicate
        behaviourally. Amendment 1 may deselect it from the mutation run, which is
        correct: it is a hygiene assertion, not a mutant-killer, and must not be
        counted toward the score. The behavioural coverage of every entry lives in
        `test_a_real_provider_is_accepted` above.
        """
        import inspect

        from llm_router import cost

        src = inspect.getsource(cost._validate_routing_insert)
        literal = src.split("VALID_PROVIDERS = frozenset({")[1].split("})")[0]
        names = [
            n.split("#")[0].strip().strip("'\"")
            for n in literal.split(",")
            if n.split("#")[0].strip()
        ]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"duplicate provider(s) in the allowlist: {sorted(dupes)}"

    def test_provider_matching_is_case_sensitive(self):
        """Pins the current contract rather than assuming leniency. `OpenAI` is NOT in
        the set, so it is rejected — worth an explicit test so a future normalisation
        is a deliberate change and not an accident."""
        with pytest.raises(ValueError, match="invalid provider"):
            _ok(provider="OpenAI")


class TestTestLikeModelsAreRejected:
    """`if not final_model or final_model.startswith('test/')`."""

    def test_a_test_prefixed_model_is_rejected(self):
        with pytest.raises(ValueError, match="looks like test data"):
            _ok(model="test/fake-model")

    def test_an_empty_model_is_rejected(self):
        with pytest.raises(ValueError, match="looks like test data"):
            _ok(model="")

    def test_the_prefix_is_matched_at_the_START_only(self):
        """`startswith('test/')` — a real model with 'test/' elsewhere in the name must
        NOT be rejected. A mutant switching to a containment check would drop real
        traffic from any provider whose model names embed that substring."""
        _ok(model="openai/latest/gpt-4o")
        _ok(model="vendor/pytest-tuned-model")

    def test_the_prefix_literal_is_exact(self):
        """`test-` and `test` without the slash are NOT the rejection pattern."""
        _ok(model="test-model-v2")
        _ok(model="testing/gpt-4o")

    def test_the_rejection_names_the_offending_model(self):
        with pytest.raises(ValueError, match="test/synthetic"):
            _ok(model="test/synthetic")


class TestCostPlausibilityBand:
    """`if cost_usd < 0 or cost_usd > 100` — three mutants on one line.

    Both bounds and both boundary values, because `<`/`<=` and `>`/`>=` are separate
    mutations and only the exact boundary distinguishes them.
    """

    def test_zero_is_accepted(self):
        _ok(cost=0.0)

    def test_exactly_one_hundred_is_accepted(self):
        _ok(cost=100.0)

    def test_just_over_one_hundred_is_rejected(self):
        with pytest.raises(ValueError, match="implausible"):
            _ok(cost=100.01)

    def test_a_negative_cost_is_rejected(self):
        with pytest.raises(ValueError, match="implausible"):
            _ok(cost=-0.01)

    def test_a_realistic_sub_cent_cost_is_accepted(self):
        """The error text names 'Haiku ~$0.00002' as a real cost, so the low end of the
        band must admit it."""
        _ok(cost=0.00002)

    def test_the_rejection_reports_the_offending_value(self):
        with pytest.raises(ValueError, match="777"):
            _ok(cost=777.0)


class TestTheGuardsAreIndependent:
    """Each check must fire on its own input, not mask another.

    A mutant collapsing two checks into one would still reject the combined case, so
    each is asserted with the OTHER fields valid.
    """

    def test_a_valid_row_passes_all_three(self):
        _ok(provider="ollama", model="ollama/hermes3:8b", cost=0.0)

    def test_provider_is_checked_even_when_model_and_cost_are_fine(self):
        with pytest.raises(ValueError, match="invalid provider"):
            _validate_routing_insert("openai/gpt-4o", "bogus", 0.01)

    def test_model_is_checked_even_when_provider_and_cost_are_fine(self):
        with pytest.raises(ValueError, match="looks like test data"):
            _validate_routing_insert("test/x", "openai", 0.01)

    def test_cost_is_checked_even_when_provider_and_model_are_fine(self):
        with pytest.raises(ValueError, match="implausible"):
            _validate_routing_insert("openai/gpt-4o", "openai", 1e6)
