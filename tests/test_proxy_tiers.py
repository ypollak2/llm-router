"""Claude-tier rewrite in the per-call proxy (llm_router.proxy.tiers, cache_cost).

Anthropic is a mocked upstream (httpx.MockTransport) and the classifier is
stubbed, so no test makes a network call. The request fixture is the recorded
Claude Code continuation request test_proxy.py uses.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from llm_router.proxy import backends as pb
from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness, conversation_key, switch_cost_usd
from llm_router.proxy.steps import is_first_call, tier_text, user_pinned_model

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
SID = "11111111-2222-3333-4444-555555555555"
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"


def _req(model=OPUS, thinking="adaptive") -> dict:
    body = json.loads(FIXTURE.read_text())
    body["model"] = model
    if thinking is None:
        body.pop("thinking", None)
    else:
        body["thinking"] = {"type": thinking}
    return body


def _first(model=OPUS, thinking="adaptive") -> dict:
    body = _req(model, thinking)
    body["messages"] = body["messages"][:1]
    return body


def _classify(cx="moderate", task="code"):
    async def fn(text):
        return {"task_type": task, "complexity": cx, "chain_head": ["ollama/x"], "model": None}
    return fn


@pytest.fixture
def policy():
    """The bundled policy with the post-first-call handoff on, so a
    conversation reaches its policy tier on call 2 (most tests need that)."""
    raw = _raw_policy()
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
    return pt.ClaudeTierPolicy.from_dict(raw)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ── policy file ──────────────────────────────────────────────────────────────


def test_bundled_policy_maps_every_owner_tier_and_ids_come_from_the_yaml():
    policy = pt.ClaudeTierPolicy.load()
    assert policy.switch_after_first_call is False  # measured net negative; opt-in
    assert [t.name for t in policy.tiers] == ["haiku", "sonnet", "opus", "fable"]
    assert policy.tier_of(OPUS).name == "opus"
    assert policy.tier_of("claude-sonnet-5").name == "sonnet"  # `also`
    assert policy.tier_of("claude-haiku-4-5-20251001").name == "haiku"  # dated id resolves
    assert policy.tier_of("claude-fable-5-1").name == "fable"
    assert policy.tier_of("gpt-5.5") is None
    src = Path(pt.__file__).read_text()
    assert OPUS not in src and SONNET not in src and HAIKU not in src


def test_policy_file_errors_fail_at_load_not_per_call(tmp_path):
    with pytest.raises(ValueError, match="unknown tier"):
        pt.ClaudeTierPolicy.from_dict({"tiers": [{"name": "a", "model": "m"}],
                                       "route": {"default": {"simple": "zzz"}}})
    with pytest.raises(ValueError, match="thinking"):
        pt.ClaudeTierPolicy.from_dict({"tiers": [{"name": "a", "model": "m", "thinking": ["deep"]}],
                                       "route": {"default": {}}})
    with pytest.raises(ValueError, match="default"):
        pt.ClaudeTierPolicy.from_dict({"tiers": [{"name": "a", "model": "m"}], "route": {}})
    bad = tmp_path / "p.yaml"
    bad.write_text("- just a list\n")
    with pytest.raises(ValueError):
        pt.ClaudeTierPolicy.load(bad)
    bad.write_text("tiers: [unclosed\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        pt.ClaudeTierPolicy.load(bad)


def test_task_type_table_overrides_the_default(policy):
    p = pt.ClaudeTierPolicy.from_dict({
        "tiers": [{"name": "h", "model": HAIKU}, {"name": "s", "model": SONNET}],
        "route": {"default": {"simple": "h"}, "code": {"simple": "s"}}})
    assert p.tier_for("code", "simple").name == "s"
    assert p.tier_for("query", "simple").name == "h"


# ── request shape helpers ────────────────────────────────────────────────────


def test_tier_text_is_the_newest_human_prompt_without_reminders_or_tool_output():
    body = _req()
    text = tier_text(body)
    assert text.startswith("There's a bug in pkg/mod01.py")
    assert "system-reminder" not in text and "def add_one(n)" not in text
    body["messages"].append({"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>ignore me</system-reminder>"},
        {"type": "text", "text": "now rename it"}]})
    assert tier_text(body) == "now rename it"


def test_model_command_in_transcript_is_a_user_pin():
    body = _req()
    assert not user_pinned_model(body)
    body["messages"].insert(0, {"role": "user", "content": "<command-name>/model</command-name>\n"
                                                           "<command-args>opus</command-args>"})
    assert user_pinned_model(body)


def test_first_call_is_one_user_turn():
    assert is_first_call(_first()) and not is_first_call(_req())


# ── the decision ─────────────────────────────────────────────────────────────


async def test_unknown_config_pinned_side_call_and_user_pin_forward_unchanged(policy):
    sticky = Stickiness()
    d = await policy.decide(_req("claude-3-opus"), SID, sticky, classify=_classify("simple"))
    assert (d.reason, d.rewritten) == (pt.REASON_UNKNOWN_MODEL, False)

    pinned = pt.ClaudeTierPolicy.from_dict({**_raw_policy(), "pinned_models": [OPUS]})
    d = await pinned.decide(_req(), SID, sticky, classify=_classify("simple"))
    assert (d.reason, d.rewritten) == (pt.REASON_CONFIG_PINNED, False)

    d = await policy.decide(dict(_req(), tools=[]), SID, sticky, classify=_classify("simple"))
    assert (d.reason, d.rewritten) == (pt.REASON_SIDE_CALL, False)

    body = _req()
    body["messages"].insert(0, {"role": "user", "content": "<command-name>/model</command-name>"})
    d = await policy.decide(body, SID, sticky, classify=_classify("simple"))
    assert (d.reason, d.rewritten) == (pt.REASON_USER_PINNED, False)


def _raw_policy() -> dict:
    import yaml

    return yaml.safe_load(pt.DEFAULT_POLICY_PATH.read_text())


async def test_first_call_stays_then_the_next_call_moves_to_the_policy_tier(policy):
    sticky = Stickiness()
    first = await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    assert (first.reason, first.served_model) == (pt.REASON_FIRST_CALL, OPUS)
    sticky.record_usage(conversation_key(_first(), SID),
                        {"input_tokens": 10, "cache_read_input_tokens": 30_000, "cache_creation_input_tokens": 9_990})
    d = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d.reason, d.served_model, d.tier, d.switched) == (pt.REASON_POLICY, SONNET, "sonnet", True)
    assert d.switch_cost_usd == switch_cost_usd(SONNET, 40_000) and d.switch_cost_usd > 0


async def test_by_default_a_conversation_stays_on_its_first_call_model_until_cold():
    """Default policy: no post-first-call handoff, so a single-prompt task
    never pays a mid-task cache re-write; the next cold point may switch."""
    policy = pt.ClaudeTierPolicy.load()
    clock = Clock()
    sticky = Stickiness(cold_gap_s=policy.cold_gap_s, clock=clock)
    await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    d = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d.served_model, d.reason, d.switched) == (OPUS, pt.REASON_STICKY, False)
    clock.t += policy.cold_gap_s + 1
    d = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d.served_model, d.switched, d.switch_cost_usd) == (SONNET, True, 0.0)


async def test_unseen_mid_conversation_stays_on_requested_model(policy):
    """After a proxy restart the conversation is unknown: it must not switch at
    once (that is the measured net-negative mid-task switch)."""
    sticky = Stickiness()
    d = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d.served_model, d.reason, d.switched) == (OPUS, pt.REASON_STICKY, False)


async def test_same_class_sticks_and_a_class_change_switches(policy):
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))  # -> sonnet
    again = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (again.served_model, again.switched) == (SONNET, False)
    # complex -> opus is a class change, so the switch is allowed
    up = await policy.decide(_req(), SID, sticky, classify=_classify("complex"))
    assert (up.served_model, up.switched, up.reason) == (OPUS, True, pt.REASON_POLICY)


async def test_warm_cache_holds_a_tier_the_policy_would_now_leave(policy):
    """Same class, warm cache: the conversation stays where it was even if the
    policy table now points elsewhere (e.g. the file changed mid-session)."""
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))  # sonnet
    changed = pt.ClaudeTierPolicy.from_dict({**_raw_policy(), "route": {"default": {"moderate": "opus"}}})
    d = await changed.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d.served_model, d.reason, d.switched) == (SONNET, pt.REASON_STICKY, False)


async def test_cold_gap_allows_a_switch_at_no_extra_cache_cost(policy):
    clock = Clock()
    sticky = Stickiness(cold_gap_s=3600, clock=clock)
    await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))  # sonnet
    changed = pt.ClaudeTierPolicy.from_dict({**_raw_policy(), "route": {"default": {"moderate": "opus"}}})
    clock.t += 3601
    d = await changed.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d.served_model, d.switched, d.switch_cost_usd) == (OPUS, True, 0.0)


async def test_never_upgrades_above_the_requested_tier(policy):
    sticky = Stickiness()
    await policy.decide(_first(SONNET), SID, sticky, classify=_classify("complex"))
    d = await policy.decide(_req(SONNET), SID, sticky, classify=_classify("complex"))
    assert (d.served_model, d.rewritten) == (SONNET, False)


async def test_thinking_type_raises_the_tier_to_one_that_accepts_it(policy):
    sticky = Stickiness()
    await policy.decide(_first(thinking="adaptive"), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(_req(thinking="adaptive"), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason) == (SONNET, pt.REASON_THINKING_FLOOR)

    sticky = Stickiness()
    body = _req(thinking="enabled")
    body.pop("output_config")
    first = copy.deepcopy(body)
    first["messages"] = first["messages"][:1]
    await policy.decide(first, SID, sticky, classify=_classify("simple"))
    d = await policy.decide(body, SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason) == (HAIKU, pt.REASON_POLICY)

    sticky = Stickiness()
    await policy.decide(_first(thinking=None), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(_req(thinking=None), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason) == (SONNET, pt.REASON_THINKING_FLOOR)  # the fixture sets effort

    def no_effort(body):
        body.pop("output_config", None)
        return body

    sticky = Stickiness()
    await policy.decide(no_effort(_first(thinking=None)), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(no_effort(_req(thinking=None)), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason) == (HAIKU, pt.REASON_POLICY)


async def test_subagent_with_its_own_first_turn_keeps_its_own_state(policy):
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))  # main -> sonnet
    sub = _req()
    sub["messages"][0] = {"role": "user", "content": [{"type": "text", "text": "sub-agent brief"}]}
    assert conversation_key(sub, SID) != conversation_key(_req(), SID)
    sub_first = copy.deepcopy(sub)
    sub_first["messages"] = sub_first["messages"][:1]
    d = await policy.decide(sub_first, SID, sticky, classify=_classify("moderate"))
    assert (d.reason, d.served_model) == (pt.REASON_FIRST_CALL, OPUS)


# ── conversation-level mode (Phase 1.2b, --tiers conversation) ──────────────


def _conversation_policy(**overrides):
    raw = {**_raw_policy()}
    raw["stickiness"] = dict(raw.get("stickiness") or {}, **overrides)
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=True)


async def test_conversation_mode_classifies_and_rewrites_the_first_call():
    """The whole point of 1.2b: unlike per-turn mode, the first call is not
    exempt -- it IS the conversation-level decision, and it is not billed as
    a 'switch' (nothing was cached yet to re-write)."""
    policy = _conversation_policy()
    sticky = Stickiness()
    d = await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    assert (d.reason, d.served_model, d.tier) == (pt.REASON_POLICY, SONNET, "sonnet")
    assert d.switched is False and d.switch_cost_usd is None


def _no_thinking(body: dict) -> dict:
    """A request haiku can actually serve: no thinking type and no effort, so
    the thinking floor never overrides a target haiku decision."""
    body = copy.deepcopy(body)
    body.pop("thinking", None)
    body.pop("output_config", None)
    return body


async def test_conversation_mode_never_downgrades_back_once_committed():
    """A later prompt that classifies cheaper than the conversation's opening
    tier does not pull it back down -- only a cold point or a genuine rise
    does (module docstring: 'escalate ... only ... when the complexity class
    clearly rises')."""
    policy = _conversation_policy()
    sticky = Stickiness()
    await policy.decide(_no_thinking(_first()), SID, sticky, classify=_classify("moderate"))  # -> sonnet
    d = await policy.decide(_no_thinking(_req()), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason, d.switched) == (SONNET, pt.REASON_STICKY, False)


async def test_conversation_mode_escalates_on_a_clear_complexity_rise():
    policy = _conversation_policy()
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))  # -> sonnet
    d = await policy.decide(_req(), SID, sticky, classify=_classify("complex"))  # rises -> opus
    assert (d.served_model, d.reason, d.switched) == (OPUS, pt.REASON_POLICY, True)


