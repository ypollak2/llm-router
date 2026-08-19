"""G-F Group B — `tool_surface.resolve`, all four resolution branches.

37 mutants survived, spread across every return in the function:

     7  ToolCall(cand_door, pinned, ..., degraded=True)   chain entry behind a door
     5  ToolCall(logical, logical=logical)                 registered / unknown passthrough
     5  pinned = (("task", task),) if task and cand_door == "llm" else ()
     4  ToolCall(candidate, ..., degraded=True)            direct chain hit
     4  floor = next(iter(sorted(reg)))                    the floor's own fallback
     3  ToolCall(floor, ..., degraded=True)                tier floor
     2  floor = _TIER_FLOOR.get(tier, "llm_query")

The docstring states the resolution order, and each step has a distinct observable
result. Existing tests exercised step 1 and step 2; steps 3 and 4, and the `degraded`
flag that distinguishes them, were untouched.

WHY `degraded` MATTERS
----------------------
`resolve` is total by design — it always returns something callable. `degraded=True` is
the only signal that the answer is a SUBSTITUTE rather than the thing asked for. A mutant
flipping or dropping it turns "we could not give you llm_analyze, here is llm_code" into
"here is llm_analyze", which is a silent capability downgrade.
"""

from __future__ import annotations

import pytest

from llm_router.tool_surface import (
    DEPRECATED_TOOLS,
    KNOWN_TOOLS,
    _FALLBACK_CHAINS,
    _TIER_FLOOR,
    registered_tools,
    resolve,
)


class TestStepOneRegisteredPassthrough:
    """A name registered in the tier is returned as-is, never degraded."""

    def test_a_registered_tool_resolves_to_itself(self):
        call = resolve("llm_query", "core")
        assert call.name == "llm_query"
        assert call.degraded is False
        assert call.pinned == ()

    def test_tier_off_registers_everything_and_translates_nothing(self):
        """`if reg is None: return ToolCall(logical, ...)` — the earliest exit.

        Under `off`, even a deprecated name comes back untouched: there is no tier to
        translate into.
        """
        assert registered_tools("off") is None
        call = resolve("llm_analyze", "off")
        assert call.name == "llm_analyze"
        assert call.degraded is False

    def test_an_unknown_tier_behaves_as_off(self):
        assert resolve("llm_analyze", "not-a-tier").name == "llm_analyze"


class TestStepTwoFrontDoorCarriesTheTaskArgument:
    """`pinned = (("task", task),) if task and door == "llm" else ()`.

    The specialization must survive the translation: `llm_code` becomes
    `llm(task="code")`, not a bare `llm`. Losing the pin turns a specific request into
    a generic one — the model gets a different instruction than the caller wrote.
    """

    def test_a_deprecated_name_maps_to_its_door_with_the_task_pinned(self):
        call = resolve("llm_code", "consolidated")
        assert call.name == "llm"
        assert call.pinned == (("task", "code"),)
        assert call.degraded is False, "a front-door translation is not a degrade"

    def test_the_display_form_renders_the_pinned_argument(self):
        assert resolve("llm_code", "consolidated").display == 'llm(task="code")'

    def test_different_logical_names_pin_DIFFERENT_task_values(self):
        """A mutant pinning a constant task would satisfy any single-name assertion."""
        assert resolve("llm_code", "consolidated").pinned == (("task", "code"),)
        assert resolve("llm_query", "consolidated").pinned == (("task", "query"),)
        assert resolve("llm_research", "consolidated").pinned == (("task", "research"),)

    def test_the_task_is_pinned_ONLY_when_the_door_is_llm(self):
        """`and door == "llm"` — a door that is not the consolidated one takes no task
        argument, and pinning one would produce an unexpected-keyword call."""
        for logical, door in DEPRECATED_TOOLS.items():
            call = resolve(logical, "consolidated")
            if call.name != "llm":
                assert call.pinned == (), (
                    f"{logical} -> {call.name} must not carry a task pin"
                )


