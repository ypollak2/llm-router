"""Plan 3.2: the edit protocol's Ollama calls carry a JSON Schema ``format``.

(a) the Ollama payloads (proxy edit step and the zero-Claude edit hook) carry
    the schema; (b) other edit paths do not; (c) the schema accepts what
    ``parse_edit_response`` accepts and rejects the malformed shapes; (d) a
    server that refuses ``format`` with HTTP 400 gets the request again,
    unconstrained, and the edit still works.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import httpx
import jsonschema
import pytest

from llm_router.edit import parse_edit_response
from llm_router.hooks import direct_executor as de
from llm_router.local_agent import proxy_step
from llm_router.local_agent.constrain import EDIT_PAIRS_SCHEMA, is_format_rejection, with_edit_format

VALID = [{"file": "a.py", "old_string": "return n", "new_string": "return n + 1",
          "description": "off by one"}]


# ── (c) the schema matches the parser ────────────────────────────────────────

@pytest.mark.parametrize("sample", [
    VALID,
    [{"file": "a.py", "old_string": "x", "new_string": "y"}],  # description optional
    [],                                                       # "no changes needed"
])
def test_schema_accepts_what_the_parser_accepts(sample):
    jsonschema.validate(sample, EDIT_PAIRS_SCHEMA)
    instructions, warnings = parse_edit_response(json.dumps(sample))
    assert len(instructions) == len(sample) and not warnings


@pytest.mark.parametrize("sample", [
    {"file": "a.py", "old_string": "x", "new_string": "y"},   # object, not array
    [{"file": "a.py", "old_string": "x"}],                    # missing new_string
    [{"path": "a.py", "old": "x", "new": "y"}],               # wrong keys
    ["a.py"],                                                 # item not an object
])
def test_schema_rejects_the_malformed_shapes(sample):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(sample, EDIT_PAIRS_SCHEMA)
    instructions, _ = parse_edit_response(json.dumps(sample))
    assert instructions == []


def test_with_edit_format_copies_and_only_adds_format():
    payload = {"model": "m", "messages": []}
    out = with_edit_format(payload)
    assert out == {**payload, "format": EDIT_PAIRS_SCHEMA}
    assert "format" not in payload


def test_only_400_counts_as_a_format_rejection():
    assert is_format_rejection(400)
    assert not any(is_format_rejection(s) for s in (None, 200, 404, 500, 503))


# ── the proxy edit step (local_agent.proxy_step.generator_for) ───────────────

class _Backend:
    model, num_ctx, keep_alive, base_url = "qwen3-coder:30b", 8192, -1, "http://ollama.test"

    def __init__(self, handler):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": content}, "done_reason": "stop",
                                     "prompt_eval_count": 10, "eval_count": 5})


def test_a_proxy_edit_step_sends_the_schema_to_ollama():
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return _ok(json.dumps(VALID))

    raw, _ = asyncio.run(proxy_step.generator_for(_Backend(handler))("sys", "prompt", 256, 10.0))
    assert len(sent) == 1 and sent[0]["format"] == EDIT_PAIRS_SCHEMA
    assert json.loads(raw) == VALID


def test_a_proxy_edit_step_retries_unconstrained_when_format_is_refused():
    sent: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "format" in body:
            return httpx.Response(400, json={"error": "json: cannot unmarshal object into "
                                                      "Go struct field ChatRequest.format of type string"})
        return _ok(json.dumps(VALID))

    raw, usage = asyncio.run(proxy_step.generator_for(_Backend(handler))("sys", "prompt", 256, 10.0))
    assert [("format" in b) for b in sent] == [True, False]
    assert {k: v for k, v in sent[1].items()} == {k: v for k, v in sent[0].items() if k != "format"}
    assert json.loads(raw) == VALID and usage["output_tokens"] == 5


def test_a_proxy_edit_step_does_not_retry_other_server_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"error": "model failed to load"})

    raw, _ = asyncio.run(proxy_step.generator_for(_Backend(handler))("sys", "prompt", 256, 10.0))
    assert raw is None and len(calls) == 1


def test_a_backend_with_its_own_generate_text_is_left_alone():
    class _Custom:
        async def generate_text(self, *a):
            return "x", {}

    b = _Custom()
    assert proxy_step.generator_for(b) == b.generate_text


# ── the zero-Claude edit hook (direct_executor.call_ollama) ──────────────────

def _stream(chunks):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def __iter__(self):
            for c in chunks:
                yield json.dumps(c).encode()
    return _Resp()


def _done(text):
    return [{"message": {"content": text}}, {"message": {"content": ""}, "done": True, "eval_count": 3}]


def test_call_ollama_sends_format_only_when_asked(monkeypatch):
    sent: list[dict] = []

    def urlopen(req, timeout):
        sent.append(json.loads(req.data))
        return _stream(_done("[]"))

    monkeypatch.setattr(de.urllib.request, "urlopen", urlopen)
    de.call_ollama("q", "m", 30)
    de.call_ollama("q", "m", 30, format=EDIT_PAIRS_SCHEMA)
    assert "format" not in sent[0]
    assert sent[1]["format"] == EDIT_PAIRS_SCHEMA


def test_call_ollama_retries_unconstrained_on_a_400(monkeypatch):
    sent: list[dict] = []

    def urlopen(req, timeout):
        body = json.loads(req.data)
        sent.append(body)
        if "format" in body:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))
        return _stream(_done(json.dumps(VALID)))

    monkeypatch.setattr(de.urllib.request, "urlopen", urlopen)
    text, usage = de.call_ollama("q", "m", 30, format=EDIT_PAIRS_SCHEMA)
    assert [("format" in b) for b in sent] == [True, False]
    assert json.loads(text) == VALID and usage["output_tokens"] == 3


def test_call_ollama_without_format_does_not_retry_a_400(monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(de.urllib.request, "urlopen", urlopen)
    assert de.call_ollama("q", "m", 30) == (None, {})
    assert len(calls) == 1


def test_zero_claude_edit_asks_ollama_for_the_schema(monkeypatch):
    from llm_router import zero_claude_edit as zce

    seen: list[dict] = []

    def fake_call_ollama(prompt, model, timeout, **kw):
        seen.append(kw)
        return json.dumps([{"file": "a.py", "old_string": "return n", "new_string": "return n + 1"}]), {}

    monkeypatch.setattr(de, "call_ollama", fake_call_ollama)
    import time
    new, _, err = zce.generate_edits("fix", {"a.py": "def f(n):\n    return n\n"}, "m",
                                     time.monotonic() + 60)
    assert err is None and new["a.py"].endswith("return n + 1\n")
    assert seen[0]["format"] == EDIT_PAIRS_SCHEMA


# ── (b) the cloud-routed llm_edit path is unchanged ──────────────────────────

async def test_the_routed_llm_edit_path_sends_no_ollama_format(tmp_path, monkeypatch):
    """``tools/text.py``'s ``llm_edit`` goes through ``route_and_call`` to any
    provider; the schema is wired only into the two direct Ollama payloads."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from llm_router.tools.text import llm_edit

    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    resp = MagicMock(content=json.dumps([{"file": str(f), "old_string": "x = 1", "new_string": "x = 2"}]),
                     model="openai/gpt-4o-mini", citations=[])
    resp.header.return_value = "Model: openai/gpt-4o-mini"
    route = AsyncMock(return_value=resp)
    with patch("llm_router.tools.text.route_and_call", route), \
            patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock):
        out = await llm_edit("bump x", [str(f)], MagicMock(request_id="r"))
    assert "applied=True" in out
    assert route.await_count == 1
    assert "format" not in route.await_args.kwargs
    assert EDIT_PAIRS_SCHEMA not in route.await_args.args
