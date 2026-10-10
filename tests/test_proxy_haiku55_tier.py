"""The Haiku tier on claude-haiku-5-5 (docs/BUGS.md HAIKU55-TIER-1).

Every capability asserted here is from platform.claude.com, fetched 2026-10-10:
models/haiku-5-5/overview (context 1M, max output 128K, pricing 100K line),
models/haiku-5-5/migration-guide (thinking `enabled` 400s, sampling parameters and prefill
400, tokenizer ~30% more tokens), build-with-claude/thinking-troubleshooting (Haiku 5.5:
adaptive only, "disabled" at effort high or below), build-with-claude/effort (all five
levels), build-with-claude/mid-conversation-system-messages (Haiku 5.5 listed).
The 4.5 behaviour must stay exactly as it was: tests/test_proxy_tiers.py and
tests/test_proxy_haiku_fold.py still run against the shipped claude-haiku-4-5 policy.
"""
from __future__ import annotations

import pytest

from llm_router.proxy import tiers as pt
from llm_router.proxy.translate import HAIKU55_MAX_OUTPUT_TOKENS, HAIKU_MAX_OUTPUT_TOKENS, for_haiku
from tests.test_proxy_haiku_fold import SHAPES
from tests.test_proxy_tiers import SID, Stickiness, _classify, _raw_policy, _turns

H55, H45 = "claude-haiku-5-5", "claude-haiku-4-5"
SONNET = "claude-sonnet-5-5"


def _policy55(**over):
    raw = _raw_policy()
    raw["tiers"][0] = {"name": "haiku", "model": H55, "thinking": ["adaptive"]}
    raw["haiku_rewrite"] = True
    raw["haiku_fold_system"] = True  # must have no effect on 5.5
    raw.update(over)
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=True)


def _eligible_body() -> dict:
    """Claude Code shape: adaptive thinking, effort, the mid-conversation system message."""
    body = _turns("rename foo")
    body["thinking"] = {"type": "adaptive"}
    body["output_config"] = {"effort": "high"}
    body["messages"] = [body["messages"][0],
                        {"role": "system", "content": "reminder", "output_config": {"effort": "low"}}]
    return body


# ── for_haiku ────────────────────────────────────────────────────────────────

def test_for_haiku_55_keeps_adaptive_thinking_effort_and_the_system_role():
    body = _eligible_body()
    body["max_tokens"] = 32_000
    assert for_haiku(body, fold_system=True, model=H55) == body


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_for_haiku_55_never_folds(name):
    assert for_haiku(SHAPES[name], fold_system=True, model=H55)["messages"] == SHAPES[name]["messages"]


def test_for_haiku_55_turns_budget_thinking_into_adaptive_keeping_context_edits_and_clamps_to_128k():
    body = _eligible_body()
    body["thinking"] = {"type": "enabled", "budget_tokens": 8000}
    body["context_management"] = {"edits": [{"type": "clear_thinking_20251015"}]}
    body["max_tokens"] = 200_000
    out = for_haiku(body, model=H55)
    assert out["thinking"] == {"type": "adaptive"} and out["output_config"] == {"effort": "high"}
    assert out["context_management"] == body["context_management"]
    assert out["max_tokens"] == HAIKU55_MAX_OUTPUT_TOKENS == 128_000
    assert body["thinking"]["type"] == "enabled"  # input untouched (kept for the 4xx retry)


@pytest.mark.parametrize("model", [None, H45, "claude-haiku-4-5-20251001"])
def test_for_haiku_without_55_is_the_45_rewrite(model):
    body = _eligible_body()
    body["max_tokens"] = 100_000
    out = for_haiku(body, model=model)
    assert "thinking" not in out and "output_config" not in out
    assert out["max_tokens"] == HAIKU_MAX_OUTPUT_TOKENS == 64_000


# ── haiku_block_reason ───────────────────────────────────────────────────────

def test_55_takes_the_system_role_45_does_not():
    body = _eligible_body()
    assert pt.haiku_block_reason(body, model=H55) == "none"
    assert pt.haiku_block_reason(body, model=H45) == "system_message"
    assert pt.haiku_block_reason(body) == "system_message"