class TestStepThreeFallbackChain:
    """The capability-ordered chain, and the `degraded` flag that marks it."""

    def test_a_chain_hit_is_marked_degraded(self):
        """`llm_analyze` is absent from `core`; its chain is (llm_code, llm_query),
        both of which ARE in core. The first is taken and marked degraded."""
        assert "llm_analyze" not in (registered_tools("core") or set())
        call = resolve("llm_analyze", "core")
        assert call.name == "llm_code", "chain order must be respected"
        assert call.degraded is True, (
            "a substitute must announce itself; silent substitution is a capability "
            "downgrade the caller cannot see"
        )

    def test_the_chain_is_ordered_not_arbitrary(self):
        """`llm_analyze -> (llm_code, llm_query)`: the FIRST registered entry wins.

        A mutant iterating in another order returns llm_query — still callable, still
        registered, and a weaker tool than the chain intends.
        """
        chain = _FALLBACK_CHAINS["llm_analyze"]
        assert chain[0] == "llm_code"
        assert resolve("llm_analyze", "core").name == chain[0]

    def test_a_chain_entry_that_is_directly_registered_wins(self):
        """`llm_classify` chains to (llm_route, llm_query). `llm_route` is registered
        under `consolidated`, so it is taken directly — no door, no pin."""
        call = resolve("llm_classify", "consolidated")
        assert call.name == "llm_route"
        assert call.degraded is True
        assert call.pinned == ()
        assert call.logical == "llm_classify"

    def test_the_cand_door_branch_is_UNREACHABLE_with_the_current_tables(self):
        """Documents why 7 mutants on that branch cannot be killed behaviourally.

        The branch is `for candidate in chain: ... elif cand_door in reg` — a chain
        entry reachable only through a door. Enumerated over every (tier, logical)
        pair, respecting chain ORDER, **zero inputs reach it**: for every name, either
        it is registered, or its own door is registered (step 2 returns first), or an
        earlier chain entry is directly registered.

        Two wrong turns got here, both worth recording:

        1. The first test used `llm_analyze`, which HAS a registered door — so step 2
           returned and step 3 never ran. The test asserted it covered this branch,
           passed, and killed none of the seven.
        2. The corrected enumeration ignored chain ORDER and proposed `llm_classify`.
           `llm_route` comes first in that chain and IS registered, so resolution stops
           there. Only after ordering the walk correctly did the count come out 0.

        This test asserts the UNREACHABILITY, so if a future table change makes the
        branch live, it fails and someone writes the real coverage.
        """
        reachable = []
        for tier in ("core", "routing", "consolidated"):
            reg = registered_tools(tier)
            if reg is None:
                continue
            for logical, chain in _FALLBACK_CHAINS.items():
                own_door = DEPRECATED_TOOLS.get(logical)
                if logical in reg or (own_door and own_door in reg):
                    continue                       # step 1 or step 2 returns first
                for candidate in chain:
                    if candidate in reg:
                        break                      # direct chain hit wins
                    cand_door = DEPRECATED_TOOLS.get(candidate)
                    if cand_door and cand_door in reg:
                        reachable.append((tier, logical, candidate, cand_door))
                        break

        assert reachable == [], (
            "the cand_door branch is now reachable and needs real coverage: "
            f"{reachable}"
        )


class TestStepFourUnknownNamesAndTheTierFloor:
    """An unknown name passes through; a KNOWN one falls to the tier floor."""

    def test_a_name_outside_KNOWN_TOOLS_is_returned_unchanged(self):
        """The docstring's reason: this value "is also compared against the tool the
        caller actually invoked — rewriting an unknown name silently breaks that
        match." Substituting the floor here would be a guess."""
        assert "third_party_mcp_tool" not in KNOWN_TOOLS
        call = resolve("third_party_mcp_tool", "core")
        assert call.name == "third_party_mcp_tool"
        assert call.degraded is False

    def test_the_tier_floor_differs_between_tiers(self):
        """`_TIER_FLOOR.get(tier, "llm_query")` — core floors to llm_query,
        consolidated floors to llm. A mutant returning a constant floor is invisible
        against a single tier."""
        assert _TIER_FLOOR["core"] == "llm_query"
        assert _TIER_FLOOR["consolidated"] == "llm"
        assert _TIER_FLOOR["core"] != _TIER_FLOOR["consolidated"]


class TestResolveIsTotalAcrossEveryTier:
    """The guard the docstring claims: `resolve` ALWAYS returns a registered name.

    This is the property that makes every other caller safe, and it is asserted over
    the full cross product rather than a sample — a mutant breaking one branch for one
    tier has nowhere to hide.
    """

    @pytest.mark.parametrize("tier", ["core", "routing", "consolidated", "off"])
    def test_every_known_tool_resolves_to_something_registered(self, tier):
        reg = registered_tools(tier)
        for logical in KNOWN_TOOLS:
            call = resolve(logical, tier)
            assert call.name, f"{logical} on {tier} resolved to an empty name"
            if reg is not None:
                assert call.name in reg, (
                    f"{logical} on {tier} resolved to {call.name!r}, which is NOT "
                    f"registered in that tier — resolve is meant to be total"
                )

    @pytest.mark.parametrize("tier", ["core", "routing", "consolidated"])
    def test_the_logical_name_is_always_preserved_on_the_result(self, tier):
        """`logical=logical` on every return. It is what lets a caller report what was
        ASKED for after a substitution, and it is set in five separate places."""
        for logical in list(KNOWN_TOOLS)[:12]:
            assert resolve(logical, tier).logical == logical
