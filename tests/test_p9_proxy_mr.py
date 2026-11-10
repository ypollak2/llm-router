"""P9: LLM_ROUTER_PROXY_MR (default off) summarizes the oldest history of an over-window local session.

Behaviour only, mocked backend, no network. The map call goes through the live backend object, so the
model / num_ctx test drives a real OllamaBackend against a mocked Ollama transport.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random

import httpx

from llm_router.proxy import backends as pb
from llm_router.proxy import cls_decide, deadline, local_mode, mr_summary
from llm_router.token_estimate import est_tokens
from llm_router.proxy.translate import from_ollama
from tests.test_proxy_local_mode import MODEL, Backend, Breaker, _call, _first_call, _ollama, _overrides_readable, _post, env  # noqa: F401  (fixtures)

SUMMARY = "\n".join(f"{h}\nnone-{i}" for i, h in enumerate(mr_summary.HEADINGS))


def _is_map(body: dict) -> bool:
    first = body["messages"][0]["content"]
    return isinstance(first, str) and first.startswith("You compress one slice")


def _session(pairs: int = 20, result_chars: int = 8_000) -> dict:
    """A main-session conversation of ``pairs`` tool round trips, far over the local cap, ending on a tool_result."""
    body = _first_call()
    msgs = [body["messages"][0]]
    for i in range(pairs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Bash", "input": {"command": f"cat f{i}.py"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": ("line %d of output\n" % i) * (result_chars // 20)}]})
    body["messages"] = msgs
    return body


class MRBackend(Backend):
    """Live replies are distinct tool calls (the loop guard refuses repeats); map prompts get ``map_reply``."""

    def __init__(self, map_reply=SUMMARY, map_exc=None):
        super().__init__()
        self.map_reply, self.map_exc, self.maps, self.lives = map_reply, map_exc, [], []

    async def complete(self, body, timeout_s):
        self.calls.append(body)
        if _is_map(body):
            self.maps.append(body)
            if self.map_exc:
                raise self.map_exc
            m, err = from_ollama(_ollama(self.map_reply), body)
            return m, err, {"prompt_tokens": 10, "output_tokens": 2}
        self.lives.append(body)
        m, err = from_ollama(_ollama("", [_call("Bash", {"command": f"ls {len(self.lives)}"})]), body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}


async def _warm(app):
    await _post(app, _first_call())  # the conversation's first call, served locally


def _est(body: dict) -> int:
    return local_mode.estimate_prompt_tokens(body)


async def test_over_window_session_is_summarized_and_served_local(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    body = _session()
    assert _est(body) > local_mode.prompt_cap_tokens()
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert env.up.requests == []  # served locally: nothing went to Claude
    row = env.rows()[-1]
    mr = row["mr_summary"]
    assert mr["outcome"] == "summarized" and mr["chunks"] >= 1 and mr["map_calls"] == len(backend.maps) == mr["chunks"]
    assert mr["tokens_before"] > local_mode.prompt_cap_tokens() >= mr["tokens_after"] and mr["map_wall_s"] >= 0
    sent = backend.lives[-1]
    assert _est(sent) <= local_mode.prompt_cap_tokens()
    first = sent["messages"][0]["content"]
    assert sum("<chunk-summary" in b["text"] for b in first if b.get("type") == "text") == mr["chunks"]
    assert sent["messages"][-1] == body["messages"][-1]  # the current turn is verbatim
    assert len(sent["messages"]) < len(body["messages"])
    assert "line 0 of output" not in json.dumps(row)  # counts only, never text


async def test_summarizer_failure_falls_back_to_claude_without_truncating(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    body = _session()
    backend = MRBackend(map_exc=RuntimeError("backend down"))
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert env.rows()[-1]["mr_summary"]["outcome"] == "map_failed"
    assert [b for b in backend.lives if len(b["messages"]) > 2] == []  # no shortened or truncated history was served
    (req,) = env.up.requests
    assert json.loads(req.content)["messages"] == body["messages"]  # Claude gets the full original history


async def test_summary_that_still_does_not_fit_falls_back(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setattr(mr_summary, "SUMMARY_CEIL", 300)
    body = _session(pairs=8, result_chars=40_000)  # the protected tail alone is over the cap
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert env.rows()[-1]["mr_summary"]["outcome"] in ("cannot_fit", "no_closable_chunks", "still_over")
    assert [b for b in backend.lives if len(b["messages"]) > 2] == []
    (req,) = env.up.requests
    assert json.loads(req.content)["messages"] == body["messages"]


async def test_under_window_session_is_untouched(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    body = _session(pairs=5, result_chars=200)
    assert _est(body) < local_mode.prompt_cap_tokens()
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert backend.maps == [] and backend.lives[-1]["messages"] == body["messages"]
    assert "mr_summary" not in env.rows()[-1]


async def test_flag_off_over_window_is_unchanged(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LLM_ROUTER_PROXY_MR", raising=False)
    body = _session()
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert backend.maps == [] and len(backend.lives) == 1  # only the warm-up was local
    row = env.rows()[-1]
    assert row["reason"] == "prompt_over_cap" and "mr_summary" not in row
    (req,) = env.up.requests
    assert json.loads(req.content)["messages"] == body["messages"]


async def test_map_calls_carry_the_live_model_and_num_ctx(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    sent = []

    def ollama(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        is_map = payload["messages"][0]["content"].startswith("You compress one slice")
        msg = ({"role": "assistant", "content": SUMMARY} if is_map else
               {"role": "assistant", "content": "", "tool_calls": [_call("Bash", {"command": "ls"})]})
        line = json.dumps({"message": msg, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 5})
        return httpx.Response(200, content=(line + "\n").encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(ollama))
    backend = pb.OllamaBackend(MODEL, client, base_url="http://127.0.0.1:11999", num_ctx=32_768, num_predict=2048)
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    maps = [p for p in sent if p["messages"][0]["content"].startswith("You compress one slice")]
    lives = [p for p in sent if p not in maps]
    assert maps and lives
    assert {p["model"] for p in sent} == {"fake:1"}
    assert {p["options"]["num_ctx"] for p in sent} == {32_768}
    assert {p["options"]["num_predict"] for p in maps} == {mr_summary.DEFAULT_MAP_NUM_PREDICT}
    assert {p.get("keep_alive") for p in maps} == {p.get("keep_alive") for p in lives}


# -- turns local_step would not serve locally never spend a map call --------------------------------------

async def _no_maps_goes_to_claude(env, backend, body, app=None):  # noqa: F811
    app = app or env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert backend.maps == []
    assert "mr_summary" not in env.rows()[-1]
    assert json.loads(env.up.requests[-1].content)["messages"] == body["messages"]


async def test_exempt_session_spends_no_map_call(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setattr(local_mode, "exempt_reason", lambda *a, **k: (local_mode.EXEMPT_SUBAGENT, None))
    await _no_maps_goes_to_claude(env, MRBackend(), _session())


async def test_classifier_veto_spends_no_map_call(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setattr(cls_decide, "veto_of", lambda row: "veto_tier")
    await _no_maps_goes_to_claude(env, MRBackend(), _session())


async def test_open_breaker_spends_no_map_call(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    backend = MRBackend()
    await _no_maps_goes_to_claude(env, backend, _session(), env.app(backend, breaker=Breaker(allowed=False)))


async def test_claude_pinned_session_spends_no_map_call(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    backend = MRBackend()
    app = env.app(backend)
    await _post(app, _session())  # no local first call: started elsewhere, so pinned to Claude
    await _post(app, _session())
    assert backend.maps == [] and all("mr_summary" not in r for r in env.rows())


async def test_summarized_turn_that_is_then_forwarded_is_stamped_wasted(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")

    class LiveFails(MRBackend):
        async def complete(self, body, timeout_s):
            if len(self.calls) and not _is_map(body):
                self.calls.append(body)
                raise RuntimeError("backend down")
            return await super().complete(body, timeout_s)

    backend = LiveFails()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "wasted" and mr["wasted_reason"] and mr["map_calls"] == len(backend.maps) > 0
    assert len(env.up.requests) == 1


async def test_slow_summarizer_hits_the_deadline_and_falls_back(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    monkeypatch.setattr(deadline, "cap_s", lambda keepalive=False: 0.05)

    class Slow(MRBackend):
        async def complete(self, body, timeout_s):
            if _is_map(body):
                await asyncio.sleep(5)
            return await super().complete(body, timeout_s)

    backend = Slow()
    app = env.app(backend)
    await _warm(app)
    r = await _post(app, copy.deepcopy(_session()))
    assert r.status_code == 200 and env.rows()[-1]["mr_summary"]["outcome"] == "timeout"
    assert len(env.up.requests) == 1


async def test_summarizer_exception_is_claude_fallback_not_a_500(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")

    async def boom(*a, **k):
        raise ValueError("estimator broke")

    monkeypatch.setattr(mr_summary, "shorten", boom)
    backend = MRBackend()
    app = env.app(backend)
    await _warm(app)
    r = await _post(app, copy.deepcopy(_session()))
    assert r.status_code == 200 and env.rows()[-1]["mr_summary"]["outcome"] == "error"
    (req,) = env.up.requests
    assert json.loads(req.content)["messages"] == _session()["messages"]


async def test_invalid_map_replies_use_the_extractive_fallback(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_ROUTER_PROXY_MR", "1")
    backend = MRBackend(map_reply="no headings here")
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(_session()))
    mr = env.rows()[-1]["mr_summary"]
    assert mr["outcome"] == "summarized" and mr["extractive_chunks"] == mr["chunks"]
    assert mr["map_calls"] == 2 * mr["chunks"]  # one retry per chunk, then extractive
    assert env.up.requests == []


# -- the chunker never splits a tool call from its result ------------------------------------------------

def _random_history(rng: random.Random) -> dict:
    msgs = [{"role": "user", "content": "task"}]
    n = 0
    for _ in range(rng.randint(30, 60)):
        if rng.random() < 0.7:
            ids = [f"t{n + k}" for k in range(rng.randint(1, 3))]
            n += len(ids)
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": i, "name": "Bash", "input": {"c": "x" * rng.randint(10, 400)}} for i in ids]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "r" * rng.randint(50, 1200)} for i in ids]})
        else:
            msgs.append({"role": "assistant", "content": "thinking " * rng.randint(1, 200)})
            msgs.append({"role": "user", "content": "go on " * rng.randint(1, 100)})
    return {"model": "m", "messages": msgs}


async def test_a_chunk_boundary_never_splits_tool_use_from_tool_result(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_MR_CHUNK_TARGET", "800")
    seen_chunks = []

    async def fake_map(prompt, num_predict):
        return SUMMARY

    changed = 0
    for seed in range(120):
        body = _random_history(random.Random(seed))
        res = await mr_summary.shorten(body, fake_map, cap=9000, est=est_tokens)
        if res is None or not res.changed:
            continue
        changed += 1
        kept = res.body["messages"][1:]
        uses = {b["id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_use"}
        results = {b["tool_use_id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"}
        assert uses == results, f"seed {seed}: orphan tool call/result in the verbatim window"
        assert kept == body["messages"][len(body["messages"]) - len(kept):]  # a contiguous tail, nothing dropped inside it
        seen_chunks.append(res.row["chunks"])
    assert changed >= 5 and max(seen_chunks) >= 2  # the property was checked on real multi-chunk closures