@pytest.mark.parametrize("extra", [{"temperature": 0.7}, {"top_p": 0.9}, {"top_p": 1}, {"top_k": 5}])
def test_55_rejected_sampling_parameters_block(extra):
    assert pt.haiku_block_reason(dict(_eligible_body(), **extra), model=H55) == "params"
    assert pt.haiku_block_reason(dict(_eligible_body(), **extra), model=H45) == "system_message"


@pytest.mark.parametrize("extra", [{"temperature": 1}, {"top_p": 0.99}])
def test_55_accepted_sampling_values_do_not_block(extra):
    assert pt.haiku_block_reason(dict(_eligible_body(), **extra), model=H55) == "none"


def test_55_assistant_prefill_blocks():
    body = _eligible_body()
    body["messages"] = [body["messages"][0], {"role": "assistant", "content": "{"}]
    assert pt.haiku_block_reason(body, model=H55) == "params"


def _body_of(est_tokens: int, **extra) -> dict:
    """A later call of the conversation `_eligible_body()` opens: a short first prompt, then a
    big user turn (so the first-call rules are off), the system reminder last."""
    body = _eligible_body()
    first, sysmsg = body["messages"]
    body["messages"] = [first, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                        {"role": "user", "content": [{"type": "text", "text": "x" * 4 * est_tokens}]}, sysmsg]
    body.update(dict({"max_tokens": 128_000}, **extra))
    return body


async def _later(policy, body):
    sticky = Stickiness()
    opener = _eligible_body()
    opener["messages"] = opener["messages"][:1]
    await policy.decide(opener, SID, sticky, classify=_classify("simple"))  # served on Haiku 5.5
    return await policy.decide(body, SID, sticky, classify=_classify("simple"))


def _no_system(body: dict) -> dict:
    return dict(body, messages=[m for m in body["messages"] if m["role"] != "system"])


def test_context_limit_is_per_model_and_the_55_one_guarantees_the_window():
    from llm_router.proxy.tiers import haiku55_context_limit
    assert pt.HAIKU55_MAX_CONTEXT_TOKENS == 670_769 == haiku55_context_limit({})
    for mt in (None, 1_000, 32_000, 128_000):
        lim = haiku55_context_limit({"max_tokens": mt} if mt else {})
        assert lim * 1.3 + (mt or 128_000) <= 1_000_000 < (lim + 2) * 1.3 + (mt or 128_000)  # the largest that fits
    assert haiku55_context_limit({"max_tokens": 1_000}) > haiku55_context_limit({"max_tokens": 128_000})
    # 4.5 keeps its own 150K limit, 5.5 takes what 4.5 refuses
    big = _body_of(200_000)
    assert pt.haiku_block_reason(_no_system(big), model=H45) == "context"
    assert pt.haiku_block_reason(big, model=H55) == "none"


async def test_just_under_the_limit_goes_to_55_just_over_goes_up_a_tier():
    from llm_router.proxy.tiers import haiku55_context_limit
    limit = haiku55_context_limit({"max_tokens": 4096})
    fixed = pt._approx_context_tokens(_body_of(0, max_tokens=4096))  # system prompt, tools, JSON framing
    under = _body_of(limit - fixed - 50, max_tokens=4096)
    over = _body_of(limit - fixed + 50, max_tokens=4096)
    assert pt._approx_context_tokens(under) < limit < pt._approx_context_tokens(over)
    assert (await _later(_policy55(), under)).served_model == H55
    d = await _later(_policy55(), over)
    assert (d.served_model, d.reason) == (SONNET, pt.REASON_HAIKU_BODY)
    assert pt.haiku_block_reason(over, model=H55) == "context"


async def test_the_arm_uses_the_same_per_model_limit():
    policy = _policy55(haiku_arm_share=1.0)
    assert policy._haiku_body_ok(_body_of(200_000)) is True   # over 4.5's 150K, inside 5.5's
    assert policy._haiku_body_ok(_body_of(700_000)) is False
    legacy = pt.ClaudeTierPolicy.from_dict(_raw_policy(), conversation_level=True)
    assert legacy._haiku_body_ok(_no_system(_body_of(100_000))) is True
    assert legacy._haiku_body_ok(_no_system(_body_of(200_000))) is False


# ── the decision ─────────────────────────────────────────────────────────────

async def _decide(policy, body):
    return await policy.decide(body, SID, Stickiness(), classify=_classify("simple"))


