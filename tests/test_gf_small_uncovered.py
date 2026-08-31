"""G-F — the five small functions with no test executing them.

Together they hold ~21 mutants, all 🫥 no-coverage at baseline. Each is small enough
that "it's obviously fine" is the reason nobody wrote a test, and each has a contract its
own docstring states explicitly.

`tool_surface.door_name` is the one that matters most. Its docstring:

    "Matching must not do that: `_door_for` in enforce-route compares this against the
    tool the caller actually invoked, and substituting a different name there turns a
    correct call into a RECORDED VIOLATION."

So a mutant here does not produce a wrong hint — it produces a false accusation against a
caller that did the right thing. That is worth a test even at three mutants.
"""

from __future__ import annotations

import pytest

from llm_router import budget, router, tool_surface


class TestDoorNameNeverSubstitutes:
    """`door_name` returns the name to MATCH against, never a capable substitute.

    Contrast with `resolve`, which deliberately degrades a known tool to something
    callable. Confusing the two turns a correct call into a recorded violation.
    """

    def test_a_registered_name_is_returned_unchanged(self):
        reg = tool_surface.registered_tools(None)
        known = next(iter(reg)) if reg else "llm"
        assert tool_surface.door_name(known) == known

    def test_an_unknown_name_is_returned_EXACTLY_as_given(self):
        """Not resolved, not substituted, not defaulted.

        `door_name` is compared against what the caller actually invoked. Returning
        anything other than the input for an unrecognised name would make an honest
        call look like a violation of a tool it never claimed to use.
        """
        assert tool_surface.door_name("totally-made-up-tool") == "totally-made-up-tool"

    def test_a_deprecated_name_maps_to_its_door_when_that_door_is_registered(self):
        legacy = next(iter(tool_surface.DEPRECATED_TOOLS))
        expected_door = tool_surface.DEPRECATED_TOOLS[legacy]
        reg = tool_surface.registered_tools(None)
        out = tool_surface.door_name(legacy)
        if reg is not None and expected_door in reg and legacy not in reg:
            assert out == expected_door
        else:
            assert out == legacy

    def test_the_slim_argument_reaches_registered_tools(self):
        """`reg = registered_tools(slim)` — the tier must be forwarded, not ignored.

        A mutant passing `None` here consults the default registry regardless of the
        tier the caller asked about, so a name registered on one tier and absent on
        another is matched against the wrong set. Only a call with an explicit,
        DIFFERENT tier can see it.
        """
        assert tool_surface.door_name("llm_query", "core") == "llm_query"
        assert tool_surface.door_name("llm_query", "consolidated") == "llm"
        assert tool_surface.door_name("llm_query", "core") != tool_surface.door_name(
            "llm_query", "consolidated"
        )

    def test_door_name_differs_from_resolve_on_a_deprecated_name(self):
        """The distinction the docstring exists to protect.

        `resolve(...).display` renders a callable form like `llm(task="code")`;
        `door_name` returns a bare name for comparison. A mutant collapsing one into
        the other passes every single-value assertion above.
        """
        legacy = next(iter(tool_surface.DEPRECATED_TOOLS))
        assert "(" not in tool_surface.door_name(legacy)
        assert "(" in tool_surface.resolve(legacy).display


class TestResolveNameIsTheBareName:
    """`resolve_name` is `resolve(...).name` — no pinned args, no display form."""

    def test_it_returns_a_bare_name_not_a_call_form(self):
        legacy = next(iter(tool_surface.DEPRECATED_TOOLS))
        name = tool_surface.resolve_name(legacy)
        assert "(" not in name and ")" not in name

    def test_it_matches_resolve_dot_name(self):
        legacy = next(iter(tool_surface.DEPRECATED_TOOLS))
        assert tool_surface.resolve_name(legacy) == tool_surface.resolve(legacy).name

    def test_it_is_not_the_display_form(self):
        """Pins that `.name` was taken, not `.display`. For a deprecated tool the two
        differ; a mutant swapping the attribute is invisible without this."""
        legacy = next(iter(tool_surface.DEPRECATED_TOOLS))
        assert tool_surface.resolve_name(legacy) != tool_surface.resolve(legacy).display

    def test_the_slim_argument_is_FORWARDED_not_dropped(self):
        """The tier argument must reach `resolve`, and this is the only test that
        can tell.

        Every other test here calls `resolve_name(legacy)` with the default tier, so
        `resolve(logical, slim)`, `resolve(logical, None)` and `resolve(logical)` are
        all indistinguishable — two mutants survived on exactly that. The tiers
        genuinely disagree: `llm_query` resolves to `llm` on the default/consolidated
        tier and stays `llm_query` on `core`.
        """
        assert tool_surface.resolve_name("llm_query", "core") == "llm_query"
        assert tool_surface.resolve_name("llm_query", "consolidated") == "llm"
        assert tool_surface.resolve_name("llm_query", "core") != tool_surface.resolve_name(
            "llm_query", "consolidated"
        )


