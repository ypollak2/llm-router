"""LC-12h repair 1 (PR #721 review): the secret door refuses precisely, recoverably, and everywhere.

* precision: a fixture corpus (placeholders, ordinary coding prompts, random real-shaped secrets) generated at test time;
* the refusal names the category and the place (current prompt / earlier turn N), never the value, and says what to do;
* the per-request override (`cloud:` prefix) is logged by count only; the gateway answers 422, the MCP tool errors;
* the SDK door; the scan is linear (ReDoS) and whole (windows overlap); the marker is reset; /api/show is retried;
* every prompt-carrying Ollama urllib call routes through ``local_privacy.open_url``.
"""
# ruff: noqa: F811  (fixtures are imported and used as arguments)
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_router import engine, local_privacy, router
from llm_router.types import TaskType
from tests import secret_fixtures as fx
from tests.test_lc12 import passed, world  # noqa: F401
from tests.test_lc12c import GOOD, LOCAL, e2e  # noqa: F401
from tests.test_lc12h import CLOUD, _drain, _route, _isolated  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_counts():
    local_privacy.secret_counts.clear()
    yield
    local_privacy.secret_counts.clear()


# ---------------------------------------------------------------------------------------------- 1. precision
def test_placeholders_are_never_refused():
    cases = fx.placeholders()
    assert len(cases) >= 30                                     # the check found something to check
    leaked = [c[:60] for c in cases if local_privacy.scan_secrets(c)]
    assert leaked == []


def test_ordinary_coding_prompts_are_never_refused():
    corpus = fx.ordinary_prompts(240)
    assert len(corpus) >= 200 and len(set(corpus)) >= 150       # a real, varied corpus
    leaked = [p[:60] for p in corpus if local_privacy.scan_secrets(p)]
    assert leaked == []


@pytest.mark.parametrize("kind", fx.KINDS)
def test_real_shaped_secrets_are_refused_with_their_category(kind):
    for seed in range(25):
        hits = local_privacy.scan_secrets(f"please debug this: {fx.real_shaped(kind, seed)} thanks")
        assert [h.label for h in hits] == [fx.LABEL[kind]], (kind, seed)
        assert hits[0].where == "current prompt"


def test_log_only_shapes_are_counted_by_category_and_never_refused():
    jwt = fx.jwt_fixtures()[0]
    assigned = 'api_key = "' + fx.real_shaped("github_token", 5)[4:] + '"'
    assert local_privacy.scan_secrets(f"fixture {jwt} and {assigned}") == []
    counts = dict(local_privacy.secret_counts)
    assert counts == {"logged:JWT": 1, "logged:assigned secret literal": 1}   # a counter and the category only
    assert jwt not in repr(counts) and assigned not in repr(counts)


# ---------------------------------------------------------------------------------------------- 2./3. messages
def test_refusal_names_category_place_and_remedy_never_the_value(e2e):
    key = fx.real_shaped("aws_key_id", 3)
    e2e.state["chain"] = [CLOUD]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused) as info:
        _route("write a test for the uploader", messages=[
            {"role": "user", "content": "hello"},
            {"role": "user", "content": f"my key is {key}"}])
    msg = str(info.value)
    assert key not in msg and "AKIA" not in msg
    assert "AWS access key (earlier turn 2)" in msg
    assert "only in the conversation history" in msg                 # history-only: says so, names the turn
    assert "llm-router local-check" in msg and "remove the secret" in msg and "`cloud:`" in msg
    assert e2e.state["tried"] == []
    assert local_privacy.secret_counts == {"refused:AWS access key": 1}


def test_current_prompt_refusal_says_current_prompt(e2e):
    e2e.state["chain"] = [CLOUD]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused) as info:
        _route(f"use {fx.real_shaped('github_token', 2)}")
    assert "GitHub token (current prompt)" in str(info.value)
    assert "only in the conversation history" not in str(info.value)