async def test_conversation_mode_cold_point_still_allows_a_downward_move():
    """A cold point resets stickiness regardless of direction: unlike a live
    downgrade, there is no warm cache left to protect."""
    clock = Clock()
    policy = _conversation_policy()
    sticky = Stickiness(cold_gap_s=policy.cold_gap_s, clock=clock)
    await policy.decide(_no_thinking(_first()), SID, sticky, classify=_classify("moderate"))  # -> sonnet
    clock.t += policy.cold_gap_s + 1
    d = await policy.decide(_no_thinking(_req()), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason, d.switched, d.switch_cost_usd) == (HAIKU, pt.REASON_POLICY, True, 0.0)


async def test_conversation_mode_thinking_floor_still_applies_on_the_first_call():
    policy = _conversation_policy()
    sticky = Stickiness()
    d = await policy.decide(_first(thinking="enabled"), SID, sticky, classify=_classify("simple"))
    # Haiku (the only tier accepting `enabled` thinking) fails on the fixture's
    # `output_config.effort`, and no other tier accepts `enabled`: the floor
    # has nowhere to land, so the first call keeps the requested tier.
    assert (d.served_model, d.reason) == (OPUS, pt.REASON_THINKING_FLOOR)


async def test_per_turn_mode_is_unaffected_by_conversation_level_default(policy):
    """`policy` (the module fixture) is built with conversation_level unset
    (False): the first call stays exempt, exactly as PR #215 shipped it."""
    assert policy.conversation_level is False
    sticky = Stickiness()
    d = await policy.decide(_first(), SID, sticky, classify=_classify("moderate"))
    assert (d.reason, d.served_model) == (pt.REASON_FIRST_CALL, OPUS)


def test_classify_constructor_hook_is_used_when_no_call_override_is_given():
    """The Phase 2 kNN scorer hook: a policy built with its own `classify`
    is used without a server.py callsite change."""
    calls = []

    async def scorer(text):
        calls.append(text)
        return {"task_type": "code", "complexity": "moderate", "chain_head": []}

    raw = {**_raw_policy()}
    policy = pt.ClaudeTierPolicy.from_dict(raw, conversation_level=True, classify=scorer)
    import asyncio

    sticky = Stickiness()
    d = asyncio.run(policy.decide(_first(), SID, sticky))
    assert calls and d.served_model == SONNET


