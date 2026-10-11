"""PLAN v16 P1.3-a: overflow is trimmed, then a bigger window, then escalated.

Covers: trim order, request/recent never trimmed, bigger-window pick, the `ast`
test that every `check_overflow(` site is preceded by `pack.trim_to(`, one
synthetic overflow fixture per site (6), and the default-off guarantee (D-47).
Ollama and litellm are fakes; no test touches a real model.
"""
from __future__ import annotations

import dataclasses
import json
import urllib.request

import pytest

from llm_router import failopen
from llm_router import local_context_guard as lcg
from llm_router import local_models, pack_overflow
from llm_router.paths import is_isolated

MODEL = "qwen3-coder:30b"
WINDOW = 2048


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    assert is_isolated()
    monkeypatch.delenv("LLM_ROUTER_PACK_OVERFLOW", raising=False)
    failopen.reset_cache()
    failopen.clear()
    lcg.reset_cache()
    yield
    failopen.reset_cache()
    lcg.reset_cache()


def _wrap(layer: str, body: str) -> str:
    tag = pack_overflow.TAGS[layer]
    return f"<{tag}>{body}</{tag}>"


SUMMARY = "## Goal\nship P1.3\n## Decisions\n" + "decided a thing\n" * 200 + "## Current state\nwriting tests\n"
INSTR = "# Rules\n" + "- a rule line that is long enough to cost tokens\n" * 200 + "## Style\n- more\n"
PROJECT = "knowledge " * 4000


def _system(project=PROJECT, summary=SUMMARY, instr=INSTR) -> str:
    return "BASE\n" + _wrap("project", project) + "\n" + _wrap("summary", summary) + "\n" + _wrap("instructions", instr)


def _payload(system: str | None = None, request: str = "do the thing") -> dict:
    return {"model": MODEL, "options": {"num_ctx": WINDOW},
            "messages": [{"role": "system", "content": system if system is not None else _system()},
                         {"role": "user", "content": request}]}


# ── trim order ───────────────────────────────────────────────────────────────

def test_default_is_off_and_returns_the_same_object():
    p = _payload()
    pack = pack_overflow.PayloadPack(p, num_ctx=WINDOW, model=MODEL)
    assert pack.trim_to() is p
    assert (pack.num_ctx, pack.model, pack.action) == (WINDOW, MODEL, "off")


