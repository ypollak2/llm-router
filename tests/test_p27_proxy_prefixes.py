"""Prefix contracts at the ASGI door; no real providers."""
import copy
import json

import httpx
import pytest

from llm_router import prefixes
from llm_router.proxy import escalation, ledger, server as ps, tiers as pt
from llm_router.proxy.cache_cost import Stickiness
from tests.test_proxy_local_mode import Backend, Breaker, _first_call, _ollama

pytestmark = pytest.mark.asyncio


def request(text, *, tools=True):
    body = _first_call()
    body["messages"] = [{"role": "user", "content": text}]
    if not tools:
        body.pop("tools", None)
    return body


async def door(tmp_path, monkeypatch, *, local=False, killed=False, breaker=True,
               policy=None):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    seen = []

    def upstream(req):
        seen.append(req.content)
        return httpx.Response(200, stream=httpx.ByteStream(
            b'{"type":"message","stop_reason":"end_turn","usage":{"output_tokens":1}}'),
            headers={"content-type": "application/json"})

    if policy is None:
        policy = pt.ClaudeTierPolicy.load()
    monkeypatch.setattr(pt.ClaudeTierPolicy, "load", lambda *a, **kw: policy)
    kill = tmp_path / "kill"
    if killed:
        kill.touch()
    cfg = ps.ProxyConfig(
        upstream="http://127.0.0.1:9", ledger_path=tmp_path / "rows.jsonl",
        steps=frozenset(), tiers="off" if local else "on", warm_up=False,
        serve="local-agent" if local else "off", model="ollama/fake:1",
        trim="none", hedge_s=None, kill_switch=kill)
    backend = Backend(_ollama("Done."))
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = ps.build_app(cfg, client=client, backend_factory=lambda m: backend,
                       breaker_fn=Breaker(breaker))

    async def post(body):
        raw = json.dumps(body).encode()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as c:
            response = await c.post("/v1/messages", content=raw)
        return response, raw, ledger.read_rows(cfg.ledger_path)[-1]

    return post, seen, backend, policy


@pytest.mark.parametrize("name", ["opus", "sonnet", "haiku", "claude", "local", "codex"])
async def test_prefixes(tmp_path, monkeypatch, name):
    post, seen, backend, policy = await door(tmp_path, monkeypatch)
    body = request(f"{name}: explain this")
    response, raw, row = await post(body)
    assert response.status_code == 200 and not backend.calls
    assert row["prefix"] == name
    if name in {"local", "codex"}:
        assert seen == [raw]
        assert row["prefix_outcome"] == f"unserved:no_{name}_backend"
    else:
        sent = json.loads(seen[0])
        assert sent["messages"][0]["content"] == "explain this"
        expected = body["model"] if name == "claude" else policy.by_name[name].model
        assert sent["model"] == expected


@pytest.mark.parametrize("text,tools", [
    ("gpt: explain this", True), ("explain this", True),
    ("opus: explain this", False),
])
async def test_untouched(tmp_path, monkeypatch, text, tools):
    post, seen, _, _ = await door(tmp_path, monkeypatch)
    _, raw, row = await post(request(text, tools=tools))
    assert seen == [raw]
    assert "prefix" not in row and "prefix_outcome" not in row


async def test_long_pin(tmp_path, monkeypatch):
    post, seen, _, policy = await door(tmp_path, monkeypatch)
    body = request("opus: " + "x" * 20000)
    body["model"] = policy.by_name["sonnet"].model
    assert escalation.explicit_pin(body) == "opus"
    decision = await policy.decide(body, None, Stickiness(policy.cold_gap_s))
    assert decision.served_model == policy.by_name["opus"].model
    await post(body)
    assert json.loads(seen[0])["model"] == policy.by_name["opus"].model


@pytest.mark.parametrize("stop", [None, "kill_switch", "breaker_open"])
async def test_forced_local(tmp_path, monkeypatch, stop):
    post, seen, backend, _ = await door(
        tmp_path, monkeypatch, local=True, killed=stop == "kill_switch",
        breaker=stop != "breaker_open")
    response, _, row = await post(request("local: explain this"))
    assert not seen and response.status_code == 200
    if stop:
        assert not backend.calls
        assert row["prefix_outcome"] == f"refused:{stop}"
        assert f"[llm_router] `local:` refused: {stop}; not sent to Claude." in response.text
    else:
        assert backend.calls
        assert "local:" not in json.dumps(backend.calls[0]["messages"])
        assert row["prefix_outcome"] == "served_local"


