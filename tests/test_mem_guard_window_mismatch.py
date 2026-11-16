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