async def test_simple_claude_code_turn_is_served_on_55_as_sent():
    d = await _decide(_policy55(), _eligible_body())
    assert (d.served_model, d.tier, d.body_rewrite) == (H55, "haiku", None)  # no rewrite, so no fold, effort kept


async def test_55_without_the_rewrite_flag_still_takes_what_it_accepts_natively():
    d = await _decide(_policy55(haiku_rewrite=False), _eligible_body())
    assert (d.served_model, d.body_rewrite) == (H55, None)


@pytest.mark.parametrize("mutate, why", [
    (lambda b: b.update(temperature=0.2), "params"),
    (lambda b: b["messages"][0].update(content=[{"type": "text", "text": "x" * 3_200_000}]), "context"),
    (lambda b: b["tools"].append({"type": "web_search_20250305", "name": "web_search"}), "builtin_tools"),
])
async def test_a_body_55_cannot_take_moves_up_even_though_thinking_and_effort_are_accepted(mutate, why):
    policy = _policy55()
    body = _eligible_body()
    mutate(body)
    d = await _decide(policy, body)
    assert (d.served_model, d.reason, d.body_rewrite) == (SONNET, pt.REASON_HAIKU_BODY, None)
    assert pt.haiku_block_reason(body, model=H55) == why


async def test_budget_thinking_on_55_is_rewritten_not_sent_as_is():
    body = _eligible_body()
    body["thinking"] = {"type": "enabled", "budget_tokens": 4000}
    d = await _decide(_policy55(), body)
    assert (d.served_model, d.reason, d.body_rewrite) == (H55, pt.REASON_HAIKU_REWRITE, pt.REWRITE_HAIKU)


async def test_the_45_policy_is_unchanged_by_55_support():
    raw = _raw_policy()
    assert raw["tiers"][0]["model"] == H45
    policy = pt.ClaudeTierPolicy.from_dict(dict(raw, haiku_rewrite=True, haiku_fold_system=True), conversation_level=True)
    assert policy.haiku_model == H45 and policy.haiku_folds_system is True
    body = _eligible_body()  # adaptive thinking + effort: 4.5 takes neither, so the rewrite runs
    d = await policy.decide(body, SID, Stickiness(), classify=_classify("simple"))
    assert (d.served_model, d.body_rewrite) == (H45, pt.REWRITE_HAIKU)
    assert _policy55().haiku_folds_system is False


# ── repair round 1 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("extra", [
    {"temperature": 1, "top_p": 0.99},                       # both set is a 400 even at the allowed values
    {"thinking": {"type": "between_tools"}},                  # Sonnet 5.5 only
    {"thinking": {"type": "disabled"}, "output_config": {"effort": "max"}},
    {"thinking": {"type": "disabled"}, "output_config": {"effort": "xhigh"}},
    {"max_tokens": 128_001},
    # per-message effort ("low" in the body) differs from the level in effect, thinking disabled
    {"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}},
    {"thinking": {"type": "disabled"}},  # default medium vs the per-message "low"
])
def test_55_more_rejected_shapes_block(extra):
    assert pt.haiku_block_reason(dict(_eligible_body(), **extra), model=H55) == "params"


@pytest.mark.parametrize("extra", [
    {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}},  # matches the body's per-message "low"
    {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}},  # adaptive may vary effort per turn
    {"thinking": {"type": "enabled", "budget_tokens": 2000}},   # rewritten to adaptive, not blocked
    {"max_tokens": 128_000},
])
def test_55_accepted_shapes_do_not_block(extra):
    assert pt.haiku_block_reason(dict(_eligible_body(), **extra), model=H55) == "none"


@pytest.mark.parametrize("extra", [{"thinking": {"type": "between_tools"}}, {"max_tokens": 200_000}])
@pytest.mark.parametrize("rewrite", [True, False])
async def test_55_blocked_shapes_move_up_with_the_rewrite_flag_on_or_off(extra, rewrite):
    d = await _decide(_policy55(haiku_rewrite=rewrite), dict(_eligible_body(), **extra))
    assert (d.served_model, d.reason) == (SONNET, pt.REASON_HAIKU_BODY)


def test_haiku_max_output_tokens_is_the_per_model_clamp():
    from llm_router.proxy.translate import haiku_max_output_tokens
    assert (haiku_max_output_tokens(H55), haiku_max_output_tokens(H45), haiku_max_output_tokens(None)) == (128_000, 64_000, 64_000)