async def test_pressure_and_admin(tmp_path, monkeypatch):
    policy = pt.ClaudeTierPolicy.load()
    reading = type("Reading", (), {"pressure": 1.0, "state": "ok"})()
    monkeypatch.setattr(policy, "_read_quota", lambda: reading)
    post, seen, _, _ = await door(tmp_path, monkeypatch, policy=policy)
    opus = policy.by_name["opus"].model
    for text, expected in [("explain this", "sonnet"), ("opus: explain this", "opus")]:
        body = request(text)
        body["model"] = opus
        await post(body)
        assert json.loads(seen[-1])["model"] == policy.by_name[expected].model
    monkeypatch.setattr(policy, "pinned", policy.pinned | {pt._canonical(opus)})
    body = request("sonnet: explain this")
    body["model"] = opus
    _, _, row = await post(body)
    assert json.loads(seen[-1])["model"] == opus
    assert row["tier_reason"] == pt.REASON_CONFIG_PINNED


async def test_haiku_media_keeps_model(tmp_path, monkeypatch):
    post, seen, _, _ = await door(tmp_path, monkeypatch)
    body = request("haiku: explain this")
    body["messages"][0]["content"] = [
        {"type": "text", "text": "haiku: explain this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
    ]
    _, _, row = await post(body)
    assert json.loads(seen[0])["model"] == body["model"]
    assert row["tier_detail"] == "prefix_unservable:haiku_body"


async def test_one_example_for_tool_loop(tmp_path, monkeypatch):
    post, _, _, _ = await door(tmp_path, monkeypatch)
    body = request("opus: explain this")
    await post(body)
    for index in range(2):
        body["messages"].extend([
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{index}", "name": "Read",
                 "input": {"file_path": "/tmp/x"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{index}", "content": "ok"}]},
        ])
        await post(body)
    rows = [json.loads(line) for line in (tmp_path / "examples.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["forced"] == "opus" and rows[0]["door"] == "proxy"


async def test_strip_copy_and_malformed():
    body = request("opus: explain this")
    before = copy.deepcopy(body)
    stripped = prefixes.strip_body(body)
    assert body == before and stripped["messages"][0]["content"] == "explain this"
    for malformed in (None, [], {"messages": [None]}):
        assert escalation.explicit_override(malformed) is None
        assert escalation.explicit_pin(malformed) is None


REMINDER = "<system-reminder>\nAs you answer, use the context below.\n</system-reminder>"


def blocks_body(*texts):
    body = request("x")
    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": t} for t in texts]}]
    return body


@pytest.mark.parametrize("texts,kept", [
    ((REMINDER, "opus: explain"), [REMINDER, "explain"]),
    (("   ", "opus: explain"), ["   ", "explain"]),
    ((REMINDER + "\nopus: explain",), [REMINDER + "\nexplain"]),
])
def test_strip_selects_the_block_detection_selects(texts, kept):
    body = blocks_body(*texts)
    before = copy.deepcopy(body)
    assert escalation.explicit_pin(body) == "opus"
    stripped = prefixes.strip_body(body)
    assert body == before
    assert [b["text"] for b in stripped["messages"][0]["content"]] == kept


async def test_multi_block_body_is_stripped_at_the_door(tmp_path, monkeypatch):
    post, seen, _, policy = await door(tmp_path, monkeypatch)
    body = blocks_body(REMINDER, "opus: explain")
    body["model"] = policy.by_name["sonnet"].model
    await post(body)
    sent = json.loads(seen[0])
    texts = [b["text"] for b in sent["messages"][0]["content"]]
    assert texts == [REMINDER, "explain"]
    assert sent["model"] == policy.by_name["opus"].model


async def test_mid_text_prefix_is_forwarded_untouched(tmp_path, monkeypatch):
    post, seen, _, _ = await door(tmp_path, monkeypatch)
    _, raw, row = await post(request("please explain opus: why"))
    assert seen == [raw]
    assert "prefix" not in row


async def test_pin_sticks_for_the_rest_of_the_conversation(monkeypatch):
    policy = pt.ClaudeTierPolicy.load()
    # The client asks for Sonnet on every call; only the recorded pin can hold Opus.
    monkeypatch.setattr(policy, "allow_upgrade", True)
    sticky = Stickiness(policy.cold_gap_s)
    first = request("opus: explain this")
    first["model"] = policy.by_name["sonnet"].model
    d1 = await policy.decide(first, None, sticky)
    assert d1.served_model == policy.by_name["opus"].model
    later = copy.deepcopy(first)
    later["messages"] += [
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "thanks, and now the next part"}]},
    ]
    d2 = await policy.decide(later, None, sticky)
    assert (d2.served_model, d2.reason) == (policy.by_name["opus"].model, pt.REASON_STICKY)


async def test_pin_sticks_through_decide_unclassified(monkeypatch):
    policy = pt.ClaudeTierPolicy.load()
    # The client asks for Sonnet on every call; only the pin recorded by decide_unclassified can hold Opus.
    monkeypatch.setattr(policy, "allow_upgrade", True)
    sticky = Stickiness(policy.cold_gap_s)
    first = request("opus: explain this")
    first["model"] = policy.by_name["sonnet"].model
    d1 = policy.decide_unclassified(first, None, sticky)
    assert (d1.served_model, d1.reason) == (policy.by_name["opus"].model, pt.REASON_EXPLICIT_OPUS_PIN)
    later = copy.deepcopy(first)
    later["messages"] += [
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "thanks, and now the next part"}]},
    ]
    d2 = policy.decide_unclassified(later, None, sticky)
    assert (d2.served_model, d2.reason, d2.detail) == (policy.by_name["opus"].model, pt.REASON_STICKY, pt.STEP_ERROR)


