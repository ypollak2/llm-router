"""LLM_ROUTER_TIER_COLD_CACHE_ONLY (D-643): a pinned Opus conversation is rewritten to Haiku by the D-31
arm only when Opus's cache for the conversation is cold. The classifier is stubbed; no network.
A warm cache is built the way the proxy builds it: an earlier pinned call of the same conversation
that stayed on Opus (noted by ``decide``). Fixture text is short and synthetic."""
from __future__ import annotations

import pytest

from llm_router.proxy import haiku_arm as arm
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness
from tests.test_proxy_haiku_arm import HAIKU, OPUS, TURN3, SHARE, _body, _classify, _policy

FLAG = "LLM_ROUTER_TIER_COLD_CACHE_ONLY"


def _sid() -> str:
    """In the arm's bucket at turn 5 (the third human turn), out of it at turn 3 (the second)."""
    for i in range(10_000):
        sid = f"cc-{i}"
        if arm.bucket(sid, 5) < SHARE and arm.bucket(sid, 3) >= SHARE:
            return sid
    raise AssertionError("no session found")


SID = _sid()


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


async def _prior_pinned_call(policy, sticky):
    """Turn 2 of the conversation: out of the arm's bucket, so it stays on Opus (and re-arms its cache)."""
    d = await policy.decide(_body(*TURN3[:2], sid=SID), SID, sticky, classify=_classify())
    assert (d.reason, d.served_model) == (pt.REASON_CONFIG_PINNED, OPUS)


async def _arm_turn(policy, sticky):
    return await policy.decide(_body(*TURN3, sid=SID), SID, sticky, classify=_classify())


async def test_flag_off_warm_cache_is_still_rewritten(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    policy, sticky = _policy(), Stickiness()
    await _prior_pinned_call(policy, sticky)
    d = await _arm_turn(policy, sticky)
    assert (d.reason, d.served_model, d.arm_assignment) == (pt.REASON_HAIKU_REWRITE, HAIKU, arm.ASSIGNED)


async def test_flag_on_warm_cache_keeps_opus_and_says_why(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    policy, sticky = _policy(), Stickiness()
    await _prior_pinned_call(policy, sticky)
    d = await _arm_turn(policy, sticky)
    assert (d.served_model, d.reason, d.switched, d.body_rewrite) == (OPUS, pt.REASON_CONFIG_PINNED, False, None)
    assert (d.arm_assignment, d.arm_reason) == (arm.INELIGIBLE, "tier_skip_warm_cache")


async def test_flag_on_cold_cache_is_rewritten(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    clock = Clock()
    policy, sticky = _policy(), Stickiness(clock=clock)
    await _prior_pinned_call(policy, sticky)
    clock.t += sticky.cold_gap_s + 1  # the 1-hour cache has expired
    d = await _arm_turn(policy, sticky)
    assert (d.reason, d.served_model, d.arm_assignment) == (pt.REASON_HAIKU_REWRITE, HAIKU, arm.ASSIGNED)


async def test_flag_on_unseen_mid_conversation_counts_as_warm(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    d = await _arm_turn(_policy(), Stickiness())  # e.g. the proxy restarted: no record of this conversation
    assert (d.served_model, d.arm_reason) == (OPUS, "tier_skip_warm_cache")


async def test_a_rewritten_call_does_not_re_arm_the_opus_cache(monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    clock = Clock()
    policy, sticky = _policy(), Stickiness(clock=clock)
    await _prior_pinned_call(policy, sticky)
    clock.t += sticky.cold_gap_s + 1
    assert (await _arm_turn(policy, sticky)).served_model == HAIKU  # rewrite: Opus not served, not noted
    key = pt.conversation_key(_body(*TURN3, sid=SID), SID)
    assert sticky.served_warm(key, OPUS, unseen_warm=False) is False


def test_flag_default_is_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    from llm_router.proxy.cache_cost import cold_cache_only
    assert cold_cache_only() is False


@pytest.mark.parametrize("val", ["1", "on", "true", "YES"])
def test_flag_truthy_values(monkeypatch, val):
    monkeypatch.setenv(FLAG, val)
    from llm_router.proxy.cache_cost import cold_cache_only
    assert cold_cache_only() is True
