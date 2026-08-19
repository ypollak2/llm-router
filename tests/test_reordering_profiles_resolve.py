"""Every RoutingProfile must resolve to a usable chain, including the reordering ones.

WHY THIS EXISTS
===============

Two profiles have no chains of their own in ``policies/standard.yaml``. They are
reorderings applied to another profile's chain at a later stage:

    QUOTA_BALANCED      reordered in router._build_and_filter_chain()
    SUBSCRIPTION_LOCAL  reordered by subscription_local_routing

So a ``ROUTING_TABLE`` lookup keyed on either one MISSES. Three callers took a
caller-supplied profile straight to the table without substituting the base, and
each degraded differently. Measured on ``main`` before the fix, task_type=CODE:

    get_model_chain     balanced 6 · quota_balanced 6 · subscription_local 1
    _static_chain       balanced 7 · quota_balanced 0 · subscription_local 0

The `subscription_local 1` is the worst of the three and the least visible: not
an error, not an empty list, but the one-element default ``["anthropic/
claude-sonnet-4-6"]`` — a chain with no fallback whatsoever, consisting solely
of the paid seat, under the profile whose entire purpose is to prefer the free
local bucket over the paid seat. Nothing crashes. Routing just quietly does the
opposite of what the profile is for, and bills for it.

WHY IT ENUMERATES THE ENUM
==========================

The obvious version of this test names the two profiles. That version passes
forever and protects nothing: the defect is not that these two profiles were
wrong, it is that adding a profile without a chain table is an easy and silent
mistake. Iterating ``RoutingProfile`` means the test already covers the profile
somebody adds next year — it fails on the commit that adds it, naming the fix.

CONTROL (re-run this if the test is edited)
===========================================

With ``base_lookup_profile`` reverted to the identity function:

    test_every_profile_yields_a_usable_static_chain  FAILED  quota_balanced (0)
    test_every_profile_yields_a_usable_static_chain  FAILED  subscription_local (0)
    test_every_profile_yields_a_chain_with_a_fallback FAILED subscription_local (1)
    test_memory_profile_lookup_returns_a_model_id    FAILED  returns 'llm_code'

4 failures, one per call site plus the fallback assertion. With the fix, all pass.
"""

from __future__ import annotations

import pytest

from llm_router.chain_builder import _static_chain
from llm_router.profiles import ROUTING_TABLE, base_lookup_profile, get_model_chain
from llm_router.types import RoutingProfile, TaskType

#: A chain of one is not a chain — the whole point of the structure is that
#: there is somewhere to go when the first model fails.
_MIN_USABLE_CHAIN = 2

_ALL_PROFILES = list(RoutingProfile)


def test_the_enum_is_not_empty():
    """Guards the guard: a parametrisation over an empty enum passes vacuously."""
    assert len(_ALL_PROFILES) >= 4, (
        f"only {len(_ALL_PROFILES)} routing profiles found — if the enum moved, "
        f"every parametrised test below is silently covering nothing."
    )


@pytest.mark.parametrize("profile", _ALL_PROFILES, ids=lambda p: p.value)
def test_every_profile_yields_a_usable_static_chain(profile: RoutingProfile):
    """``chain_builder._static_chain`` is the last-resort path; it must not be empty.

    Its module docstring promises "Never empty (falls back to static)", and it
    is reached exactly when nothing else worked — discovery returned nothing, or
    the dynamic build raised. An empty list there is a routing outage.
    """
    chain = _static_chain(TaskType.CODE, profile)
    assert len(chain) >= _MIN_USABLE_CHAIN, (
        f"_static_chain(CODE, {profile.value}) returned {chain!r}.\n"
        f"If {profile.value} reorders another profile's chain rather than owning "
        f"one, add it to profiles._REORDERING_PROFILE_BASE. If it owns its "
        f"chains, add them to policies/standard.yaml."
    )


@pytest.mark.parametrize("profile", _ALL_PROFILES, ids=lambda p: p.value)
def test_every_profile_yields_a_chain_with_a_fallback(profile: RoutingProfile):
    """``get_model_chain`` must never degrade to the single-model default.

    A length of exactly 1 is the signature of the ``ROUTING_TABLE.get(key,
    ["anthropic/claude-sonnet-4-6"])` default firing — a miss that looks like a
    result.
    """
    chain = get_model_chain(profile, TaskType.CODE)
    assert len(chain) >= _MIN_USABLE_CHAIN, (
        f"get_model_chain({profile.value}, CODE) returned {chain!r}.\n"
        f"A one-element chain means the table lookup missed and the hardcoded "
        f"default answered for it — there is no fallback model at all."
    )


@pytest.mark.parametrize("profile", _ALL_PROFILES, ids=lambda p: p.value)
def test_base_lookup_profile_resolves_to_something_in_the_table(profile: RoutingProfile):
    """The resolved base must actually have entries, or the mapping is wrong."""
    base = base_lookup_profile(profile)
    entries = [k for k in ROUTING_TABLE if k[0] is base]
    assert entries, (
        f"base_lookup_profile({profile.value}) -> {base.value}, which has no "
        f"ROUTING_TABLE entries either. The mapping points at another profile "
        f"that does not own chains."
    )


def test_reordering_profiles_are_not_their_own_base():
    """A reordering profile mapped to itself is the bug, spelled differently."""
    from llm_router.profiles import _REORDERING_PROFILE_BASE

    for profile, base in _REORDERING_PROFILE_BASE.items():
        assert profile is not base, (
            f"{profile.value} is mapped to itself, which resolves to the same "
            f"missing key it started from."
        )


@pytest.mark.asyncio
async def test_memory_profile_lookup_returns_a_model_id(monkeypatch):
    """``memory/profiles.py`` returns the tool NAME when the lookup misses.

    That failure is silent in a different way from the others: the caller gets a
    plausible-looking string (`"llm_code"`) where a `"provider/model"` id
    belongs, so it propagates instead of raising.
    """
    from llm_router import state
    from llm_router.memory import profiles as memory_profiles

    monkeypatch.setattr(
        state, "get_active_profile", lambda: RoutingProfile.SUBSCRIPTION_LOCAL
    )
    resolved = await memory_profiles.get_primary_model_for_tool("llm_code")
    assert resolved != "llm_code", (
        "get_model_for_tool fell through to returning the tool name, which "
        "means the ROUTING_TABLE lookup missed for SUBSCRIPTION_LOCAL."
    )
    assert "/" in resolved, (
        f"expected a provider/model id, got {resolved!r}"
    )
