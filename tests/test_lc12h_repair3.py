"""LC-12h repair 3 (owner's merge of the #721 review fixes on top of repair 2): what repair 2 did not do.

* (a) a value in a line, or under a line, labelled ``.env.example`` / sample / placeholder is not refused; a real JWT
  is refused, the jwt.io sample and test-claim fixtures are not.
* (b) only the NEW request (prompt, system prompt, this call's context) is scanned, never earlier turns: a real secret
  in turn 3 does not refuse turn 30. Repair 2 scanned all of ``messages`` and named the turn.
* (d) the override also exists as a flag (``allow_cloud=True``), a body field and a header, not only the ``cloud:``
  prefix; every use is counted by category.
* (e) the gateway and the route server answer a refusal with a typed 422 body (category, places, remedy).
* (g) every shape scans 48k adversarial characters in a few milliseconds.
* (h) the dispatch markers are restored for the caller, in order, also around a nested call.

Every secret is generated at run time (tests/secret_fixtures.py); no model, no non-loopback host.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import http.client
import json
import random
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import llm_router.gateway as G
from llm_router import eligibility_routing as er
from llm_router import engine, local_privacy, router, sdk
from llm_router.hooks.direct_executor import DirectResult, ModelSpec
from llm_router.types import LLMResponse, TaskType
from tests import secret_fixtures as fx
from tests.test_lc12 import world  # noqa: F401
from tests.test_lc12c import GOOD, LOCAL, e2e  # noqa: F401
from tests.test_lc12h import CLOUD, _isolated  # noqa: F401

_B62 = fx.B62


def _rand(n: int, seed: int, alphabet: str = _B62) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(n))


def high(text: str) -> list[str]:
    """The shapes the door would refuse on."""
    return sorted(k for k, v in engine.secret_findings(text).items() if v)


def real_jwt() -> str:
    claims = {"sub": "b7e1c2d4-5f60-4a1b-9c3d-2e8f0a6b1c77", "iat": 1_700_000_000}
    return f"{fx._b64({'alg': 'HS256', 'typ': 'JWT'})}.{fx._b64(claims)}.{_rand(43, 13, _B62 + '_-')}"


@pytest.fixture(autouse=True)
def _clean_counts():
    local_privacy.secret_counts.clear()
    yield
    local_privacy.secret_counts.clear()


def _route(prompt, **k):
    return asyncio.run(router.route_and_call(TaskType.GENERATE, prompt, **k))


# ----------------------------------------------------------------------------- (a) labelled examples, JWT
def test_a_values_in_labelled_blocks_are_not_refused_and_unlabelled_ones_are():
    key = fx.real_shaped("openai_key", 5)
    aws = fx.real_shaped("aws_key_id", 6)
    token = fx.real_shaped("github_token", 7)
    labelled = [
        f"# .env.example\nOPENAI_API_KEY=\"{key}\"\n",
        f".env.sample:\nAPI_KEY=\"{key}\"",
        f"Here is a sample config:\n```\nOPENAI_API_KEY={key}\n```",
        f"placeholder values below\nGITHUB_TOKEN={token}",
        f"example credentials: {aws}",
        f"sample env file\n\nAWS_ACCESS_KEY_ID={aws}",
        f"DATABASE_URL={fx.real_shaped('db_url_password', 8)}  # example, not a real password",
    ]
    assert [t[:30] for t in labelled if high(t)] == []
    assert [t[:30] for t in labelled if local_privacy.scan_secrets(t)] == []
    unlabelled = [f"OPENAI_API_KEY={key}", f"debug this: {aws}", f"my token is {token}"]
    assert all(high(t) for t in unlabelled)


def test_a_a_real_secret_a_few_lines_below_a_label_is_still_refused():
    aws = "AWS_ACCESS_KEY_ID=" + fx.real_shaped("aws_key_id", 9)
    assert high("sample env file\n" + aws) == []
    assert high("sample env file\nA=1\n" + aws) == []                      # the label is two lines above: covered
    assert high("sample env file\nA=1\nB=2\n" + aws) == ["aws_key_id"]    # three lines above: not covered
    mixed = "# .env.example\nAPI_KEY=" + fx.real_shaped("openai_key", 10) + "\n\n\n\nnow my real one:\n" + aws
    assert high(mixed) == ["aws_key_id"]


def test_a_a_real_jwt_is_refused_and_fixtures_and_the_jwt_io_sample_are_not():
    assert high("my session token is " + real_jwt()) == ["jwt"]
    for fixture in fx.jwt_fixtures():
        assert high(f"fixture token = {fixture}") == [], fixture[:30]
    none_alg = f"{fx._b64({'alg': 'none'})}.{fx._b64({'sub': 'someone'})}.{_rand(20, 3)}"
    assert high(none_alg) == []
    not_json = "eyJ" + _rand(20, 4) + "." + _rand(30, 5) + "." + _rand(30, 6)
    assert high(not_json) == []                                           # does not decode: not a token


def test_a_the_refusal_corpus_numbers():
    ordinary = fx.ordinary_prompts(260)
    placeholders = fx.placeholders()
    refused_ordinary = [t for t in ordinary if local_privacy.scan_secrets(t)]
    refused_placeholder = [t for t in placeholders if local_privacy.scan_secrets(t)]
    real = [fx.real_shaped(k, s) for k in fx.KINDS for s in range(25)] + [real_jwt()]
    missed = [r[:10] for r in real if not local_privacy.scan_secrets(f"debug this: {r} please")]
    print(f"\nrefusal rate: ordinary {len(refused_ordinary)}/{len(ordinary)}, "
          f"placeholder {len(refused_placeholder)}/{len(placeholders)}, real-shaped missed {len(missed)}/{len(real)}")
    assert len(ordinary) >= 200 and len(placeholders) >= 30 and len(real) >= 250
    assert refused_ordinary == [] and refused_placeholder == [] and missed == []


# ----------------------------------------------------------------------------- (b) only the new turn
def test_b_a_real_secret_in_an_earlier_turn_does_not_refuse_the_new_turn(e2e):
    e2e.state["chain"] = [CLOUD]
    history = [{"role": "user", "content": f"my key is {fx.real_shaped('github_token', 1)}"},
               {"role": "assistant", "content": "noted"}]
    resp = _route("now explain what a mutex is", messages=history)
    assert e2e.state["tried"] == [CLOUD] and resp.content == GOOD


def test_b_the_new_turn_system_prompt_and_context_are_still_scanned(e2e):
    e2e.state["chain"] = [CLOUD, LOCAL]
    _route(f"debug this: {fx.real_shaped('github_token', 2)}")
    _route("hello", system_prompt=f"use {fx.real_shaped('aws_key_id', 3)}")
    _route("hello", caller_context=f"the key is {fx.real_shaped('openai_key', 4)}")
    assert e2e.state["tried"] == [LOCAL] * 3


def test_b_a_refusal_never_blames_the_history(e2e):
    e2e.state["chain"] = [CLOUD]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused) as ei:
        _route(f"debug this: {fx.real_shaped('github_token', 5)}",
               messages=[{"role": "user", "content": f"old {fx.real_shaped('aws_key_id', 6)}"}])
    assert "current prompt" in str(ei.value) and "earlier turn" not in str(ei.value)
    assert ei.value.categories == ["GitHub token"]


# ----------------------------------------------------------------------------- (c) typed exception
def test_c_the_refusal_carries_category_and_places_and_never_the_value(e2e):
    secret = fx.real_shaped("aws_key_id", 7)
    e2e.state["chain"] = [CLOUD]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused) as ei:
        _route(f"my key is {secret}")
    err = ei.value
    assert err.categories == ["AWS access key"] and "AWS access key" in str(err)
    body = err.body()["error"]
    assert body["type"] == "secret_local_only_refused" and body["places"] == ["current prompt"]
    assert "cloud:" in body["remedy"] and "allow_cloud" in body["remedy"]
    assert secret not in str(err) and secret not in json.dumps(err.body())
    assert e2e.state["tried"] == []


# ----------------------------------------------------------------------------- (d) override flag, header, body field
def test_d_the_allow_cloud_flag_sends_the_request_to_the_normal_chain_and_is_counted(e2e):
    e2e.state["chain"] = [CLOUD, LOCAL]
    secret = fx.real_shaped("github_token", 8)
    _route(f"debug: {secret}", allow_cloud=True)
    assert e2e.state["tried"][0] == CLOUD                                 # not cut to the local set
    assert local_privacy.secret_counts["overridden:GitHub token"] == 1
    assert not [k for k in local_privacy.secret_counts if secret in k]
    e2e.state["tried"].clear()
    _route(f"debug: {secret}")                                            # no override: cut to local
    assert e2e.state["tried"] == [LOCAL]


def test_d_the_flag_is_not_sticky_and_costs_nothing_without_a_secret(e2e):
    e2e.state["chain"] = [CLOUD]
    _route("plain question", allow_cloud=True)
    assert dict(local_privacy.secret_counts) == {}
    with pytest.raises(local_privacy.SecretLocalOnlyRefused):
        _route(f"debug: {fx.real_shaped('github_token', 9)}")


@pytest.fixture
def client():
    return TestClient(G.app, base_url="http://127.0.0.1")


def _refuse_with(monkeypatch):
    async def boom(payload):
        raise local_privacy.refusal([local_privacy.SecretHit("AWS access key", "current prompt")])
    import llm_router.route_server as RS
    monkeypatch.setattr(RS, "route_payload_async", boom)


def test_d_gateway_header_and_body_field_reach_the_router(client, monkeypatch):
    seen: list[tuple[bool, bool]] = []

    async def fake(task_type, prompt, **k):
        seen.append((k.get("allow_cloud"), local_privacy.override_requested()))
        return LLMResponse(content="ok", model="m/x", input_tokens=1, output_tokens=1, cost_usd=0.0,
                           latency_ms=1.0, provider="m")
    monkeypatch.setattr(router, "route_and_call", fake)
    client.post("/route", json={"prompt": "hi"})
    client.post("/route", json={"prompt": "hi"}, headers={local_privacy.OVERRIDE_HEADER: "1"})
    client.post("/route", json={"prompt": "hi", "allow_cloud": True})
    client.post("/route", json={"prompt": "hi"}, headers={local_privacy.OVERRIDE_HEADER: "0"})
    assert seen == [(False, False), (False, True), (True, False), (False, False)]


# ----------------------------------------------------------------------------- (e) typed 422
@pytest.mark.parametrize("path,body", [
    ("/route", {"prompt": "debug this"}),
    ("/v1/chat/completions", {"model": "auto", "messages": [{"role": "user", "content": "debug this"}]}),
    ("/v1/messages", {"model": "auto", "max_tokens": 16, "messages": [{"role": "user", "content": "debug this"}]}),
])
def test_e_gateway_answers_a_refusal_with_a_typed_422_not_a_502(client, monkeypatch, path, body):
    _refuse_with(monkeypatch)
    r = client.post(path, json=body)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["type"] == err["code"] == "secret_local_only_refused"
    assert err["categories"] == ["AWS access key"] and err["places"] == ["current prompt"]
    assert "cloud:" in err["remedy"] and "Refused" in err["message"]


def test_e_gateway_still_answers_a_provider_failure_with_502(client, monkeypatch):
    async def boom(payload):
        raise RuntimeError("provider down")
    import llm_router.route_server as RS
    monkeypatch.setattr(RS, "route_payload_async", boom)
    assert client.post("/route", json={"prompt": "hi"}).status_code == 502


def test_e_route_server_answers_a_refusal_with_a_typed_422():
    import unittest.mock as mock

    import llm_router.route_server as RS

    def refuse(_payload):
        raise local_privacy.refusal([local_privacy.SecretHit("Slack token", "current prompt")])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), RS.make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with mock.patch.object(RS, "route_payload", refuse):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("POST", "/route", json.dumps({"prompt": "x"}), {"Content-Type": "application/json"})
            resp = c.getresponse()
            body = json.loads(resp.read())
        assert resp.status == 422 and body["error"]["categories"] == ["Slack token"]
    finally:
        srv.shutdown()
        srv.server_close()


# ----------------------------------------------------------------------------- (f) SDK door: flag, new turn, class
@pytest.fixture
def sdk_chain(monkeypatch):
    ran: dict = {"chains": []}
    chain = [ModelSpec("gemini", "gemini-2.5-flash"), ModelSpec("ollama", "qwen3:8b"),
             ModelSpec("openai", "gpt-4o-mini"), ModelSpec("ollama", "gpt-oss:120b-cloud")]
    monkeypatch.setattr("llm_router.hooks.chain_builder.get_current_pressure", lambda: ("green", 5))
    monkeypatch.setattr("llm_router.hooks.chain_builder.build_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr("llm_router.hooks.chain_builder.needs_claude_tools", lambda *a, **k: False)

    def execute(prompt, ch, *a, **k):
        ran["chains"].append((prompt, [(m.provider, m.model) for m in ch]))
        return DirectResult(text="ok", model=ch[0], latency_ms=1)
    monkeypatch.setattr("llm_router.hooks.direct_executor.execute_chain", execute)
    monkeypatch.setattr("llm_router.hooks.direct_executor.execute_agent", lambda p, ch, *a, **k: execute(p, ch))
    monkeypatch.setattr("llm_router.hooks.savings_logger.log_direct_to_db", lambda **k: None)
    monkeypatch.setattr("llm_router.hooks.savings_logger.log_direct_savings", lambda **k: None)
    monkeypatch.setattr(sdk, "_decide", lambda *a, **k: ("query", "simple", None))
    ran["full"] = [(m.provider, m.model) for m in chain]
    return ran


def test_f_sdk_flag_override_new_turn_only_and_the_error_class(sdk_chain, monkeypatch):
    secret = fx.real_shaped("github_token", 11)
    sdk.route(f"debug: {secret}")
    assert sdk_chain["chains"][-1][1] == [("ollama", "qwen3:8b")]         # the door cuts to the loopback local model
    sdk.route(f"debug: {secret}", allow_cloud=True)
    assert sdk_chain["chains"][-1][1] == sdk_chain["full"]
    sdk.Router().run(f"debug: {secret}", allow_cloud=True)
    assert sdk_chain["chains"][-1][1] == sdk_chain["full"]
    assert local_privacy.secret_counts["overridden:GitHub token"] == 2
    # earlier turns are not scanned
    sdk.route("what is a mutex", messages=[{"role": "user", "content": f"token {secret}"}])
    assert sdk_chain["chains"][-1][1] == sdk_chain["full"]
    # no local model: a RoutingError that is also the router's refusal, with the category
    monkeypatch.setattr("llm_router.hooks.chain_builder.build_chain",
                        lambda *a, **k: [ModelSpec("gemini", "gemini-2.5-flash")])
    ran_before = len(sdk_chain["chains"])
    with pytest.raises(sdk.RoutingError) as ei:
        sdk.route(f"debug: {secret}")
    assert isinstance(ei.value, local_privacy.SecretLocalOnlyRefused) and ei.value.categories == ["GitHub token"]
    assert secret not in str(ei.value) and len(sdk_chain["chains"]) == ran_before


# ----------------------------------------------------------------------------- (g) linear time, every shape
ADVERSARIAL_48K = {
    "url-no-at": "http://a:" + "b" * 48_000,
    "url-scheme-run": "a-" * 24_000 + "://u:" + "b" * 100,
    "url-plus-run": "a+" * 24_000 + "://x:bbbbbbbbb",
    "url-many-anchors": "a://" * 12_000,
    "url-many-credentials": ("x://u:" + "b" * 500 + "/") * 94,
    "xoxb-repeat": "xoxb-" * 9_600,
    "sk-dashes": "sk-" * 16_000,
    "sk-ant-repeat": "sk-ant-" * 6_800,
    "jwt-dashes": "eyJ-" * 12_000,
    "jwt-no-dot": "eyJ" + "a" * 48_000,
    "token-dashes": "token-" * 8_000,
    "bearer-repeat": ("Bearer " + "a" * 29 + " ") * 1_290,
    "private-key-header": "-----BEGIN PRIVATE KEY-----" * 1_770,
    "aws-example-repeat": "AKIAIOSFODNN7EXAMPLE " * 2_280,
    "placeholder-line": "ghp_" + "x" * 48_000,
    "labelled-many": ("# .env.example\nAPI_KEY=" + "A1b2" * 10 + "\n") * 1_000,
}


@pytest.mark.timing
@pytest.mark.parametrize("name", sorted(ADVERSARIAL_48K))
def test_g_every_shape_scans_adversarial_48k_characters_in_milliseconds(name):
    text = ADVERSARIAL_48K[name]
    assert 38_000 <= len(text) <= 49_000
    t0 = time.perf_counter()
    engine.secret_findings(text)
    ms = (time.perf_counter() - t0) * 1000
    assert ms < (50 if name.startswith("url-") else 250), f"{name}: {ms:.1f} ms"


# ----------------------------------------------------------------------------- (h) markers, nested and during dispatch
def test_h_outer_markers_survive_a_nested_secret_call(e2e):
    async def go():
        outer_s, outer_p = local_privacy.set_secret_dispatch(True), er.set_pass_only_dispatch(True)
        try:
            e2e.state["chain"] = [LOCAL]
            await router.route_and_call(TaskType.GENERATE, f"debug: {fx.real_shaped('github_token', 12)}")
            assert local_privacy.secret_dispatch_active() and er.pass_only_dispatch_active()
        finally:
            er.reset_pass_only_dispatch(outer_p)
            local_privacy.reset_secret_dispatch(outer_s)
        assert not local_privacy.secret_dispatch_active() and not er.pass_only_dispatch_active()
    asyncio.run(go())


def test_h_the_marker_is_set_during_the_dispatch_of_a_secret_call_only(e2e, monkeypatch):
    during: list[bool] = []
    real = router._call_text

    async def spy(model, *a, **k):
        during.append(local_privacy.secret_dispatch_active())
        return await real(model, *a, **k)
    monkeypatch.setattr(router, "_call_text", spy)
    e2e.state["chain"] = [LOCAL]
    _route(f"debug: {fx.real_shaped('github_token', 13)}")
    _route("plain question")
    assert during == [True, False]
    assert not local_privacy.secret_dispatch_active()
