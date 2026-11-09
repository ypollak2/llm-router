"""proxy.prefix_hash: append-only evidence in the proxy ledger row (measurement only)."""
from __future__ import annotations

import json

import httpx

from llm_router.proxy import backends as pb
from llm_router.proxy import prefix_hash as ph
from llm_router.proxy import server as ps


def _payload(msgs, tools=None, keep_alive=600):
    return {"messages": msgs, "tools": tools, "keep_alive": keep_alive}


S = {"role": "system", "content": "SECRET-SYSTEM-TEXT"}
U1 = {"role": "user", "content": "SECRET-U1"}
A1 = {"role": "assistant", "content": "SECRET-A1"}
U2 = {"role": "user", "content": "SECRET-U2"}


def _f(t, key, msgs, **kw):
    return t.fields(key, ph.observe_payload(_payload(msgs, **kw)))


def test_first_request_has_no_previous():
    f = _f(ph.PrefixTracker(), "c", [S, U1])
    assert f["n_msgs"] == 2 and f["keep_alive"] == 600
    assert f["prefix_common_with_prev"] is None and f["appended_only"] is None
    assert len(f["prefix_sha_sys"]) == 16


def test_append_only_turns():
    t = ph.PrefixTracker()
    _f(t, "c", [S, U1])
    f = _f(t, "c", [S, U1, A1, U2])
    assert f["prefix_common_with_prev"] == 2 and f["appended_only"] is True


def test_rewritten_middle_message_is_not_append_only():
    t = ph.PrefixTracker()
    _f(t, "c", [S, U1, A1])
    f = _f(t, "c", [S, dict(U1, content="rewritten"), A1, U2])
    assert f["prefix_common_with_prev"] == 1 and f["appended_only"] is False


def test_system_or_tools_change_breaks_append_only():
    t = ph.PrefixTracker()
    _f(t, "c", [S, U1], tools=[{"n": 1}])
    f = _f(t, "c", [S, U1, A1], tools=[{"n": 2}])
    assert f["prefix_common_with_prev"] == 2 and f["appended_only"] is False


def test_conversations_are_independent_and_lru_bounded():
    t = ph.PrefixTracker(max_conversations=2)
    _f(t, "a", [S, U1]); _f(t, "b", [S, U1]); _f(t, "c", [S, U1])
    assert len(t._last) == 2
    assert _f(t, "a", [S, U1])["appended_only"] is None  # evicted, so unknown
    assert _f(t, "c", [S, U1, A1])["appended_only"] is True


def test_no_prompt_text_in_fields():
    t = ph.PrefixTracker()
    _f(t, "c", [S, U1])
    blob = json.dumps(_f(t, "c", [S, U1, A1, U2]))
    assert "SECRET" not in blob


def test_failures_never_raise():
    assert ph.observe_payload({"messages": [object()]}) is None
    assert ph.PrefixTracker().fields("c", {"sys": "x"}) == {}  # missing "msgs"
    row = {"session_id": "s"}
    ps._note_prefix(row, None, {"prefix_obs": {"sys": "x", "msgs": ["h"]}})  # body None
    ps._note_prefix(row, {}, None)


async def test_backend_reports_and_proxy_flattens_prefix_fields():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        lines = [{"message": {"content": "o"}, "done": False},
                 {"message": {"content": ""}, "done": True, "eval_count": 1}]
        return httpx.Response(200, text="".join(json.dumps(x) + "\n" for x in lines))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    b = pb.OllamaBackend("ollama/x", client, base_url="http://127.0.0.1:1", num_ctx=32768, keep_alive=-1)
    body = {"model": "m", "max_tokens": 64, "system": "sys",
            "messages": [{"role": "user", "content": "hi"}]}
    _m, _err, usage = await b.complete(body, 5.0)
    row = {"session_id": "sess-prefix-test"}
    ps._note_prefix(row, body, usage)
    assert "prefix_obs" not in usage
    assert row["keep_alive"] == -1 and row["n_msgs"] == len(seen[0]["messages"])
    assert row["appended_only"] is None and len(row["prefix_sha_sys"]) == 16
    body2 = dict(body, messages=body["messages"] + [{"role": "assistant", "content": "o"},
                                                    {"role": "user", "content": "more"}])
    _m, _err, usage2 = await b.complete(body2, 5.0)
    row2 = {"session_id": "sess-prefix-test"}
    ps._note_prefix(row2, body2, usage2)
    assert row2["appended_only"] is True and row2["prefix_common_with_prev"] == row["n_msgs"]