def test_placeholder_in_history_does_not_poison_later_turns(e2e):
    e2e.state["chain"] = [CLOUD]
    resp = _route("and now the next question", messages=[
        {"role": "user", "content": "AWS docs use AKIAIOSFODNN7EXAMPLE and postgres://postgres:postgres@db/x"}])
    assert e2e.state["tried"] == [CLOUD] and resp.content == GOOD


# ---------------------------------------------------------------------------------------------- 3. override
def test_cloud_prefix_overrides_for_that_request_and_is_stripped_and_counted(e2e, monkeypatch):
    e2e.state["chain"] = [CLOUD]
    sent: list = []
    inner = router._call_text

    async def spy(model, prompt, *a, **k):
        sent.append(prompt)
        return await inner(model, prompt, *a, **k)
    monkeypatch.setattr(router, "_call_text", spy)
    key = fx.real_shaped("anthropic_key", 4)
    _route(f"cloud: summarise this config, key {key}")
    _drain()
    assert e2e.state["tried"] == [CLOUD]
    assert sent and "summarise this config" in sent[0] and "cloud:" not in sent[0]
    assert local_privacy.secret_counts == {"overridden:Anthropic API key": 1}
    # the override is per request: the next request without the prefix is refused again
    with pytest.raises(local_privacy.SecretLocalOnlyRefused):
        _route(f"summarise this config, key {key}")


def test_explicit_cloud_model_override_on_a_secret_prompt_is_refused_not_rerouted(e2e):
    e2e.state["chain"] = [LOCAL, CLOUD]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused) as info:
        _route(f"key {fx.real_shaped('openai_key', 1)}", model_override=CLOUD)
    assert "you asked for model" in str(info.value) and CLOUD in str(info.value)
    assert e2e.state["tried"] == []
    # with the override prefix the request is ordinary traffic again: no refusal, the cloud entry is not cut
    _route(f"cloud: key {fx.real_shaped('openai_key', 1)}", model_override=CLOUD)
    assert e2e.state["tried"]


def test_mcp_tool_returns_the_message_as_its_error(e2e, monkeypatch):
    from llm_router.tools import text as text_tools
    monkeypatch.setattr(text_tools, "route_and_call", router.route_and_call)
    e2e.state["chain"] = [CLOUD]
    with pytest.raises(local_privacy.SecretLocalOnlyRefused) as info:
        asyncio.run(text_tools.llm_query(prompt=f"explain {fx.real_shaped('slack_token', 1)}", ctx=MagicMock()))
    assert "Slack token (current prompt)" in str(info.value) and "llm-router local-check" in str(info.value)


def test_llm_stream_tool_returns_the_message(monkeypatch):
    from llm_router.tools import routing
    monkeypatch.setattr("llm_router.profiles.get_model_chain", lambda *a, **k: [CLOUD])
    ctx = MagicMock()
    ctx.info = AsyncMock()
    out = asyncio.run(routing.llm_stream(prompt=f"x {fx.real_shaped('github_token', 8)}", ctx=ctx))
    assert out.startswith("Error: Refused:") and "GitHub token (current prompt)" in out and "`cloud:`" in out
    out = asyncio.run(routing.llm_stream(prompt=f"x {fx.real_shaped('github_token', 8)}", model=CLOUD, ctx=ctx))
    assert "you asked for model" in out