# ── backends widening ────────────────────────────────────────────────────────


async def test_anthropic_targets_are_valid_only_when_asked_for(monkeypatch):
    assert not pb.tool_capable("anthropic/claude-sonnet-5-5")
    assert pb.tool_capable("anthropic/claude-sonnet-5-5", anthropic=True)
    assert pb.tool_capable("ollama/q:1") and pb.tool_capable("ollama/q:1", anthropic=True)

    async def chain(text):
        return "code", "moderate", ["anthropic/claude-sonnet-5-5", "ollama/q:1"]

    monkeypatch.setattr(pb, "policy_chain", chain)
    assert (await pb.choose_model("x", None))["model"] == "ollama/q:1"  # local path unchanged
    assert (await pb.choose_model("x", None, anthropic=True))["model"] == "anthropic/claude-sonnet-5-5"


# ── server integration ───────────────────────────────────────────────────────


def _sse(model, msg_id="msg_up01", usage=None):
    usage = usage or {"input_tokens": 5, "cache_read_input_tokens": 30_000,
                      "cache_creation_input_tokens": 1_000,
                      "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000},
                      "output_tokens": 1}
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "usage": usage}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 40}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


class Upstream:
    """Mocked Anthropic that answers with whatever model the request named,
    or with the queued (status, body) responses first."""

    def __init__(self, responses=None):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses or [])

    def __call__(self, request):
        self.requests.append(request)
        if self.responses:
            status, body = self.responses.pop(0)
        else:
            status, body = 200, _sse(json.loads(request.content)["model"])
        ctype = "text/event-stream" if body.startswith(b"event:") else "application/json"
        return httpx.Response(status, stream=httpx.ByteStream(body), headers={"content-type": ctype})


def _app(tmp_path, upstream, *, tiers=ps.TIERS_ON, policy_overrides=None, **cfg):
    import yaml

    raw = _raw_policy()
    raw["stickiness"]["switch_after_first_call"] = True
    raw.update(policy_overrides or {})
    policy_file = tmp_path / "tiers.yaml"
    policy_file.write_text(yaml.safe_dump(raw))
    config = ps.ProxyConfig(steps=frozenset(), upstream="http://127.0.0.1:9", tiers=tiers,
                            tier_policy=str(policy_file), ledger_path=tmp_path / "proxy_calls.jsonl", **cfg)
    return ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))


async def _post(app, body):
    h = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=h)


def _rows(tmp_path):
    return ledger.read_rows(tmp_path / "proxy_calls.jsonl")


@pytest.fixture
def moderate(monkeypatch):
    async def choose(text, pinned, *, anthropic=False):
        assert anthropic is True
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)


async def test_rewrite_reaches_anthropic_with_tools_untouched_and_is_ledgered(tmp_path, moderate):
    up = Upstream()
    app = _app(tmp_path, up)
    assert (await _post(app, _first())).status_code == 200
    r = await _post(app, _req())
    assert r.status_code == 200 and SONNET.encode() in r.content  # the reply names the served model
    sent = [json.loads(q.content) for q in up.requests]
    assert [b["model"] for b in sent] == [OPUS, SONNET]
    original = _req()
    assert sent[1]["tools"] == original["tools"] and sent[1]["messages"] == original["messages"]
    assert {k: v for k, v in sent[1].items() if k != "model"} == {k: v for k, v in original.items() if k != "model"}
    first, second = _rows(tmp_path)
    assert (first["tier_reason"], first["served_model"], first["tier_switch"]) == ("first_call", OPUS, False)
    assert (second["requested_model"], second["served_model"], second["response_model"]) == (OPUS, SONNET, SONNET)
    assert (second["tier"], second["tier_reason"], second["tier_switch"]) == ("sonnet", "policy", True)
    assert second["tier_switch_cost_usd"] == switch_cost_usd(SONNET, 31_005)
    assert second["tier_complexity"] == "moderate"


async def test_rejected_rewrite_is_resent_unchanged_once(tmp_path, moderate):
    err = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                 "message": "effort is not supported"}}).encode()
    up = Upstream(responses=[(200, _sse(OPUS)), (400, err)])
    app = _app(tmp_path, up)
    await _post(app, _first())
    r = await _post(app, _req())
    assert r.status_code == 200
    assert [json.loads(q.content)["model"] for q in up.requests] == [OPUS, SONNET, OPUS]
    assert up.requests[2].content == json.dumps(_req()).encode()  # the client's own bytes
    row = _rows(tmp_path)[-1]
    assert row["tier_retry"]["status"] == 400 and "effort" in row["tier_retry"]["detail"]
    assert row["served_model"] == OPUS and row["response_model"] == OPUS
    assert row["tier_switch"] is False and row["tier_switch_cost_usd"] is None
    assert ledger.stats(_rows(tmp_path))["tiers"]["switches"] == 0


async def test_auth_failure_on_a_rewrite_is_not_retried(tmp_path, moderate):
    up = Upstream(responses=[(200, _sse(OPUS)), (401, b'{"type":"error"}')])
    app = _app(tmp_path, up)
    await _post(app, _first())
    r = await _post(app, _req())
    assert r.status_code == 401 and len(up.requests) == 2


async def test_decision_error_forwards_unchanged_and_says_why(tmp_path, monkeypatch):
    async def boom(text, pinned, *, anthropic=False):
        raise RuntimeError("classifier exploded")
    monkeypatch.setattr(pb, "tier_classify", boom)
    up = Upstream()
    app = _app(tmp_path, up)
    await _post(app, _first())
    body = _req()
    r = await _post(app, body)
    assert r.status_code == 200
    assert up.requests[1].content == json.dumps(body).encode()
    row = _rows(tmp_path)[-1]
    assert row["tier_reason"] == pt.REASON_DECISION_ERROR and "classifier exploded" in row["tier_detail"]
    assert row["served_model"] == OPUS


async def test_tiers_off_is_byte_identical_and_writes_no_tier_fields(tmp_path, moderate):
    up = Upstream()
    app = _app(tmp_path, up, tiers=ps.TIERS_OFF)
    body = _req()
    await _post(app, body)
    assert up.requests[0].content == json.dumps(body).encode()
    assert "tier_reason" not in _rows(tmp_path)[0] and "served_model" not in _rows(tmp_path)[0]
    assert _rows(tmp_path)[0]["tier_mode"] == "off"


async def test_conversation_mode_end_to_end_rewrites_the_first_call(tmp_path, moderate):
    """Server-level: `--tiers conversation` sends the FIRST call at a cheaper
    tier (per-turn mode would forward it unchanged at OPUS), then holds that
    tier for the rest of the conversation."""
    up = Upstream()
    app = _app(tmp_path, up, tiers=ps.TIERS_CONVERSATION)
    r = await _post(app, _first())
    assert r.status_code == 200 and SONNET.encode() in r.content
    r2 = await _post(app, _req())
    assert r2.status_code == 200 and SONNET.encode() in r2.content
    sent = [json.loads(q.content)["model"] for q in up.requests]
    assert sent == [SONNET, SONNET]  # both calls, including the first, ride the classified tier
    first, second = _rows(tmp_path)
    assert (first["tier_reason"], first["served_model"], first["tier_switch"]) == ("policy", SONNET, False)
    assert (second["tier_reason"], second["served_model"]) in (("policy", SONNET), ("sticky", SONNET))
    assert first["tier_mode"] == second["tier_mode"] == "conversation"


