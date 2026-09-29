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


def _app(tmp_path, upstream, *, tiers=True, **cfg):
    import yaml

    raw = _raw_policy()
    raw["stickiness"]["switch_after_first_call"] = True
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
    monkeypatch.setattr(pb, "choose_model", choose)


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
    monkeypatch.setattr(pb, "choose_model", boom)
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
    app = _app(tmp_path, up, tiers=False)
    body = _req()
    await _post(app, body)
    assert up.requests[0].content == json.dumps(body).encode()
    assert "tier_reason" not in _rows(tmp_path)[0] and "served_model" not in _rows(tmp_path)[0]


def test_tier_env_and_cli_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIERS", "on")
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIER_POLICY", str(tmp_path / "x.yaml"))
    cfg = ps.ProxyConfig.from_env()
    assert cfg.tiers is True and cfg.tier_policy == str(tmp_path / "x.yaml")
    monkeypatch.setenv("LLM_ROUTER_PROXY_TIERS", "maybe")
    with pytest.raises(ValueError):
        ps.ProxyConfig.from_env()
    monkeypatch.delenv("LLM_ROUTER_PROXY_TIERS")
    assert ps.ProxyConfig.from_env().tiers is False  # opt-in


def test_missing_policy_file_fails_at_startup(tmp_path):
    with pytest.raises(OSError):
        ps.build_app(ps.ProxyConfig(tiers=True, tier_policy=str(tmp_path / "nope.yaml"),
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