def test_gateway_answers_422_with_the_message(e2e, monkeypatch):
    from fastapi.testclient import TestClient

    from llm_router import gateway
    e2e.state["chain"] = [CLOUD]
    key = fx.real_shaped("github_token", 6)
    client = TestClient(gateway.app, base_url="http://127.0.0.1")
    body = {"model": "auto", "messages": [{"role": "user", "content": f"token {key}"},
                                          {"role": "assistant", "content": "ok"},
                                          {"role": "user", "content": "now what?"}]}
    r = client.post("/v1/chat/completions", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "GitHub token (earlier turn 1)" in detail and "llm-router local-check" in detail and key not in r.text
    assert e2e.state["tried"] == []
    # streaming: the error frame carries the same text
    r = client.post("/v1/chat/completions", json={**body, "stream": True})
    assert "earlier turn 1" in r.text and '"code":"422"' in r.text and key not in r.text


# ---------------------------------------------------------------------------------------------- 4. SDK door
def _sdk_world(monkeypatch, chain):
    from llm_router import sdk
    from llm_router.hooks import chain_builder, direct_executor as de
    seen: list = []
    monkeypatch.setattr(chain_builder, "build_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 0))

    def execute_chain(prompt, ch, task_type, **k):
        seen.append((prompt, list(ch)))
        return de.DirectResult(text="ok", model=ch[0], latency_ms=1)
    monkeypatch.setattr(de, "execute_chain", execute_chain)
    return sdk, seen, de


def _call_sdk(sdk, prompt, **k):
    return sdk._route(prompt, task_type="query", complexity="simple", timeout=5, session_id="s1", **k)


def test_sdk_door_cuts_the_chain_to_local_models(monkeypatch):
    de = __import__("llm_router.hooks.direct_executor", fromlist=["ModelSpec"])
    chain = [de.ModelSpec("gemini", "gemini-2.5-flash"), de.ModelSpec("openai", "gpt-4o"),
             de.ModelSpec("ollama", "qwen3:8b"), de.ModelSpec("ollama", "big:120b-cloud")]
    sdk, seen, _de = _sdk_world(monkeypatch, chain)
    monkeypatch.setattr(_de, "_get_ollama_url", lambda: "http://127.0.0.1:11434")
    _call_sdk(sdk, f"fix this, key {fx.real_shaped('github_token', 9)}")
    assert [(m.provider, m.model) for m in seen[0][1]] == [("ollama", "qwen3:8b")]
    seen.clear()
    _call_sdk(sdk, "fix this, no key")
    assert len(seen[0][1]) == 4                                   # an ordinary prompt keeps its whole chain
    seen.clear()
    _call_sdk(sdk, f"cloud: fix this, key {fx.real_shaped('github_token', 9)}")
    assert len(seen[0][1]) == 4 and seen[0][0].startswith("fix this")
    assert local_privacy.secret_counts == {"overridden:GitHub token": 1}


def test_sdk_door_refuses_without_a_local_model_and_in_history(monkeypatch):
    de = __import__("llm_router.hooks.direct_executor", fromlist=["ModelSpec"])
    sdk, seen, _de = _sdk_world(monkeypatch, [de.ModelSpec("gemini", "gemini-2.5-flash")])
    with pytest.raises(sdk.RoutingError) as info:
        _call_sdk(sdk, "next question", messages=[{"role": "user", "content": fx.real_shaped("aws_key_id", 2)}])
    assert "AWS access key (earlier turn 1)" in str(info.value) and seen == []


def test_sdk_door_refuses_a_non_loopback_ollama(monkeypatch):
    de = __import__("llm_router.hooks.direct_executor", fromlist=["ModelSpec"])
    sdk, seen, _de = _sdk_world(monkeypatch, [de.ModelSpec("ollama", "qwen3:8b")])
    monkeypatch.setattr(_de, "_get_ollama_url", lambda: "http://gpu-box.invalid:11434")
    with pytest.raises(sdk.RoutingError, match="not loopback"):
        _call_sdk(sdk, f"key {fx.real_shaped('github_token', 3)}")
    assert seen == []


# ---------------------------------------------------------------------------------------------- 5. ReDoS / windows
ADVERSARIAL = ["a-" * 100_000, "sk-" * 70_000, "eyJ" * 70_000, "token" * 40_000, "x://" * 50_000,
               "-----BEGIN " * 20_000, "Bearer " * 30_000, "ghp_" * 50_000, "AKIA" * 50_000, ("a." * 30 + "://") * 3_000]


@pytest.mark.parametrize("text", ADVERSARIAL, ids=range(len(ADVERSARIAL)))
def test_adversarial_200k_inputs_finish_and_are_not_secrets(text):
    assert len(text) >= 150_000
    assert engine.secret_findings(text).get("db_url_password") is not True
    assert local_privacy.scan_secrets(text) == []


@pytest.mark.timing
@pytest.mark.parametrize("text", ADVERSARIAL, ids=range(len(ADVERSARIAL)))
def test_adversarial_inputs_scan_in_linear_time(text):
    t0 = time.perf_counter()
    engine.secret_findings(text)
    assert time.perf_counter() - t0 < 2.0     # 48k chars took 1.6 s and 150k more than 8 s before the fix


def test_a_secret_straddling_every_window_edge_is_found():
    step = engine._SCAN_WINDOW - engine._SCAN_OVERLAP
    secret = fx.real_shaped("github_token", 11)
    pad = "word " * 60_000
    for edge in (engine._SCAN_WINDOW, 2 * step + engine._SCAN_OVERLAP, 3 * step + engine._SCAN_WINDOW - len(secret) // 2):
        for shift in range(-len(secret), 3):
            at = edge + shift
            text = pad[:at] + " " + secret + " " + pad[at:]
            assert engine.secret_findings(text).get("github_token") is True, (edge, shift)
    pem = fx.real_shaped("private_key", 4)
    at = engine._SCAN_WINDOW - 20                          # the header ends inside the window, its body lies beyond
    assert engine.secret_findings(pad[:at] + "\n" + pem + "\n" + pad)["private_key"] is True


# ---------------------------------------------------------------------------------------------- 6. marker reset
def test_secret_dispatch_marker_is_reset_and_the_next_cloud_call_is_not_refused(e2e):
    e2e.state["chain"] = [LOCAL, CLOUD]

    async def main():
        await router.route_and_call(TaskType.GENERATE, f"key {fx.real_shaped('github_token', 12)}")
        after_secret = local_privacy.secret_dispatch_active()
        e2e.state["chain"] = [CLOUD]
        e2e.state["tried"].clear()
        resp = await router.route_and_call(TaskType.GENERATE, "an ordinary question")
        return after_secret, local_privacy.private_dispatch_active(), resp
    after_secret, private_after, resp = asyncio.run(main())
    assert after_secret is False and private_after is False
    assert e2e.state["tried"] == [CLOUD] and resp.content == GOOD


# ---------------------------------------------------------------------------------------------- 7. /api/show
def test_failed_api_show_is_retried_after_the_short_ttl_and_a_good_one_refreshed(monkeypatch):
    import litellm

    from llm_router.local_check import runtimes
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")
    answers = [(False, None), (True, {"model_info": {"llama.context_length": 4096}, "template": ""}),
               (True, {"model_info": {"llama.context_length": 8192}, "template": ""})]
    calls: list = []
    monkeypatch.setattr(runtimes, "_fetch", lambda *a, **k: (calls.append(1), answers[len(calls) - 1])[1])
    clock = [1000.0]
    monkeypatch.setattr(local_privacy.time, "monotonic", lambda: clock[0])
    model = "ollama/ttl-probe:1b"
    local_privacy.ensure_ollama_model_info(model)
    assert litellm.model_cost["ollama/ttl-probe:1b"]["max_input_tokens"] is None      # failure entry, no proxy lookup
    local_privacy.ensure_ollama_model_info(model)
    assert len(calls) == 1                                                           # within the retry window
    clock[0] += local_privacy.INFO_RETRY_FAILED_S + 1
    local_privacy.ensure_ollama_model_info(model)
    assert len(calls) == 2 and litellm.model_cost["ollama/ttl-probe:1b"]["max_input_tokens"] == 4096
    clock[0] += local_privacy.INFO_RETRY_FAILED_S + 1
    local_privacy.ensure_ollama_model_info(model)
    assert len(calls) == 2                                                           # a good answer is kept longer
    clock[0] += local_privacy.INFO_REFRESH_OK_S + 1                                  # `ollama cp` / re-pull seen
    local_privacy.ensure_ollama_model_info(model)
    assert len(calls) == 3 and litellm.model_cost["ollama/ttl-probe:1b"]["max_input_tokens"] == 8192


# ---------------------------------------------------------------------------------------------- 10. api base
@pytest.mark.parametrize("raw,want", [
    ("http://127.0.0.1:11434/api/generate", "http://127.0.0.1:11434"),
    ("http://localhost:11434/api/chat/", "http://localhost:11434"),
    ("http://localhost:11434/api", "http://localhost:11434"),
    ("http://localhost:11434/", "http://localhost:11434"),
    ("http://localhost:11434", "http://localhost:11434"),
    ("http://gpu.invalid:11434/proxy/ollama", "http://gpu.invalid:11434/proxy/ollama"),
])
def test_ollama_api_base_strips_endpoint_suffixes(raw, want):
    assert local_privacy.ollama_api_base(raw) == want


# ---------------------------------------------------------------------------------------------- 8. urllib sites
def _load_hook(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "src/llm_router/hooks" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    pass


def _record_open(monkeypatch):
    seen: list[str] = []

    def open_url(req, *, base_url, timeout):
        seen.append(base_url)
        raise _Stop

    monkeypatch.setattr(local_privacy, "open_url", open_url)
    return seen


def test_every_prompt_carrying_ollama_call_goes_through_open_url(monkeypatch, tmp_path):
    from llm_router import context_summary, local_classifier, model_evaluator
    from llm_router.agentic import react
    from llm_router.hooks import agent_loop, direct_executor, tool_intercept
    from llm_router.toolkit.adapters import ollama as adapter
    seen = _record_open(monkeypatch)
    hit: dict[str, bool] = {}

    def run(label, fn):
        before = len(seen)
        try:
            fn()
        except Exception:  # noqa: BLE001 - a site fails open or raises once open_url refused: only the route matters
            pass
        hit[label] = len(seen) > before

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG-synthetic")
    monkeypatch.setattr(direct_executor, "_ollama_model_available", lambda *a, **k: True)
    run("context_summary._generate", lambda: context_summary._generate("m:1b", "text"))
    run("direct_executor.ollama_is_alive", direct_executor.ollama_is_alive)
    run("direct_executor.available_ollama_models", direct_executor.available_ollama_models)
    run("direct_executor.call_ollama", lambda: direct_executor.call_ollama("hello", "m:1b", timeout=2))
    run("local_classifier._post", lambda: local_classifier._post("m:1b", "text", 1.0))
    run("toolkit.adapters.ollama", lambda: adapter.OllamaAdapter("m:1b", base_url="http://localhost:11434").chat(
        [{"role": "user", "content": "x"}], [], timeout_s=1.0))
    run("model_evaluator", lambda: asyncio.run(model_evaluator.eval_ollama_model("m:1b")))
    run("agentic.react", lambda: react._default_ollama_client("m:1b")([{"role": "user", "content": "x"}], []))
    run("tool_intercept.describe_image", lambda: tool_intercept.describe_image(str(img), "m:1b", timeout=1))
    run("agent_loop.run_agent_loop", lambda: agent_loop.run_agent_loop("hello", "m:1b", tmp_path, timeout_per_call=1))
    hook = _load_hook("auto-route")
    monkeypatch.setattr(hook, "OLLAMA_MODELS", ["m:1b"])
    monkeypatch.setattr(hook, "OLLAMA_CODE_MODELS", ["m:1b"])
    run("hooks/auto-route", lambda: hook.classify_with_ollama("some prompt"))
    run("hooks/playwright-compress", lambda: _load_hook("playwright-compress")._try_ollama("page text"))
    assert len(hit) == 12                                      # the check found every site it lists
    assert [k for k, v in hit.items() if not v] == []


def test_security_doc_lists_every_ollama_urllib_site_with_a_status():
    text = (ROOT / "SECURITY.md").read_text()
    for site in ("context_summary.py", "hooks/auto-route.py", "hooks/direct_executor.py", "toolkit/adapters/ollama.py",
                 "hooks/playwright-compress.py", "model_evaluator.py", "hooks/agent_loop.py", "agentic/react.py",
                 "local_classifier.py", "hooks/tool_intercept.py"):
        row = next((ln for ln in text.splitlines() if site in ln and ("fixed" in ln or "open" in ln)), None)
        assert row, f"SECURITY.md has no fixed/open row for {site}"