def test_tier_env_and_cli_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIERS", "on")
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIER_POLICY", str(tmp_path / "x.yaml"))
    cfg = ps.ProxyConfig.from_env()
    assert cfg.tiers == ps.TIERS_ON and cfg.tier_policy == str(tmp_path / "x.yaml")
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIERS", "conversation")
    assert ps.ProxyConfig.from_env().tiers == ps.TIERS_CONVERSATION
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIERS", "maybe")
    with pytest.raises(ValueError):
        ps.ProxyConfig.from_env()
    monkeypatch.delenv("LLM_ROUTER_PROXY_TIERS")
    assert ps.ProxyConfig.from_env().tiers == ps.TIERS_OFF  # opt-in


def test_missing_policy_file_fails_at_startup(tmp_path):
    with pytest.raises(OSError):
        ps.build_app(ps.ProxyConfig(tiers=ps.TIERS_ON, tier_policy=str(tmp_path / "nope.yaml"),
                                    upstream="http://127.0.0.1:9"))


# ── stats ────────────────────────────────────────────────────────────────────


def _row(requested, served, *, switch=False, reason="policy", usage=None, cost=None):
    return {"ts": 1.0, "session_id": SID, "decision": "forwarded", "requested_model": requested,
            "served_model": served, "tier_reason": reason, "tier_switch": switch, "tier_switch_cost_usd": cost,
            "response_model": served,
            "usage": ledger.normalize_usage(usage or {"input_tokens": 0, "cache_read_input_tokens": 100_000,
                                                      "output_tokens": 1_000})}


def test_cost_is_priced_at_the_served_model():
    row = _row(OPUS, HAIKU)
    assert ledger.anthropic_cost(row) < ledger.tier_counterfactual_cost(row)
    assert ledger.anthropic_cost(dict(row, served_model=None)) == ledger.tier_counterfactual_cost(row)


def test_tier_stats_mix_switch_rate_and_labelled_estimate():
    switch_usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 40_000,
                    "cache_creation": {"ephemeral_1h_input_tokens": 40_000}, "output_tokens": 500}
    rows = [_row(OPUS, OPUS, reason="first_call"),
            _row(OPUS, SONNET, switch=True, usage=switch_usage, cost=0.32),
            _row(OPUS, SONNET), _row(OPUS, SONNET)]
    s = ledger.stats(rows)["tiers"]
    assert s["calls"] == 4 and s["served_model_mix"] == {SONNET: 3, OPUS: 1}
    assert (s["switches"], s["switch_rate"], s["rewritten"]) == (1, 0.25, 3)
    assert s["est_switch_cost_usd"] == 0.32 and s["response_model_mismatch"] == 0
    assert s["switch_calls_cache_write_usd"] == round(
        ledger._price_tokens(SONNET, cache_creation_1h=40_000), 4)
    # the switched row's 40k writes are priced as reads in the counterfactual
    cf_switch = ledger.tier_counterfactual_cost(rows[1])
    assert cf_switch == ledger._price_tokens(OPUS, cache_read_input_tokens=40_000, output_tokens=500)
    assert s["est_counterfactual_requested_usd"] == pytest.approx(
        sum(ledger.tier_counterfactual_cost(r) for r in rows), abs=1e-4)
    assert s["est_saving_usd"] == pytest.approx(s["est_counterfactual_requested_usd"] - s["est_cost_usd"], abs=1e-4)
    text = ledger.format_stats(ledger.stats(rows))
    assert "ESTIMATE" in text and "paired A/B" in text


def test_stats_without_tier_rows_has_no_tier_block():
    assert ledger.stats([{"decision": "forwarded", "requested_model": OPUS}])["tiers"] is None


# ── complexity_knn on the classify= hook (Phase 2.1) ─────────────────────────


def _score(score, thr=0.5, calls=None):
    from llm_router.complexity_knn import ComplexityScore

    async def fn(text):
        if calls is not None:
            calls.append(text)
        return None if score is None else ComplexityScore(score, score >= thr, 0.9, thr)
    return fn


def _knn_policy(on: bool, cx: str):
    raw = _raw_policy()
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
    raw["complexity_knn"] = on
    return pt.ClaudeTierPolicy.from_dict(raw, classify=_classify(cx))


async def _second_call(policy):
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky)
    return await policy.decide(_req(), SID, sticky)


def test_bundled_policy_ships_the_knn_hook_off():
    assert pt.ClaudeTierPolicy.load().complexity_knn is False


async def test_knn_off_never_consults_the_score(monkeypatch):
    from llm_router import complexity_knn

    calls = []
    monkeypatch.setattr(complexity_knn, "complexity_score", _score(0.99, calls=calls))
    d = await _second_call(_knn_policy(False, "moderate"))
    assert (d.tier, d.complexity, d.complexity_score, calls) == ("sonnet", "moderate", None, [])


@pytest.mark.parametrize("cx,score,want_cx,want_tier", [
    ("simple", 0.2, "simple", "sonnet"),  # unchanged; haiku refuses adaptive thinking -> sonnet
    ("simple", 0.9, "complex", "opus"),
    ("complex", 0.1, "moderate", "sonnet"),
])
async def test_knn_on_wraps_the_classify_hook_and_moves_across_the_frontier(monkeypatch, cx, score,
                                                                             want_cx, want_tier):
    from llm_router import complexity_knn

    monkeypatch.setattr(complexity_knn, "complexity_score", _score(score))
    d = await _second_call(_knn_policy(True, cx))
    assert (d.complexity, d.complexity_score, d.tier) == (want_cx, score, want_tier)


async def test_knn_on_but_abstaining_keeps_the_classifier_complexity(monkeypatch):
    from llm_router import complexity_knn

    monkeypatch.setattr(complexity_knn, "complexity_score", _score(None))
    d = await _second_call(_knn_policy(True, "moderate"))
    assert (d.complexity, d.complexity_score, d.tier) == ("moderate", None, "sonnet")


# ── opt-in Haiku tier (haiku_rewrite) ────────────────────────────────────────
#
# Haiku 4.5 400s on `thinking.type: adaptive` and has no effort parameter
# (Anthropic API docs, fetched 2026-10-02), which is why the thinking floor
# raises a simple turn to Sonnet. `haiku_rewrite: true` instead serves it on
# Haiku with a rewritten body (translate.for_haiku).

from llm_router.proxy import escalation as esc  # noqa: E402
from llm_router.proxy.translate import HAIKU_MAX_OUTPUT_TOKENS, for_haiku  # noqa: E402


def _haiku_policy(*, on=True, conversation_level=True):
    raw = _raw_policy()
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
    raw["haiku_rewrite"] = on
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=conversation_level)


def _no_system_reminders(body: dict) -> dict:
    """The fixture's own ``role: "system"`` mid-conversation reminders
    stripped out. Real Claude Code 2.1.285 traffic always carries these
    (beta flags ``mid-conversation-system-2026-04-07`` /
    ``per-turn-control-2026-07-01``) and Haiku 4.5 rejects the role outright
    (live smoke call, 2026-10-02: ``{"type":"invalid_request_error",
    "message":"role \'system\' is not supported on this model"}``), so
    ``_haiku_eligible`` excludes any body that still has one -- see
    ``test_a_real_shaped_continuation_keeps_the_sonnet_floor_today`` below.
    Tests that exercise the REWRITE path use this helper to build a body
    shaped like the eligible class instead of the ineligible-by-default
    fixture."""
    body = copy.deepcopy(body)
    body["messages"] = [m for m in body["messages"] if not (isinstance(m, dict) and m.get("role") == "system")]
    return body


