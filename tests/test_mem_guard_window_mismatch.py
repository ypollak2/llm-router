"""STALL-1: a resident model whose window differs from the requested one is a reload, in either direction.

Incident 2026-11-16: a runner held num_ctx 131072 while bench proxies asked for 32768. Ollama keys a runner on
num_ctx, so each request reloaded it; the memory guard read ``have >= want`` as "already resident" and never counted
the load. Every test fakes /api/ps and the host memory (see tests/test_mem_guard.py); no sockets.

The load is observable through the free-memory rule: with 6 GiB free a counted load of ~20 GB is refused, an
uncounted one returns without a size lookup.
"""
from __future__ import annotations

import pytest

from tests.test_mem_guard import GB, SMALL, TARGET, Fake, _env, host, row  # noqa: F401


def _resident(window):
    r = row(TARGET, 20)
    if window is not None:
        r["context_length"] = window
    return r


@pytest.fixture(autouse=True)
def _want_32768(monkeypatch):
    from llm_router import local_models
    from llm_router.toolkit.adapters import ollama as oa
    monkeypatch.setattr(local_models, "num_ctx", lambda name: 32768)
    monkeypatch.setattr(oa, "_trained_ctx", lambda base_url, name: None)


@pytest.mark.parametrize("have", [131072, 8192])
def test_a_resident_window_different_from_the_request_counts_as_a_load(monkeypatch, have):
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [_resident(have)]}, {TARGET: 20 * GB})
    host(monkeypatch, 48.0, 6.0)
    with pytest.raises(oa.MemoryBudgetExceeded) as ei:
        oa.ensure_fits(TARGET, SMALL, "t")
    assert ei.value.rule == "free"


def test_a_resident_window_equal_to_the_request_is_not_a_load(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [_resident(32768)]}, {TARGET: 20 * GB})
    host(monkeypatch, 48.0, 6.0)
    assert oa._verdict(TARGET, SMALL, None, "t") is None
    oa.ensure_fits(TARGET, SMALL, "t")
    assert not any(u.endswith("/api/tags") for u in fake.calls)      # no size lookup: nothing to load


def test_a_resident_model_with_no_reported_window_is_not_a_load(monkeypatch):
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [_resident(None)]}, {TARGET: 20 * GB})
    host(monkeypatch, 48.0, 6.0)
    assert oa._verdict(TARGET, SMALL, None, "t") is None
    assert not any(u.endswith("/api/tags") for u in fake.calls)


def test_the_callers_explicit_num_ctx_beats_the_table_value(monkeypatch):
    """The table says 32768. A caller that really sends 131072 (the proxy's --num-ctx) is judged on that."""
    from llm_router.toolkit.adapters import ollama as oa
    Fake(monkeypatch, {"11434": [_resident(131072)]}, {TARGET: 20 * GB})
    host(monkeypatch, 48.0, 6.0)
    assert oa._verdict(TARGET, SMALL, None, "t", num_ctx=131072) is None          # same window: not a load
    oa._PS_CACHE.clear()
    with pytest.raises(oa.MemoryBudgetExceeded):
        oa.ensure_fits(TARGET, SMALL, "t")                                         # table 32768 vs resident 131072
    oa._PS_CACHE.clear()
    oa.ensure_fits(TARGET, SMALL, "t", num_ctx=131072)                             # explicit: nothing to load


def test_the_mismatch_verdict_is_the_same_path_as_a_larger_request(monkeypatch):
    """131072 resident, 32768 asked, and 8192 resident, 32768 asked, give the same refusal rule."""
    from llm_router.toolkit.adapters import ollama as oa
    rules = []
    for have in (131072, 8192):
        oa._PS_CACHE.clear()
        Fake(monkeypatch, {"11434": [_resident(have)]}, {TARGET: 20 * GB})
        host(monkeypatch, 48.0, 6.0)
        verdict = oa._verdict(TARGET, SMALL, None, "t")
        assert verdict is not None
        rules.append(verdict[0].rule)
    assert rules[0] == rules[1] == "free"


# -- STALL review round 3: N1 (proxy edit step) and N3 (calibrate) guard the window they send ------------------------

class _OkResponse:
    status_code = 200
    def raise_for_status(self): return None
    def json(self): return {"message": {"content": "{}"}, "done_reason": "stop"}


class _PostClient:
    def __init__(self): self.posts = []
    async def post(self, url, **k):
        self.posts.append(k.get("json"))
        return _OkResponse()


def test_the_proxy_edit_step_is_judged_on_the_window_it_sends(monkeypatch):
    """Proxy at --num-ctx 131072, table 32768, runner resident at 131072, 6 GiB free: the edit generate is a hit.
    Guarding on the table value counted a phantom load, refused the step and always fell back to the raw edit."""
    import asyncio

    from llm_router.local_agent.proxy_step import generator_for
    from llm_router.proxy.backends import OllamaBackend
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [_resident(131072)]}, {TARGET: 20 * GB})
    host(monkeypatch, 48.0, 6.0)
    client = _PostClient()
    be = OllamaBackend("ollama/" + TARGET, client, base_url=SMALL, num_ctx=131072)
    oa._PS_CACHE.clear()
    text, _usage = asyncio.run(generator_for(be)("sys", "prompt", 50, 5.0))
    assert len(client.posts) == 1 and client.posts[0]["options"]["num_ctx"] == 131072   # sent, not refused
    assert text == "{}"
    assert not any(u.endswith("/api/tags") for u in fake.calls)                         # no phantom load counted


def test_a_calibration_probe_is_judged_on_its_explicit_window(monkeypatch):
    """An explicit num_ctx goes into the body and into the guard; without one the table value still applies."""
    from llm_router.resolver.calibrate import OllamaCompleter
    from llm_router.toolkit.adapters import ollama as oa
    fake = Fake(monkeypatch, {"11434": [_resident(131072)]}, {TARGET: 20 * GB})
    host(monkeypatch, 48.0, 6.0)
    posted = []

    def post(url, body, timeout):
        posted.append(body)
        return {"message": {"content": "ok"}}
    comp = OllamaCompleter(SMALL, post)
    oa._PS_CACHE.clear()
    reply = comp.chat("ollama/" + TARGET, [{"role": "user", "content": "x"}], num_ctx=131072)
    assert not reply.error and posted[0]["options"]["num_ctx"] == 131072
    assert not any(u.endswith("/api/tags") for u in fake.calls)                         # same window: not a load
    oa._PS_CACHE.clear()
    reply = comp.chat("ollama/" + TARGET, [{"role": "user", "content": "x"}])           # table 32768 vs resident 131072
    assert reply.error and len(posted) == 1                                             # refused as a reload