# ── LOCAL-PIN-1: `local:` forces one step, never the conversation's pin ──────


def turns(*texts):
    """A conversation whose user prompts are ``texts``, with an assistant
    reply between each; the first prompt keys the conversation."""
    body = request(texts[0])
    for text in texts[1:]:
        body["messages"] += [{"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                             {"role": "user", "content": text}]
    return body


async def test_local_prefix_serves_one_step_and_the_claude_pin_holds(tmp_path, monkeypatch):
    post, seen, backend, _ = await door(tmp_path, monkeypatch, local=True)
    _, _, row = await post(turns("explain this", "and the next part"))  # never saw the start
    assert row["reason"] == "conversation_started_on_claude" and len(seen) == 1
    response, _, row = await post(turns("explain this", "and the next part", "local: do it"))
    assert row["prefix_outcome"] == "served_local" and len(backend.calls) == 1
    assert row["local_mode"]["pin"] == "claude" and row["local_mode"]["forced"] is True
    _, _, row = await post(turns("explain this", "and the next part", "local: do it", "and the rest"))
    assert len(backend.calls) == 1 and len(seen) == 2
    assert row["decision"] == "forwarded" and row["reason"] == "pinned_claude"


async def test_a_refused_local_step_leaves_no_local_pin(tmp_path, monkeypatch):
    post, seen, backend, _ = await door(tmp_path, monkeypatch, local=True)
    await post(turns("explain this", "and the next part"))  # pinned to Claude
    (tmp_path / "kill").touch()
    response, _, row = await post(turns("explain this", "and the next part", "local: do it"))
    assert row["prefix_outcome"] == "refused:kill_switch" and len(seen) == 1
    (tmp_path / "kill").unlink()
    _, _, row = await post(turns("explain this", "and the next part", "local: do it", "and the rest"))
    assert not backend.calls and len(seen) == 2
    assert row["decision"] == "forwarded" and row["reason"] == "pinned_claude"


async def test_an_unforced_local_decision_still_pins_the_conversation_local(tmp_path, monkeypatch):
    post, seen, backend, _ = await door(tmp_path, monkeypatch, local=True)
    _, _, row = await post(turns("explain this"))
    assert row["decision"] == "served" and row["local_mode"]["pin"] == "local"
    _, _, row = await post(turns("explain this", "and the next part"))
    assert row["decision"] == "served" and row["local_mode"]["pin"] == "local"
    assert len(backend.calls) == 2 and not seen


async def test_a_local_first_turn_pins_local_as_the_unforced_path_would(tmp_path, monkeypatch):
    post, seen, backend, _ = await door(tmp_path, monkeypatch, local=True)
    _, _, row = await post(turns("local: do it"))
    assert row["prefix_outcome"] == "served_local" and row["local_mode"]["pin"] == "local"
    _, _, row = await post(turns("local: do it", "and the rest"))
    assert row["decision"] == "served" and row["local_mode"]["pin"] == "local"
    assert "forced" not in row["local_mode"] and len(backend.calls) == 2 and not seen


async def test_a_refused_local_first_turn_stores_no_pin(tmp_path, monkeypatch):
    post, seen, backend, _ = await door(tmp_path, monkeypatch, local=True, killed=True)
    _, _, row = await post(turns("local: do it"))
    assert row["prefix_outcome"] == "refused:kill_switch" and row["local_mode"]["pin"] is None
    (tmp_path / "kill").unlink()
    _, _, row = await post(turns("local: do it", "and the rest"))
    assert row["reason"] == "conversation_started_on_claude" and not backend.calls and len(seen) == 1


async def test_a_served_local_step_mid_conversation_with_an_unseen_start_stores_no_pin(tmp_path, monkeypatch):
    post, seen, backend, _ = await door(tmp_path, monkeypatch, local=True)
    _, _, row = await post(turns("explain this", "local: do it"))
    assert row["prefix_outcome"] == "served_local" and len(backend.calls) == 1
    assert row["local_mode"]["pin"] is None and row["local_mode"]["forced"] is True
    _, _, row = await post(turns("explain this", "local: do it", "and the rest"))
    assert row["decision"] == "forwarded" and row["reason"] == "conversation_started_on_claude"
    assert len(backend.calls) == 1 and len(seen) == 1