def _turns(*human_texts: str, model=OPUS) -> dict:
    """Plain text turns sharing ONE conversation key (same messages[0])."""
    body = _req(model)
    msgs: list[dict] = []
    for i, text in enumerate(human_texts):
        if i:
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": text}]})
    body["messages"] = msgs
    return body


def test_for_haiku_strips_exactly_the_fields_haiku_rejects():
    body = _req()
    body["thinking"] = {"type": "adaptive", "display": "omitted"}
    body["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"},
                                            {"type": "clear_tool_uses_20250919"}]}
    before = copy.deepcopy(body)
    out = for_haiku(body)
    assert body == before  # the input is never mutated
    assert "thinking" not in out and "output_config" not in out
    assert out["context_management"] == {"edits": [{"type": "clear_tool_uses_20250919"}]}  # only clear_thinking_* dropped
    assert out["max_tokens"] == HAIKU_MAX_OUTPUT_TOKENS == 64_000
    for kept in ("model", "messages", "system", "tools", "metadata", "stream"):
        assert out[kept] == body[kept]
    assert set(body) - set(out) == {"thinking", "output_config"}


def test_for_haiku_drops_an_emptied_context_management_and_clamps_max_tokens():
    body = _req()
    body["max_tokens"] = 128_000
    out = for_haiku(body)
    assert "context_management" not in out  # its only edit was clear_thinking_*
    assert out["max_tokens"] == 64_000
    body["max_tokens"] = 4096
    assert for_haiku(body)["max_tokens"] == 4096  # smaller values are left alone
    body.pop("max_tokens")
    assert "max_tokens" not in for_haiku(body)


def test_bundled_policy_ships_haiku_rewrite_off():
    assert pt.ClaudeTierPolicy.load().haiku_rewrite is False
    assert _raw_policy()["haiku_rewrite"] is False


async def test_flag_off_keeps_the_sonnet_floor_and_never_asks_for_a_rewrite():
    policy = _haiku_policy(on=False)
    d = await policy.decide(_first(), SID, Stickiness(), classify=_classify("simple"))
    assert (d.served_model, d.reason, d.body_rewrite) == (SONNET, pt.REASON_THINKING_FLOOR, None)


async def test_flag_on_serves_a_simple_adaptive_turn_on_haiku_with_a_rewrite_marker():
    policy = _haiku_policy()
    d = await policy.decide(_first(), SID, Stickiness(), classify=_classify("simple"))
    assert (d.served_model, d.tier, d.reason, d.body_rewrite) == (HAIKU, "haiku", pt.REASON_HAIKU_REWRITE,
                                                                  pt.REWRITE_HAIKU)
    assert d.rewritten and d.switched is False  # a first decision is not a cache switch


async def test_flag_on_does_not_touch_non_simple_turns():
    policy = _haiku_policy()
    d = await policy.decide(_first(), SID, Stickiness(), classify=_classify("moderate"))
    assert (d.served_model, d.reason, d.body_rewrite) == (SONNET, pt.REASON_POLICY, None)


async def test_a_body_haiku_already_accepts_needs_no_rewrite():
    policy = _haiku_policy()
    d = await policy.decide(_no_thinking(_first()), SID, Stickiness(), classify=_classify("simple"))
    assert (d.served_model, d.reason, d.body_rewrite) == (HAIKU, pt.REASON_POLICY, None)


IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}


def _later_turn(content) -> dict:
    """A second call in the conversation `_turns("rename foo")` opened."""
    body = _turns("rename foo", "and bar")
    body["messages"][-1]["content"] = content
    return body


def _with_builtin_tool(body: dict) -> dict:
    body = copy.deepcopy(body)
    body["tools"].append({"type": "web_search_20250305", "name": "web_search"})
    return body