class TestInvalidateCache:
    """`provider=None` clears everything; a name clears exactly one entry."""

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, monkeypatch):
        monkeypatch.setattr(budget, "_cache", {"openai": ("a", 1.0),
                                               "anthropic": ("b", 2.0),
                                               "ollama": ("c", 3.0)})

    def test_none_clears_every_provider(self):
        budget.invalidate_cache(None)
        assert budget._cache == {}

    def test_a_named_provider_clears_only_that_one(self):
        budget.invalidate_cache("openai")
        assert set(budget._cache) == {"anthropic", "ollama"}

    def test_an_unknown_provider_is_a_no_op_not_an_error(self):
        """`_cache.pop(provider, None)` — the default is what makes this safe.

        Removing it turns an invalidation for a provider that was never cached into a
        KeyError, on a cache-management path that should never be able to fail.
        """
        budget.invalidate_cache("never-cached")
        assert set(budget._cache) == {"openai", "anthropic", "ollama"}

    def test_the_default_call_clears_all(self):
        budget.invalidate_cache()
        assert budget._cache == {}


class TestAuthErrorHint:
    """The hint names the provider's OWN env var when there is one."""

    def test_a_known_provider_names_its_env_var(self):
        provider = next(iter(router._PROVIDER_KEY_ENV))
        env_var = router._PROVIDER_KEY_ENV[provider]
        hint = router._auth_error_hint(provider)
        assert env_var in hint
        assert provider in hint

    def test_the_provider_lookup_is_case_insensitive(self):
        """`_PROVIDER_KEY_ENV.get(provider.lower())` — a caller passing "OpenAI"
        must still get the specific hint, not the generic fallback."""
        provider = next(iter(router._PROVIDER_KEY_ENV))
        env_var = router._PROVIDER_KEY_ENV[provider]
        assert env_var in router._auth_error_hint(provider.upper())

    def test_an_unknown_provider_gets_the_generic_hint_without_an_env_var(self):
        hint = router._auth_error_hint("some-unknown-provider")
        assert "some-unknown-provider" in hint
        assert "llm-router setup" in hint
        # The generic branch must not invent a variable name.
        assert not any(v in hint for v in router._PROVIDER_KEY_ENV.values())

    def test_both_branches_mention_the_subscription_caveat(self):
        """Present in both spellings — a user hitting an auth error on an external
        provider should not conclude their Claude subscription is broken."""
        known = next(iter(router._PROVIDER_KEY_ENV))
        for hint in (router._auth_error_hint(known),
                     router._auth_error_hint("some-unknown-provider")):
            assert "subscription" in hint.lower()


class TestRestoreClaim:
    """`_restore_claim` APPENDS a failed claim back, never replaces.

    The docstring: "Uses append (never replace) so newly-arrived lines in `live` are
    preserved." A mutant switching to write-mode silently discards whatever arrived
    between the claim being taken and restored — data loss with no error.
    """

    def test_claim_lines_are_appended_to_existing_live_content(self, tmp_path):
        live = tmp_path / "live.jsonl"
        claim = tmp_path / "claim.jsonl"
        live.write_text("arrived-while-claimed\n")
        claim.write_text("claimed-line\n")

        from llm_router import cost
        cost._restore_claim(claim, live)

        text = live.read_text()
        assert "arrived-while-claimed" in text, "append mode must preserve new lines"
        assert "claimed-line" in text
        assert text.index("arrived-while-claimed") < text.index("claimed-line")

    def test_the_claim_file_is_removed_after_restoring(self, tmp_path):
        live = tmp_path / "live.jsonl"
        claim = tmp_path / "claim.jsonl"
        live.write_text("")
        claim.write_text("x\n")

        from llm_router import cost
        cost._restore_claim(claim, live)
        assert not claim.exists()

    def test_a_missing_claim_is_a_silent_no_op(self, tmp_path):
        """`except OSError: return` — an absent claim must not raise, and must not
        touch the live log."""
        live = tmp_path / "live.jsonl"
        live.write_text("untouched\n")

        from llm_router import cost
        cost._restore_claim(tmp_path / "does-not-exist.jsonl", live)
        assert live.read_text() == "untouched\n"