def test_payload_trim_order_is_knowledge_then_summary_then_instructions(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    g = lcg.estimate_payload_tokens
    full = _payload()
    no_project = _payload(_system(project=""))
    s1 = g(no_project)
    # window just big enough once project knowledge is gone: only knowledge goes.
    pack = pack_overflow.PayloadPack(full, num_ctx=s1 + 300, model=MODEL, allow_model_swap=False)
    out = pack.trim_to()
    text = out["messages"][0]["content"]
    assert "knowledge" not in text and "decided a thing" in text and "a rule line" in text
    # smaller window: summary shrinks to Goal + Current state, instructions intact.
    s2 = g(_payload(_system(project="", summary="## Goal\nship P1.3\n## Current state\nwriting tests\n")))
    out = pack_overflow.PayloadPack(full, num_ctx=s2 + 300, model=MODEL, allow_model_swap=False).trim_to()
    text = out["messages"][0]["content"]
    assert "decided a thing" not in text and "ship P1.3" in text and "writing tests" in text
    assert "a rule line" in text
    # smaller still: instructions shrink to headings.
    out = pack_overflow.PayloadPack(full, num_ctx=700, model=MODEL, allow_model_swap=False).trim_to()
    text = out["messages"][0]["content"]
    assert "a rule line" not in text and "# Rules" in text and "ship P1.3" in text


def test_request_and_recent_are_never_trimmed(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    big_request = "r" * 20_000
    p = _payload(request=big_request)
    p["messages"].insert(1, {"role": "assistant", "content": "recent " * 50})
    out = pack_overflow.PayloadPack(p, num_ctx=512, model=MODEL, allow_model_swap=False).trim_to()
    assert out["messages"][-1]["content"] == big_request
    assert out["messages"][1]["content"] == "recent " * 50
    assert "BASE" in out["messages"][0]["content"]  # untagged text untouched
    assert p["messages"][0]["content"] == _system()  # input not mutated


def test_a_fitting_payload_is_returned_untouched(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    p = _payload(_system(project="tiny", summary="## Goal\nx\n", instr="# h\n"))
    pack = pack_overflow.PayloadPack(p, num_ctx=32768, model=MODEL)
    assert pack.trim_to() is p and pack.action == "fit"


@dataclasses.dataclass(frozen=True)
class _Pack:  # ContextPack-shaped: the real class lives in PR #392
    request: str
    recent: list
    project: str = ""
    summary: str | None = None
    instructions: str = ""


def test_trim_pack_order_on_a_contextpack_shaped_object():
    pk = _Pack("req", [{"role": "user", "content": "hi"}], PROJECT, SUMMARY, INSTR)
    t = pack_overflow._tokens
    total = lambda p: t(p.request) + t(p.project) + t(p.summary or "") + t(p.instructions) + t("hi")  # noqa: E731
    a = pack_overflow.trim_pack(pk, total(pk) - 1)
    assert a.project == "" and a.summary == SUMMARY and a.instructions == INSTR
    b = pack_overflow.trim_pack(pk, t("req") + t("hi") + t(INSTR) + 60)
    assert b.project == "" and "decided a thing" not in b.summary and "Goal" in b.summary and b.instructions == INSTR
    c = pack_overflow.trim_pack(pk, 100)
    assert c.instructions.count("\n") < INSTR.count("\n") and c.request == "req" and c.recent == pk.recent
    assert pack_overflow.trim_pack(pk, 10**9) is pk


# ── bigger window ────────────────────────────────────────────────────────────

def test_larger_model_pick_uses_local_models_num_ctx_only(monkeypatch):
    monkeypatch.setitem(local_models.NUM_CTX, "big-a", 65536)
    monkeypatch.setitem(local_models.NUM_CTX, "big-b", 131072)
    assert pack_overflow.pick_larger_model("llmr-edit", 20_000) == "qwen3-coder:30b"  # 16384 -> 32768
    assert pack_overflow.pick_larger_model("qwen3-coder:30b", 20_000) == "big-a"      # smallest larger
    assert pack_overflow.pick_larger_model("qwen3-coder:30b", 100_000) == "big-b"     # must hold the need
    assert pack_overflow.pick_larger_model("big-b", 1) is None                        # none larger
    assert pack_overflow.pick_larger_model("qwen3-coder:30b", 20_000, candidates=["llmr-edit"]) is None


def test_still_too_big_picks_a_bigger_window_then_check_overflow_passes(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    monkeypatch.setitem(local_models.NUM_CTX, "big-a", 65536)
    p = _payload(request="q" * 60_000)  # untrimmable request: ~17k tokens, window 2048
    pack = pack_overflow.PayloadPack(p, num_ctx=WINDOW, model=MODEL, candidates=["big-a", "llmr-edit"])
    out = pack.trim_to()
    assert pack.action == "bigger_window" and pack.model == "big-a" and pack.num_ctx == 65536
    assert out["model"] == "big-a" and out["options"]["num_ctx"] == 65536
    lcg.check_overflow(out, num_ctx=pack.num_ctx, model=pack.model, site="t")  # does not raise


def test_no_bigger_window_means_overflow_is_left_to_check_overflow(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    p = _payload(request="q" * 60_000)
    pack = pack_overflow.PayloadPack(p, num_ctx=WINDOW, model=MODEL, candidates=["llmr-edit"])
    out = pack.trim_to()
    assert pack.action == "overflow"
    with pytest.raises(lcg.ContextOverflow):
        lcg.check_overflow(out, num_ctx=pack.num_ctx, model=pack.model, site="t")


# ── synthetic overflow fixture, one per site ─────────────────────────────────

def _fits(payload: dict) -> bool:
    return lcg.estimate_payload_tokens(payload) <= WINDOW - lcg.DEFAULT_RESERVED_OUTPUT_TOKENS


class _Stop(Exception):
    pass


def _urlopen_spy(sent: list):
    def spy(req, *a, **k):
        if str(getattr(req, "full_url", req)).endswith("/api/chat"):
            sent.append(json.loads(req.data))
        raise _Stop()
    return spy


SITES = ["providers.call_llm", "providers.call_llm_stream_events", "proxy.OllamaBackend",
         "hooks.agent_loop", "hooks.direct_executor", "toolkit.adapters.ollama"]


async def _drive(site: str, monkeypatch, tmp_path) -> list[dict]:
    """Run one site with the oversized system prompt; return payloads that reached the transport."""
    sent: list[dict] = []
    system = _system()
    monkeypatch.setenv("LLM_ROUTER_LOCAL_NUM_CTX", str(WINDOW))
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_NUM_CTX", str(WINDOW))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_spy(sent))
    if site.startswith("providers."):
        import litellm

        from llm_router import providers

        async def acompletion(**kw):
            sent.append({"messages": kw["messages"]})
            raise _Stop()
        monkeypatch.setattr(litellm, "acompletion", acompletion)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": "do the thing"}]
        try:
            if site.endswith("call_llm"):
                await providers.call_llm(f"ollama/{MODEL}", msgs)
            else:
                async for _ in providers.call_llm_stream_events(f"ollama/{MODEL}", msgs):
                    pass
        except (_Stop, lcg.ContextOverflow):
            pass
    elif site == "proxy.OllamaBackend":
        from llm_router.proxy import backends

        class Client:
            def stream(self, method, url, json=None, **k):
                sent.append(json)
                raise _Stop()
        b = backends.OllamaBackend(MODEL, Client(), base_url="http://127.0.0.1:9", num_ctx=WINDOW,
                                   hedge_s=None, offload_cpu=False)
        body = {"model": "m", "max_tokens": 50, "system": system,
                "messages": [{"role": "user", "content": "do the thing"}]}
        try:
            await b.complete(body, 1.0)
        except (_Stop, lcg.ContextOverflow):
            pass
    elif site == "hooks.agent_loop":
        from llm_router.hooks import agent_loop

        monkeypatch.setattr("llm_router.context_injection.inject_system_prompt",
                            lambda system_prompt, *a, **k: system_prompt)
        try:
            agent_loop.run_agent_loop("do the thing", MODEL, tmp_path, system_prompt=system)
        except _Stop:
            pass
    elif site == "hooks.direct_executor":
        from llm_router.hooks import direct_executor

        try:
            direct_executor.call_ollama("do the thing", MODEL, system_prompt=system)
        except _Stop:
            pass
    else:
        from llm_router.toolkit.adapters.ollama import AdapterError, OllamaAdapter

        a = OllamaAdapter(MODEL, base_url="http://127.0.0.1:9", num_ctx=WINDOW)
        tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
        try:
            a.chat([{"role": "system", "content": system}, {"role": "user", "content": "do the thing"}],
                   tools, timeout_s=1.0)
        except (_Stop, AdapterError):
            pass
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("site", SITES)
async def test_synthetic_overflow_is_trimmed_to_fit_at_every_site(site, monkeypatch, tmp_path):
    # Control: with the flag off the fixture overflows, so nothing reaches the transport.
    assert await _drive(site, monkeypatch, tmp_path) == []
    assert failopen.snapshot().by_code.get("CHZ-FO-LOCAL-CTX-OVERFLOW", 0) >= 1
    # Flag on: a payload that fits is sent, with the request intact.
    failopen.reset_cache(); failopen.clear()  # noqa: E702
    monkeypatch.setenv("LLM_ROUTER_PACK_OVERFLOW", "1")
    sent = await _drive(site, monkeypatch, tmp_path)
    assert len(sent) == 1, f"{site}: nothing sent after trimming"
    assert _fits(sent[0]), f"{site}: sent payload still over window"
    assert "do the thing" in json.dumps(sent[0])
    assert "knowledge knowledge" not in json.dumps(sent[0])