def _image_in_tool_result() -> dict:
    body = _later_turn([{"type": "text", "text": "and bar"}])
    body["messages"] += [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": [IMG]}]}]
    return body


@pytest.mark.parametrize("make", [
    pytest.param(lambda: _later_turn([IMG, {"type": "text", "text": "what is this?"}]), id="image"),
    pytest.param(_image_in_tool_result, id="image_in_tool_result"),
    pytest.param(lambda: _with_builtin_tool(_later_turn([{"type": "text", "text": "and bar"}])), id="builtin_tool"),
    pytest.param(lambda: _later_turn([{"type": "text", "text": "x " * 400_000}]), id="context_over_limit"),
])
async def test_ineligible_conversations_keep_the_sonnet_floor(make):
    policy = _haiku_policy()
    sticky = Stickiness()
    opened = await policy.decide(_turns("rename foo"), SID, sticky, classify=_classify("simple"))
    assert opened.served_model == HAIKU  # the same conversation WAS eligible before
    d = await policy.decide(make(), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.reason, d.body_rewrite) == (SONNET, pt.REASON_THINKING_FLOOR, None)


async def test_eligibility_helper_edges():
    policy = _haiku_policy()
    assert policy._haiku_eligible(_no_system_reminders(_req())) is True
    assert pt._approx_context_tokens(_req()) < pt.HAIKU_MAX_CONTEXT_TOKENS
    assert policy._haiku_eligible(_later_turn([{"type": "text", "text": "x " * 400_000}])) is False
    assert _haiku_policy(on=False)._haiku_eligible(_req()) is False
    # the fixture itself -- real recorded Claude Code traffic -- carries the
    # mid-conversation `role: "system"` reminders Haiku rejects outright.
    assert pt._has_mid_conversation_system_message(_req()) is True
    assert policy._haiku_eligible(_req()) is False
    assert pt._has_mid_conversation_system_message(_no_system_reminders(_req())) is False


async def test_a_real_shaped_continuation_keeps_the_sonnet_floor_today():
    """The honest, live-discovered limit: Claude Code 2.1.285 always sends a
    mid-conversation `role: "system"` message (per-turn effort control), so
    an otherwise-eligible real continuation -- no media, custom tools only,
    small context -- still floors to Sonnet, not Haiku, until that message
    is handled. The fixture IS this shape; this pins the behavior so a future
    fix to lift it (e.g. folding the message into top-level `system`) has to
    deliberately change this test, not silently regress past it."""
    policy = _haiku_policy()
    sticky = Stickiness()
    await policy.decide(_first(), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(_req(), SID, sticky, classify=_classify("simple"))
    assert pt._has_mid_conversation_system_message(_req()) is True
    assert pt._has_media(_req()) is False and pt._only_custom_tools(_req()) is True
    assert (d.served_model, d.reason, d.body_rewrite) == (SONNET, pt.REASON_THINKING_FLOOR, None)
    # no `haiku` tier in the policy: the flag cannot do anything
    no_haiku = pt.ClaudeTierPolicy.from_dict({
        "tiers": [{"name": "sonnet", "model": SONNET, "thinking": ["adaptive"]}],
        "route": {"default": {"simple": "sonnet"}}, "haiku_rewrite": True})
    assert no_haiku._haiku_eligible(_req()) is False


async def test_correction_signal_still_escalates_to_opus_over_a_haiku_conversation():
    policy = _haiku_policy()
    sticky = Stickiness()
    ask = "rename foo to bar"
    d1 = await policy.decide(_turns(ask), SID, sticky, classify=_classify("simple"))
    assert (d1.served_model, d1.body_rewrite) == (HAIKU, pt.REWRITE_HAIKU)
    d2 = await policy.decide(_turns(ask, "ok", "No, that's wrong. You changed the wrong function."),
                             SID, sticky, classify=_classify("simple"))
    assert (d2.served_model, d2.reason, d2.detail, d2.body_rewrite) == (OPUS, pt.REASON_ESCALATION,
                                                                       esc.REASON_CONTRADICTION, None)


async def test_explicit_opus_pin_wins_over_the_haiku_tier():
    policy = _haiku_policy()
    d = await policy.decide(_turns("opus: rename foo to bar"), SID, Stickiness(), classify=_classify("simple"))
    assert (d.served_model, d.reason, d.body_rewrite) == (OPUS, pt.REASON_EXPLICIT_OPUS_PIN, None)


async def test_stickiness_holds_a_committed_sonnet_conversation_off_haiku():
    policy = _haiku_policy()
    sticky = Stickiness()
    await policy.decide(_turns("refactor this module"), SID, sticky, classify=_classify("moderate"))
    d = await policy.decide(_turns("refactor this module", "now fix the typo"), SID, sticky,
                            classify=_classify("simple"))
    assert (d.served_model, d.reason, d.body_rewrite) == (SONNET, pt.REASON_STICKY, None)


async def test_stickiness_holds_a_haiku_conversation_on_haiku_and_keeps_rewriting():
    policy = _haiku_policy()
    sticky = Stickiness()
    await policy.decide(_turns("rename foo"), SID, sticky, classify=_classify("simple"))
    d = await policy.decide(_turns("rename foo", "and bar"), SID, sticky, classify=_classify("simple"))
    assert (d.served_model, d.body_rewrite) == (HAIKU, pt.REWRITE_HAIKU)
    # ... until the conversation stops being eligible (an image arrives): back to the floor.
    d = await policy.decide(_later_turn([IMG, {"type": "text", "text": "what is this?"}]), SID, sticky,
                            classify=_classify("simple"))
    assert (d.served_model, d.body_rewrite) == (SONNET, None)


async def test_per_turn_mode_first_call_is_still_exempt_with_the_flag_on():
    policy = _haiku_policy(conversation_level=False)
    d = await policy.decide(_first(), SID, Stickiness(), classify=_classify("simple"))
    assert (d.reason, d.served_model, d.body_rewrite) == (pt.REASON_FIRST_CALL, OPUS, None)


async def test_a_client_that_asked_for_haiku_directly_is_rewritten_only_with_the_flag():
    for on, want in ((True, pt.REWRITE_HAIKU), (False, None)):
        d = await _haiku_policy(on=on).decide(_first(HAIKU), SID, Stickiness(), classify=_classify("simple"))
        assert d.served_model == HAIKU and d.body_rewrite == want


@pytest.fixture
def simple(monkeypatch):
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "query", "complexity": "simple", "chain_head": [], "model": None}
    monkeypatch.setattr(pb, "tier_classify", choose)


async def test_haiku_rewrite_reaches_anthropic_without_thinking_or_effort_and_is_ledgered(tmp_path, simple):
    up = Upstream()
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True})
    assert (await _post(app, _first())).status_code == 200
    r = await _post(app, _no_system_reminders(_req()))
    assert r.status_code == 200 and HAIKU.encode() in r.content
    first, second = (json.loads(q.content) for q in up.requests)
    assert first["model"] == OPUS and "thinking" in first  # the exempt first call is untouched
    original = _no_system_reminders(_req())
    assert second["model"] == HAIKU
    assert "thinking" not in second and "output_config" not in second and "context_management" not in second
    assert second["tools"] == original["tools"] and second["messages"] == original["messages"]
    assert second["max_tokens"] == 64_000
    row = _rows(tmp_path)[-1]
    assert (row["served_model"], row["response_model"], row["tier"]) == (HAIKU, HAIKU, "haiku")
    assert (row["tier_reason"], row["tier_body_rewrite"]) == (pt.REASON_HAIKU_REWRITE, "haiku")
    assert "tier_body_rewrite" not in _rows(tmp_path)[0]


async def test_haiku_rewrite_off_by_default_sends_sonnet_with_the_original_body(tmp_path, simple):
    up = Upstream()
    app = _app(tmp_path, up)
    await _post(app, _first())
    await _post(app, _req())
    second = json.loads(up.requests[1].content)
    original = _req()
    assert second["model"] == SONNET
    assert {k: v for k, v in second.items() if k != "model"} == {k: v for k, v in original.items() if k != "model"}
    row = _rows(tmp_path)[-1]
    assert row["tier_reason"] == pt.REASON_THINKING_FLOOR and "tier_body_rewrite" not in row


async def test_a_rejected_haiku_call_is_resent_with_the_clients_own_bytes(tmp_path, simple):
    err = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                 "message": "adaptive thinking is not supported"}}).encode()
    up = Upstream(responses=[(200, _sse(OPUS)), (400, err)])
    app = _app(tmp_path, up, policy_overrides={"haiku_rewrite": True})
    await _post(app, _first())
    r = await _post(app, _no_system_reminders(_req()))
    assert r.status_code == 200
    assert [json.loads(q.content)["model"] for q in up.requests] == [OPUS, HAIKU, OPUS]
    assert up.requests[2].content == json.dumps(_no_system_reminders(_req())).encode()
    row = _rows(tmp_path)[-1]
    assert row["tier_retry"]["status"] == 400 and row["served_model"] == OPUS
    # The final call used the client's own body, so the row must not claim a rewrite.
    assert "tier_body_rewrite" not in row


# ── quota pressure (proxy/quota_pressure.py) ────────────────────────────────
# Owner decision 2026-10-03: step down from Opus as the subscription quota
# fills. The reading is injected through the policy's ``quota`` hook so no test
# depends on the machine's usage.json; the reader itself is tested on files.

from llm_router.proxy import quota_pressure as qp  # noqa: E402

CONTRADICTION = "No, that's wrong. You changed the wrong function."


def _qpolicy(pressure, state=qp.STATE_OK, *, conversation_level=True, **raw_overrides):
    raw = {**_raw_policy(), **raw_overrides}
    raw["stickiness"] = dict(raw.get("stickiness") or {}, switch_after_first_call=True)
    return pt.ClaudeTierPolicy.from_dict(raw, conversation_level=conversation_level,
                                         quota=lambda: qp.QuotaReading(pressure, state))


def _usage(path, *, session=10.0, weekly=20.0, age=0.0, **extra):
    import time

    path.write_text(json.dumps({"session_pct": session, "weekly_pct": weekly, "sonnet_pct": 0.0,
                                "updated_at": time.time() - age, **extra}))
    return path


def test_reader_takes_the_higher_of_session_and_weekly_as_a_fraction(tmp_path):
    r = qp.read(_usage(tmp_path / "u.json", session=18.0, weekly=45.0))
    assert (r.pressure, r.state) == (0.45, qp.STATE_OK)
    r = qp.read(_usage(tmp_path / "u.json", session=82.0, weekly=45.0))
    assert (r.pressure, r.state) == (0.82, qp.STATE_OK)


