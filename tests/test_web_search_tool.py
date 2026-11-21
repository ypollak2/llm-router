"""web_search: formatting, cap, error kinds, flag gating, alwaysLoad _meta, log hygiene.

No network: a fake SearXNG is an httpx.MockTransport.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from llm_router.call_identity import IdentityMCPServer
from llm_router.search import backend
from llm_router.tools import web_search as ws

SECRET_QUERY = "zebra-quokka-private-needle"


def _fake(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


def _json(results, status=200):
    return lambda req: httpx.Response(status, json={"results": results})


def _run(coro):
    return asyncio.run(coro)


def test_search_parses_and_limits():
    rows = [{"title": f"T{i}", "url": f"https://e{i}.org/x", "content": f"c{i}"} for i in range(8)]
    seen = {}

    def h(req):
        seen["q"] = req.url.params["q"]
        seen["fmt"] = req.url.params["format"]
        return httpx.Response(200, json={"results": rows + [{"title": "no url"}]})

    hits = _run(backend.search("asyncio", 3, client=_fake(h)))
    assert [x.title for x in hits] == ["T0", "T1", "T2"]
    assert seen == {"q": "asyncio", "fmt": "json"}


def test_n_is_clamped():
    rows = [{"title": f"T{i}", "url": f"https://e{i}.org", "content": ""} for i in range(30)]
    assert len(_run(backend.search("q", 99, client=_fake(_json(rows))))) == backend.MAX_RESULTS_CEILING
    assert len(_run(backend.search("q", 0, client=_fake(_json(rows))))) == 1


def test_format_plain_text():
    out = ws.format_hits([backend.Hit("Title", "https://a.org/p", "snip")])
    assert out == "1. Title\n   https://a.org/p\n   snip"
    assert ws.format_hits([]) == "0 results"


def test_cap_is_two_thousand_est_tokens():
    from llm_router.token_budget import estimate_tokens
    hits = [backend.Hit("T" * 100, f"https://e{i}.org/" + "u" * 200, "s" * 5000) for i in range(10)]
    out = ws.format_hits(hits)
    assert estimate_tokens(out) <= ws.MAX_EST_TOKENS
    assert len(out) > 1000  # not trivially empty


def test_cap_marks_truncation():
    hits = [backend.Hit("T" * 100, "https://e.org/" + "u" * 300, "s" * 399) for _ in range(10)]
    assert "[truncated:" in ws.format_hits(hits)


@pytest.mark.parametrize("handler,kind", [
    (lambda r: httpx.Response(503, text="body with " + SECRET_QUERY), "http_503"),
    (lambda r: httpx.Response(200, text="<html>not json"), "bad_json"),
    (lambda r: httpx.Response(200, json={"nope": 1}), "bad_json"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("t")), "timeout"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("c")), "connect"),
])
def test_error_kinds(handler, kind, monkeypatch):
    async def go():
        c = _fake(handler)
        try:
            return await ws.web_search(SECRET_QUERY, 3)
        finally:
            await c.aclose()
    real = backend.search

    async def patched(q, n, **kw):
        return await real(q, n, client=_fake(handler))
    monkeypatch.setattr(backend, "search", patched)
    out = _run(go())
    assert out == f"web_search failed: try=1 kind={kind}"
    assert SECRET_QUERY not in out


def test_empty_query():
    assert _run(ws.web_search("   ")) == "web_search failed: try=1 kind=empty_query"


def test_no_query_text_in_logs(monkeypatch):
    calls = []

    class L:
        def info(self, event, **kw):
            calls.append((event, kw))

    monkeypatch.setattr(ws, "log", L())

    async def fake_search(q, n, **kw):
        return [backend.Hit("t", "https://a.org", "s")]
    monkeypatch.setattr(backend, "search", fake_search)
    _run(ws.web_search(SECRET_QUERY, 2))

    async def boom(q, n, **kw):
        raise backend.SearchError("http_500")
    monkeypatch.setattr(backend, "search", boom)
    _run(ws.web_search(SECRET_QUERY, 2))
    assert calls
    assert SECRET_QUERY not in repr(calls)
    assert any(kw.get("query_len") == len(SECRET_QUERY) for _, kw in calls)


def _tools(server):
    return {t.name: t for t in _run(server.list_tools())}


def test_flag_off_tool_absent(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_WEB_SEARCH", raising=False)
    s = IdentityMCPServer("t")
    ws.register(s)
    assert "web_search" not in _tools(s)
    monkeypatch.setenv("LLM_ROUTER_WEB_SEARCH", "off")
    s = IdentityMCPServer("t")
    ws.register(s)
    assert "web_search" not in _tools(s)


def test_flag_on_tool_listed_with_always_load(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_WEB_SEARCH", "on")
    s = IdentityMCPServer("t")
    ws.register(s)
    tool = _tools(s)["web_search"]
    dumped = tool.model_dump(by_alias=True)
    assert dumped["_meta"] == {"anthropic/alwaysLoad": True}
    props = dumped["inputSchema"]["properties"]
    assert set(props) == {"query", "max_results"}
    assert dumped["inputSchema"]["required"] == ["query"]
    assert props["max_results"]["default"] == 5


def test_base_url_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_SEARXNG_URL", raising=False)
    assert backend.base_url() == "http://127.0.0.1:8888"
    monkeypatch.setenv("LLM_ROUTER_SEARXNG_URL", "http://127.0.0.1:9999/")
    assert backend.base_url() == "http://127.0.0.1:9999"
