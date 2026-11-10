"""P9: LLM_ROUTER_PROXY_MR (default off) summarizes the oldest history of an over-window local session.

Behaviour only, mocked backend, no network. The map call goes through the live backend object, so the
model / num_ctx test drives a real OllamaBackend against a mocked Ollama transport.
"""

from __future__ import annotations

import copy
import json

import httpx

from llm_router.proxy import backends as pb
from llm_router.proxy import local_mode, mr_summary
from llm_router.proxy.translate import from_ollama
from tests.test_proxy_local_mode import MODEL, Backend, _call, _first_call, _ollama, _overrides_readable, _post, env  # noqa: F401  (fixtures)

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