def test_reader_reports_stale_unknown_and_off_instead_of_guessing(tmp_path, monkeypatch):
    stale = qp.read(_usage(tmp_path / "u.json", weekly=95.0, age=3600), max_age_s=1800)
    assert (stale.pressure, stale.state) == (0.95, qp.STATE_STALE)
    assert qp.read(tmp_path / "missing.json").state == qp.STATE_UNKNOWN
    (tmp_path / "bad.json").write_text("{not json")
    assert qp.read(tmp_path / "bad.json").state == qp.STATE_UNKNOWN
    (tmp_path / "seed.json").write_text(json.dumps({"pending": True, "seeded_at": 1}))
    assert qp.read(tmp_path / "seed.json").state == qp.STATE_UNKNOWN
    fallback = _usage(tmp_path / "fb.json", session=50, weekly=50, is_fallback=True)
    assert qp.read(fallback) == qp.QuotaReading(None, qp.STATE_UNKNOWN)
    (tmp_path / "nots.json").write_text(json.dumps({"session_pct": 90, "weekly_pct": 90}))
    assert qp.read(tmp_path / "nots.json").state == qp.STATE_UNKNOWN  # no updated_at: age unknown
    monkeypatch.setenv("LLM_ROUTER_PROXY_QUOTA_PRESSURE", "off")
    assert qp.read(_usage(tmp_path / "u.json", weekly=95.0)) == qp.QuotaReading(None, qp.STATE_OFF)


def test_bundled_policy_ships_the_proposed_thresholds():
    policy = pt.ClaudeTierPolicy.load()
    assert (policy.quota_enabled, policy.quota_cap_at, policy.quota_moderate_at, policy.quota_max_age_s) == \
        (True, 0.70, 0.90, 1800.0)
    with pytest.raises(ValueError, match="cap_at"):
        pt.ClaudeTierPolicy.from_dict({**_raw_policy(), "quota_pressure": {"cap_at": 0.9, "moderate_at": 0.7}})
    with pytest.raises(ValueError, match="mapping"):
        pt.ClaudeTierPolicy.from_dict({**_raw_policy(), "quota_pressure": [0.7]})


async def test_below_the_threshold_the_decision_is_unchanged():
    for pressure in (0.0, 0.5, 0.69):
        d = await _qpolicy(pressure).decide(_first(), SID, Stickiness(), classify=_classify("complex"))
        assert (d.served_model, d.reason, d.quota_pressure, d.quota_state) == (OPUS, pt.REASON_POLICY,
                                                                               pressure, qp.STATE_OK)


async def test_at_75_percent_opus_is_capped_to_sonnet():
    d = await _qpolicy(0.75).decide(_first(), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.tier, d.reason, d.quota_pressure) == (SONNET, "sonnet", pt.REASON_QUOTA_PRESSURE, 0.75)
    # A moderate turn is not moved further until moderate_at.
    d = await _qpolicy(0.75).decide(_first(), SID, Stickiness(), classify=_classify("moderate"))
    assert (d.served_model, d.reason) == (SONNET, pt.REASON_POLICY)


async def test_per_turn_first_call_and_long_prompt_keeps_are_capped_with_their_reason_in_detail():
    sticky = Stickiness()
    policy = _qpolicy(0.75, conversation_level=False)
    d = await policy.decide(_first(), SID, sticky, classify=_classify("complex"))
    assert (d.served_model, d.reason, d.detail, d.switched) == (SONNET, pt.REASON_QUOTA_PRESSURE,
                                                               pt.REASON_FIRST_CALL, False)
    # remembered on Sonnet: the next same-class call is not a switch
    nxt = await policy.decide(_req(), SID, sticky, classify=_classify("complex"))
    assert (nxt.served_model, nxt.switched) == (SONNET, False)
    long_brief = "\n".join(f"{i}. do part {i} of the migration across the whole repository" for i in range(1, 9))
    d = await _qpolicy(0.75).decide(_turns(long_brief), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.reason, d.detail) == (SONNET, pt.REASON_QUOTA_PRESSURE, pt.REASON_LONG_FIRST_PROMPT)


async def test_pins_are_respected_under_pressure():
    d = await _qpolicy(0.95).decide(_turns("opus: rename foo to bar"), SID, Stickiness(),
                                    classify=_classify("complex"))
    assert (d.served_model, d.reason, d.quota_pressure) == (OPUS, pt.REASON_EXPLICIT_OPUS_PIN, 0.95)
    body = _req()
    body["messages"].insert(0, {"role": "user", "content": "<command-name>/model</command-name>"})
    d = await _qpolicy(0.95).decide(body, SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.reason) == (OPUS, pt.REASON_USER_PINNED)
    pinned = _qpolicy(0.95, pinned_models=[OPUS])
    d = await pinned.decide(_req(), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.reason) == (OPUS, pt.REASON_CONFIG_PINNED)


async def test_escalation_still_reaches_opus_and_is_logged_under_pressure():
    for pressure, reason in ((0.5, pt.REASON_ESCALATION), (0.8, pt.REASON_ESCALATION_UNDER_PRESSURE),
                             (0.99, pt.REASON_ESCALATION_UNDER_PRESSURE)):
        policy, sticky = _qpolicy(pressure), Stickiness()
        await policy.decide(_turns("rename foo to bar"), SID, sticky, classify=_classify("moderate"))
        d = await policy.decide(_turns("rename foo to bar", "ok", CONTRADICTION), SID, sticky,
                                classify=_classify("moderate"))
        assert (d.served_model, d.reason, d.detail) == (OPUS, reason, esc.REASON_CONTRADICTION)


async def test_stale_unknown_or_switched_off_pressure_changes_nothing(monkeypatch):
    for state in (qp.STATE_STALE, qp.STATE_UNKNOWN, qp.STATE_OFF):
        d = await _qpolicy(0.97 if state == qp.STATE_STALE else None, state).decide(
            _first(), SID, Stickiness(), classify=_classify("complex"))
        assert (d.served_model, d.reason, d.quota_state) == (OPUS, pt.REASON_POLICY, state)
    off = _qpolicy(0.97, quota_pressure={"enabled": False})
    off._quota = None
    d = await off.decide(_first(), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.quota_state) == (OPUS, qp.STATE_OFF)

    def boom():
        raise RuntimeError("reader broke")

    broken = pt.ClaudeTierPolicy.from_dict(_raw_policy(), conversation_level=True, quota=boom)
    d = await broken.decide(_first(), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.quota_state) == (OPUS, qp.STATE_UNKNOWN)


async def test_the_default_reader_uses_the_cached_usage_json_with_its_staleness_limit(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    policy = pt.ClaudeTierPolicy.from_dict(_raw_policy(), conversation_level=True)
    _usage(home / "usage.json", session=12.0, weekly=76.0)
    d = await policy.decide(_first(), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.quota_pressure, d.quota_state) == (SONNET, 0.76, qp.STATE_OK)
    _usage(home / "usage.json", session=12.0, weekly=76.0, age=1801)
    d = await policy.decide(_first(), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.quota_state) == (OPUS, qp.STATE_STALE)
    monkeypatch.setenv("LLM_ROUTER_PROXY_QUOTA_PRESSURE", "0")
    _usage(home / "usage.json", weekly=96.0)
    d = await policy.decide(_first(), SID, Stickiness(), classify=_classify("complex"))
    assert (d.served_model, d.quota_state) == (OPUS, qp.STATE_OFF)


async def test_stickiness_does_not_hold_a_conversation_on_opus_above_the_threshold():
    readings = iter([0.4, 0.4, 0.75])
    raw = {**_raw_policy()}
    policy = pt.ClaudeTierPolicy.from_dict(raw, conversation_level=True,
                                           quota=lambda: qp.QuotaReading(next(readings), qp.STATE_OK))
    sticky = Stickiness()
    d1 = await policy.decide(_first(), SID, sticky, classify=_classify("complex"))
    d2 = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d1.served_model, d2.served_model, d2.reason) == (OPUS, OPUS, pt.REASON_STICKY)
    d3 = await policy.decide(_req(), SID, sticky, classify=_classify("moderate"))
    assert (d3.served_model, d3.reason, d3.switched) == (SONNET, pt.REASON_QUOTA_PRESSURE, True)
    assert sticky.get(conversation_key(_req(), SID)).model == SONNET


