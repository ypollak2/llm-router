"""F108 / D-235: the F106 failed-eligibility filter on the remaining doors.

Doors: the hook chain builder (``hooks.chain_builder.build_chain``, shared by the auto-route
DIRECT path, agent-route and ``sdk``) and ``router.route_and_stream`` (TUI). Same function
(``eligibility_routing.filter_chain``), same lane-tier rule, same opt-out and fail-open as F106.
Lanes are built from counts through ``eligibility`` itself; model names are fixtures.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from llm_router import eligibility_routing as er, failopen, skip_causes
from llm_router.hooks import chain_builder as cb
from llm_router.hooks.direct_executor import ModelSpec
from llm_router.types import RoutingProfile, TaskType
from tests.test_f106_eligibility_in_router import (
    ELIGIBLE, FAILED, INCONCLUSIVE, NOT_INFORMATIVE, PROBE,
)

BAD = ModelSpec("ollama", "fixture-bad:7b")
OTHER = ModelSpec("ollama", "fixture-other:3b")
GEM = ModelSpec("gemini", "gemini-2.5-flash")
SRC = Path(__file__).resolve().parents[1] / "src" / "llm_router"


def _ids(chain) -> list[str]:
    return [c if isinstance(c, str) else f"{c.provider}/{c.model}" for c in chain]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    monkeypatch.delenv("LLM_ROUTER_IGNORE_ELIGIBILITY", raising=False)
    er.reset_cache()
    failopen.reset_cache()
    yield h
    er.reset_cache()


def _write(home, lane_name: str, lane: dict, tiers=("simple", "moderate")) -> None:
    body = {"fixture-bad:7b": {lane_name: {t: lane for t in tiers}}}
    (home / "local_eligibility.json").write_text(json.dumps(body))


@pytest.fixture
def hook(home, monkeypatch):
    """build_chain over a stubbed chain that leads with the failed local model."""
    monkeypatch.setattr(cb, "_build_chain", lambda *a, **k: [BAD, GEM, OTHER])
    monkeypatch.setattr(cb, "_sink_unvalidated_specs", lambda chain, tt: chain)
    monkeypatch.setattr("llm_router.map_reader.apply_to_chain", lambda chain, **k: chain)
    return home


# ── hook door: build_chain ─────────────────────────────────────────────────

@pytest.mark.parametrize("task,lane", [("code", "code"), ("query", "qa"), ("generate", "qa"),
                                       ("analyze", "qa")])
@pytest.mark.parametrize("tier", ["simple", "moderate"])
def test_hook_chain_drops_failed_local_lane(hook, task, lane, tier):
    _write(hook, lane, FAILED)
    # qa_last_resort=True is the SDK / zero-Claude shape that keeps local Q&A (D-116).
    chain = cb.build_chain(tier, "green", task, qa_last_resort=True)
    assert _ids(chain) == ["gemini/gemini-2.5-flash", "ollama/fixture-other:3b"]


@pytest.mark.parametrize("tier", ["complex", "deep_reasoning"])
def test_hook_complex_untouched(hook, tier):
    _write(hook, "code", FAILED, tiers=("simple", "moderate", "complex"))
    assert _ids(cb.build_chain(tier, "green", "code")) == _ids([BAD, GEM, OTHER])


@pytest.mark.parametrize("lane", [ELIGIBLE, INCONCLUSIVE, NOT_INFORMATIVE, PROBE])
def test_hook_unknown_inconclusive_eligible_unchanged(hook, lane):
    _write(hook, "code", lane)
    assert _ids(cb.build_chain("simple", "green", "code")) == _ids([BAD, GEM, OTHER])


def test_hook_other_lane_unchanged(hook):
    _write(hook, "qa", FAILED)  # only the qa lane failed; code is unmeasured
    assert _ids(cb.build_chain("simple", "green", "code")) == _ids([BAD, GEM, OTHER])


def test_hook_opt_out(hook, monkeypatch):
    _write(hook, "code", FAILED)
    monkeypatch.setenv("LLM_ROUTER_IGNORE_ELIGIBILITY", "1")
    assert _ids(cb.build_chain("simple", "green", "code")) == _ids([BAD, GEM, OTHER])


def test_hook_filter_survives_map_reader_failure(hook, monkeypatch):
    _write(hook, "code", FAILED)

    def boom(*a, **k):
        raise RuntimeError("map unreadable")

    monkeypatch.setattr("llm_router.map_reader.apply_to_chain", boom)
    assert _ids(cb.build_chain("simple", "green", "code")) == [
        "gemini/gemini-2.5-flash", "ollama/fixture-other:3b"]


def test_hook_failopen_keeps_chain_and_records(hook, monkeypatch):
    _write(hook, "code", FAILED)

    def boom(*a, **k):
        raise RuntimeError("filter broke")

    monkeypatch.setattr(er, "filter_chain", boom)
    assert _ids(cb.build_chain("simple", "green", "code")) == _ids([BAD, GEM, OTHER])
    assert failopen.snapshot().by_code.get("CHZ-FO-HOOK-ELIGIBILITY-FILTER") == 1


def test_hook_reason_visible_when_a_skip_scope_is_open(hook):
    _write(hook, "code", FAILED)
    skip_causes.begin()
    cb.build_chain("simple", "green", "code")
    entries = skip_causes.entries_so_far()
    assert [(e["model"], e["reason"], e["lane"]) for e in entries] == [
        ("ollama/fixture-bad:7b", "failed_eligibility", "code/simple")]
    assert (entries[0]["k"], entries[0]["n"]) == (13, 113)


def test_hook_cost_is_the_cached_stat_not_a_parse_per_call(hook, monkeypatch):
    _write(hook, "code", FAILED)
    cb.build_chain("simple", "green", "code")
    parses = []
    real = er._parse
    monkeypatch.setattr(er, "_parse", lambda t: parses.append(1) or real(t))
    for _ in range(50):
        cb.build_chain("simple", "green", "code")
    assert parses == []


def test_hook_empty_chain_is_returned_as_is(hook, monkeypatch):
    monkeypatch.setattr(cb, "_build_chain", lambda *a, **k: [])
    assert cb.build_chain("simple", "green", "code") == []


# ── SDK door: sdk._route reaches the same builder ──────────────────────────

def test_sdk_route_chain_has_no_failed_local_model(hook, monkeypatch):
    from llm_router import sdk
    from llm_router.hooks import direct_executor as de

    _write(hook, "code", FAILED)
    seen: list = []
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: False)
    monkeypatch.setattr(de, "execute_chain", lambda request, chain, *a, **k: seen.append(chain))
    monkeypatch.setattr(sdk, "_pack_kwargs", lambda *a, **k: (a[0], None))
    with pytest.raises(Exception):  # noqa: B017,PT011 - the stub serves nothing: RoutingError
        sdk._route("write a function", task_type="code", complexity="simple", timeout=5,
                   session_id="s")
    assert len(seen) == 1
    assert _ids(seen[0]) == ["gemini/gemini-2.5-flash", "ollama/fixture-other:3b"]


def test_every_hook_and_sdk_chain_is_built_by_build_chain():
    """The seam: if one of these stops using ``build_chain`` the filter has to follow it."""
    for rel in ("sdk.py", "hooks/auto-route.py", "hooks/agent-route.py"):
        text = (SRC / rel).read_text()
        assert re.search(r"chain_builder import[^)]*\bbuild_chain\b", text, re.S), rel
    assert "_drop_failed_eligibility(" in (SRC / "hooks" / "chain_builder.py").read_text()


# ── stream door: route_and_stream ──────────────────────────────────────────

CHAIN = ["ollama/fixture-bad:7b", "gemini/gemini-2.5-flash", "ollama/fixture-other:3b"]


def _stream(monkeypatch, *, task=TaskType.CODE, hint="simple", classification=None,
            override=None, chain=CHAIN):
    from llm_router import providers, router

    attempted: list[str] = []

    async def fake_stream(*, model, messages, **kw):
        attempted.append(model)
        raise RuntimeError("provider down")
        yield  # pragma: no cover

    async def fake_ctx(**kw):
        return [{"role": "user", "content": "x"}]

    monkeypatch.setattr(router, "get_model_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(providers, "call_llm_stream_events", fake_stream)
    monkeypatch.setattr(router, "build_context_messages", fake_ctx)
    events: list[dict] = []

    async def go():
        async for ev in router.route_and_stream(
                task, "p", profile=RoutingProfile.BALANCED, complexity_hint=hint,
                classification_data=classification, model_override=override):
            events.append(ev)

    exc = None
    try:
        asyncio.run(go())
    except Exception as e:  # noqa: BLE001 - asserted by the caller
        exc = e
    return attempted, exc, events


@pytest.fixture
def stream_env(home, mock_env, temp_db, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    return home


@pytest.mark.parametrize("task,lane", [(TaskType.CODE, "code"), (TaskType.QUERY, "qa"),
                                       (TaskType.GENERATE, "qa")])
@pytest.mark.parametrize("tier", ["simple", "moderate"])
def test_stream_drops_failed_local_lane_and_shows_reason(stream_env, monkeypatch, task, lane, tier):
    _write(stream_env, lane, FAILED)
    attempted, _, events = _stream(monkeypatch, task=task, hint=tier)
    assert attempted == ["gemini/gemini-2.5-flash", "ollama/fixture-other:3b"]
    started = next(e for e in events if e["type"] == "route.started")
    assert started["eligibility_skipped"] == ["ollama/fixture-bad:7b"]
    assert started["candidate_count"] == 2


def test_stream_lane_tier_comes_from_classification_without_a_hint(stream_env, monkeypatch):
    _write(stream_env, "code", FAILED)
    attempted, _, _ = _stream(monkeypatch, hint=None,
                              classification={"complexity": "simple"})
    assert attempted[0] == "gemini/gemini-2.5-flash"


@pytest.mark.parametrize("tier", ["complex", "deep_reasoning"])
def test_stream_complex_untouched(stream_env, monkeypatch, tier):
    _write(stream_env, "code", FAILED, tiers=("simple", "moderate", "complex"))
    attempted, _, events = _stream(monkeypatch, hint=tier)
    assert attempted == CHAIN
    assert next(e for e in events if e["type"] == "route.started")["eligibility_skipped"] == []


@pytest.mark.parametrize("lane", [ELIGIBLE, INCONCLUSIVE, NOT_INFORMATIVE, PROBE])
def test_stream_unknown_inconclusive_eligible_unchanged(stream_env, monkeypatch, lane):
    _write(stream_env, "code", lane)
    attempted, _, _ = _stream(monkeypatch)
    assert attempted == CHAIN


def test_stream_opt_out(stream_env, monkeypatch):
    _write(stream_env, "code", FAILED)
    monkeypatch.setenv("LLM_ROUTER_IGNORE_ELIGIBILITY", "1")
    attempted, _, _ = _stream(monkeypatch)
    assert attempted == CHAIN


def test_stream_model_override_is_exempt(stream_env, monkeypatch):
    _write(stream_env, "code", FAILED)
    attempted, _, _ = _stream(monkeypatch, override="ollama/fixture-bad:7b")
    assert attempted == CHAIN


def test_stream_missing_file_unchanged(stream_env, monkeypatch):
    attempted, _, _ = _stream(monkeypatch)
    assert attempted == CHAIN


def test_stream_filter_emptied_chain_names_the_cause(stream_env, monkeypatch):
    _write(stream_env, "code", FAILED)
    _, exc, _ = _stream(monkeypatch, chain=["ollama/fixture-bad:7b"])
    assert exc is not None and "local eligibility removed ollama/fixture-bad:7b" in str(exc)


def test_stream_failopen_keeps_chain(stream_env, monkeypatch):
    _write(stream_env, "code", FAILED)
    monkeypatch.setattr(er, "filter_chain", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    attempted, _, _ = _stream(monkeypatch)
    assert attempted == CHAIN
    assert failopen.snapshot().by_code.get("CHZ-FO-ROUTER-ELIGIBILITY-FILTER") == 1