async def test_at_90_percent_moderate_moves_to_haiku_only_where_haiku_takes_the_body():
    # Claude Code's adaptive thinking + effort: the thinking floor keeps Sonnet.
    d = await _qpolicy(0.92).decide(_first(), SID, Stickiness(), classify=_classify("moderate"))
    assert (d.served_model, d.reason) == (SONNET, pt.REASON_POLICY)
    # A body Haiku accepts as sent.
    body = _first(thinking=None)
    body.pop("output_config", None)
    d = await _qpolicy(0.92).decide(body, SID, Stickiness(), classify=_classify("moderate"))
    assert (d.served_model, d.reason, d.body_rewrite) == (HAIKU, pt.REASON_QUOTA_PRESSURE, None)
    # haiku_rewrite on and an eligible conversation: Haiku with the rewrite marker.
    d = await _qpolicy(0.92, haiku_rewrite=True).decide(_turns("refactor this module"), SID, Stickiness(),
                                                       classify=_classify("moderate"))
    assert (d.served_model, d.reason, d.body_rewrite) == (HAIKU, pt.REASON_QUOTA_PRESSURE, pt.REWRITE_HAIKU)
    # complex is capped to Sonnet, not moved to Haiku
    d = await _qpolicy(0.92, haiku_rewrite=True).decide(_turns("refactor this module"), SID, Stickiness(),
                                                       classify=_classify("complex"))
    assert (d.served_model, d.reason) == (SONNET, pt.REASON_QUOTA_PRESSURE)


async def test_quota_fields_are_in_the_ledger(tmp_path, monkeypatch):
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "complex", "chain_head": [], "model": None}

    monkeypatch.setattr(pb, "tier_classify", choose)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    _usage(home / "usage.json", session=30.0, weekly=80.0)
    up = Upstream()
    app = _app(tmp_path, up, tiers=ps.TIERS_CONVERSATION)
    assert (await _post(app, _first())).status_code == 200
    assert json.loads(up.requests[0].content)["model"] == SONNET
    row = _rows(tmp_path)[-1]
    assert (row["tier_reason"], row["served_model"]) == (pt.REASON_QUOTA_PRESSURE, SONNET)
    assert (row["tier_quota_pressure"], row["tier_quota_state"]) == (0.8, qp.STATE_OK)
    (home / "usage.json").unlink()
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert (row["tier_quota_pressure"], row["tier_quota_state"]) == (None, qp.STATE_UNKNOWN)


async def test_at_90_percent_a_body_with_a_mid_conversation_system_message_stays_on_sonnet():
    """Claude Code sends role:system mid-conversation on every call and Haiku
    400s on it: the pressure step must not send such a body to Haiku."""
    body = _req(thinking=None)
    body.pop("output_config", None)
    assert pt._has_mid_conversation_system_message(body)
    for policy in (_qpolicy(0.92), _qpolicy(0.92, haiku_rewrite=True)):
        d = await policy.decide(body, SID, Stickiness(), classify=_classify("moderate"))
        assert (d.served_model, d.reason, d.body_rewrite) == (SONNET, pt.REASON_QUOTA_PRESSURE, None)
    clean = _no_system_reminders(body)
    d = await _qpolicy(0.92).decide(clean, SID, Stickiness(), classify=_classify("moderate"))
    assert d.served_model == HAIKU


def test_the_usage_read_is_cached_by_mtime_but_the_age_is_recomputed(tmp_path):
    p = _usage(tmp_path / "u.json", weekly=40.0)
    first = qp.read(p, now=None)
    real = Path.read_text
    calls = []

    def counting(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)

    Path.read_text = counting
    try:
        again = qp.read(p)
        assert not calls and again.pressure == first.pressure == 0.4
        assert qp.read(p, now=__import__("time").time() + 4000).state == qp.STATE_STALE
        _usage(p, weekly=88.0)
        st = p.stat()
        __import__('os').utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # coarse-mtime filesystems
        assert qp.read(p).pressure == 0.88 and calls
    finally:
        Path.read_text = real


# ── HAIKU55-1: a current model the policy does not configure ─────────────────

HAIKU55 = "claude-haiku-5-5"


def _haiku55_usage(cache_read):
    return {"input_tokens": 5, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": 1_000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000},
            "output_tokens": 40}


@pytest.mark.parametrize("cache_read, usd", [
    # up to 100K prompt tokens: $0.10 in, $0.01 cache read, $0.20 1h write, $0.50 out
    (30_000, (5 * 0.10 + 30_000 * 0.01 + 1_000 * 0.20 + 40 * 0.50) / 1e6),
    # over 100K (5 + 150,000 + 1,000): $0.50 in, $0.05 read, $1.00 1h write, $2.50 out
    (150_000, (5 * 0.50 + 150_000 * 0.05 + 1_000 * 1.00 + 40 * 2.50) / 1e6),
])
async def test_haiku_5_5_is_forwarded_unchanged_with_a_tier_label_and_a_cost(tmp_path, moderate, cache_read, usd):
    """docs/BUGS.md HAIKU55-1: 215 live rows on 2026-10-09 named claude-haiku-5-5 and were
    written with tier and cost null. The call stays as sent (moving it onto the Haiku tier's
    claude-haiku-4-5 would change the model), but the row is labelled and priced."""
    up = Upstream(responses=[(200, _sse(HAIKU55, usage=_haiku55_usage(cache_read)))])
    app = _app(tmp_path, up)
    assert (await _post(app, _req(HAIKU55))).status_code == 200
    assert json.loads(up.requests[0].content)["model"] == HAIKU55
    row = _rows(tmp_path)[-1]
    assert (row["served_model"], row["tier"], row["tier_reason"]) == (HAIKU55, "haiku", "unknown_model")
    assert row["anthropic_cost_usd"] == pytest.approx(usd, abs=1e-6)  # the row rounds to 6 dp


async def test_unknown_model_label_is_a_word_match_only(policy):
    sticky = Stickiness()
    for model, label in [(HAIKU55, "haiku"), ("claude-sonnet-6", "sonnet"), ("claude-3-opus", "opus"),
                         ("some-custom-fine-tune", None), ("gpt-5.5", None), ("claude-haikus-9", None)]:
        d = await policy.decide(_req(model), SID, sticky, classify=_classify("simple"))
        assert (d.reason, d.served_model, d.tier) == (pt.REASON_UNKNOWN_MODEL, model, label)
        d = policy.decide_unclassified(_req(model), SID, sticky)
        assert (d.reason, d.served_model, d.tier) == (pt.REASON_UNKNOWN_MODEL, model, label)
    assert policy.tier_of(HAIKU55) is None  # never a routing tier
