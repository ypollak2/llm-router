"""PHANTOM-LOCAL-1: a local route must not spend its first attempt on a model Ollama does not have.

Live 2026-11-02..03 (routing_quality.jsonl, 7 days to 2026-11-04T11:00Z): 9 of 94 local attempts
(72 local routes) were Ollama's 404 ``model 'qwen3.8:latest' not found``, 7 of them the first attempt
of their route, and ``qwen3.5:latest`` answered next. The model was removed on 2026-10-11. It stayed
named in an MCP server's ``env`` block (``OLLAMA_BUDGET_MODELS``), and three places believed that list:

* ``model_discovery._inventory`` (so ``local_model()`` called the user's real, installed setting "not
  installed" because it was not in the list, and returned the list's first, missing, name),
* the session-start dynamic routing table, which carries ``ollama/@local`` resolved from that and was
  never filtered against what is installed (STALE-OLLAMA-1 recorded this and left it),
* ``Config.all_ollama_models()``' budget-list fallback (``ollama_can_generate`` fails open on the 404).

Each is covered below; the runtime guard covers any source that is left.
"""
from __future__ import annotations

import json
import logging
import time
from unittest.mock import AsyncMock, patch

import litellm
import pytest

from llm_router import discover, router
from llm_router import model_discovery as md
from llm_router.router import route_and_call
from llm_router.types import Complexity, LLMResponse, RoutingProfile, TaskType

GHOST = "ollama/ghost:7b"
GHOST2 = "ollama/gone:8b"
REAL = "ollama/real:7b"
_PROMPTS = iter(f"Write a Python function named add_phantom_{i} that returns a + b" for i in range(1000))


# ── 1. what Ollama reports installed decides which operator-named models are believed ──────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for v in ("LLM_ROUTER_OLLAMA_MODEL", "OLLAMA_BUDGET_MODELS", "OLLAMA_MODELS",
              "LLM_ROUTER_DISCOVERY_TTL_HOURS"):
        monkeypatch.delenv(v, raising=False)
    getattr(md, "_not_installed_warned", set()).clear()
    yield tmp_path
    getattr(md, "_not_installed_warned", set()).clear()


def _cache(home, models, age_hours=1):
    (home / "discovery.json").write_text(json.dumps({
        "cached_at": time.time() - age_hours * 3600,
        "models": {f"ollama/{m}": {"model_id": f"ollama/{m}"} for m in models},
    }))


def test_installed_only_drops_names_a_fresh_list_does_not_carry(home, monkeypatch):
    _cache(home, ["real:latest", "other:7b"])
    monkeypatch.setattr(md, "probe_ollama", lambda: pytest.fail("probed a fresh cache"))
    # ``real`` and ``real:latest`` are one model to Ollama
    assert md.installed_only(["ghost:latest", "real", "other:7b"]) == ["real", "other:7b"]


def test_installed_only_keeps_the_names_when_the_list_is_unknown(home, monkeypatch):
    """Nothing to check against (Ollama down, no cache, or a cache too old to call a model missing)."""
    monkeypatch.setattr(md, "probe_ollama", lambda: [])
    assert md.installed_only(["ghost:latest"], allow_probe=True) == ["ghost:latest"]
    _cache(home, ["real:latest"], age_hours=48)
    assert md.installed_only(["ghost:latest"]) == ["ghost:latest"], "a stale cache must not drop a model pulled since"


def test_installed_only_returns_nothing_when_the_list_is_known_and_has_none_of_them(home):
    _cache(home, ["real:latest"])
    assert md.installed_only(["ghost:latest", "gone:8b"]) == []


def test_installed_only_probes_a_stale_cache_when_allowed_and_refreshes_it(home, monkeypatch):
    _cache(home, ["yesterday:latest"], age_hours=48)
    monkeypatch.setattr(md, "probe_ollama", lambda: ["today:latest"])
    assert md.installed_only(["today:latest", "ghost:latest"], allow_probe=True) == ["today:latest"]
    assert "ollama/today:latest" in json.loads((home / "discovery.json").read_text())["models"]


def test_a_missing_budget_name_does_not_reach_the_model_list(home, monkeypatch):
    _cache(home, ["coder:30b", "mine:latest"])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:latest,coder:30b,gone:8b")
    assert md.available_ollama_models(allow_probe=False) == ["coder:30b"]
    assert md.available_ollama_models_nowait(refresh=False) == ["coder:30b"]


def test_a_budget_list_with_nothing_installed_falls_back_to_discovery(home, monkeypatch):
    _cache(home, ["coder:30b"])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:latest,gone:8b")
    assert md.available_ollama_models(allow_probe=False) == ["coder:30b"]


def test_the_explicit_single_setting_still_wins_as_written(home, monkeypatch):
    _cache(home, ["cached:latest"])
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "chosen:latest")
    assert md.available_ollama_models() == ["chosen:latest"]


def test_local_model_returns_the_installed_setting_even_when_the_budget_list_omits_it(home, monkeypatch):
    """The live case: setting qwen3.8-uncensored (installed), list qwen3.8:latest (not) + others."""
    _cache(home, ["coder:30b", "mine:latest"])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:latest,coder:30b")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "mine:latest")
    assert md.local_model(allow_probe=False) == "mine:latest"
    assert md.last_skip_reason is None


def test_local_model_without_a_setting_never_returns_a_missing_budget_name(home, monkeypatch):
    _cache(home, ["coder:30b", "mine:latest"])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:latest,coder:30b")
    assert md.local_model(allow_probe=False) == "coder:30b"


def test_a_setting_that_is_really_missing_is_skipped_and_said_once(home, monkeypatch, caplog):
    _cache(home, ["coder:30b"])
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_MODEL", "ghost:latest")
    with caplog.at_level(logging.WARNING, logger="llm_router.model_discovery"):
        assert md.local_model(allow_probe=False) == "coder:30b"
        assert md.local_model(allow_probe=False) == "coder:30b"
        assert md.local_model(allow_probe=False) == "coder:30b"
    said = [r for r in caplog.records if "not installed" in r.getMessage()]
    assert len(said) == 1, "callers run on every route; the warning is once per process"
    assert md.last_skip_reason and "ghost:latest" in md.last_skip_reason


def test_each_dropped_name_is_logged_once(home, monkeypatch, caplog):
    _cache(home, ["coder:30b"])
    with caplog.at_level(logging.WARNING, logger="llm_router.model_discovery"):
        for _ in range(3):
            md.installed_only(["ghost:latest", "coder:30b"])
    assert [r.getMessage() for r in caplog.records if "ghost:latest" in r.getMessage()].__len__() == 1


def test_budget_fallback_of_the_config_drops_a_name_ollama_does_not_have(home, monkeypatch):
    """``ollama_can_generate`` fails open on the 404; the list is checked against Ollama's own first."""
    from llm_router.config import RouterConfig

    _cache(home, ["coder:30b"])
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    monkeypatch.setattr(discover, "ollama_can_generate", lambda name: True)
    cfg = RouterConfig(ollama_base_url="http://127.0.0.1:9", ollama_budget_models="ghost:latest,coder:30b")
    assert cfg.all_ollama_models() == ["ollama/coder:30b"]


# ── 2. the session-start dynamic table is filtered like the static chains ──────────────────────────


@pytest.fixture
def fake_local(mock_env, monkeypatch):
    from llm_router import config as config_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "real:7b")   # P0.15: configured, so the promote gate lets it lead
    config_module._config = None
    monkeypatch.setattr(discover, "get_cached_ollama_models", lambda: [REAL])
    monkeypatch.setattr(discover, "is_ollama_warm", lambda: False)
    monkeypatch.setattr(config_module.RouterConfig, "all_ollama_models", lambda self: [REAL])
    monkeypatch.setattr(router, "is_codex_available", lambda: False)
    monkeypatch.setattr(router, "is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda *a, **k: 0.0)
    yield config_module.get_config()
    from llm_router import dynamic_routing

    dynamic_routing.reset_dynamic_routing()


@pytest.mark.asyncio
async def test_a_dynamic_table_entry_for_an_uninstalled_model_is_not_in_the_chain(fake_local):
    """``ollama/@local`` was resolved when the table was built; the table is kept for the server's life."""
    from llm_router import dynamic_routing

    dynamic_routing._dynamic_routing_table = {
        (RoutingProfile.BUDGET, TaskType.QUERY): [GHOST, "openai/gpt-4o-mini"],
    }
    chain = await router._build_and_filter_chain(
        TaskType.QUERY, RoutingProfile.BUDGET, None, "simple", Complexity.SIMPLE, fake_local)
    assert GHOST not in chain, chain
    assert chain[0] == REAL, "the installed local model still leads"
    assert "openai/gpt-4o-mini" in chain


@pytest.mark.asyncio
async def test_a_failure_of_the_dynamic_filter_is_a_recorded_fail_open(fake_local, monkeypatch):
    from llm_router import dynamic_routing, failopen

    seen: list[str] = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))
    monkeypatch.setattr(discover, "filter_ollama_by_installed",
                        lambda chain: (_ for _ in ()).throw(RuntimeError("cache unreadable")))
    dynamic_routing._dynamic_routing_table = {
        (RoutingProfile.BUDGET, TaskType.QUERY): [GHOST, "openai/gpt-4o-mini"],
    }
    chain = await router._build_and_filter_chain(
        TaskType.QUERY, RoutingProfile.BUDGET, None, "simple", Complexity.SIMPLE, fake_local)
    assert chain, "fail-open must still return a chain"
    assert "CHZ-FO-ROUTER-DYNAMIC-OLLAMA-FILTER" in seen


# ── 3. runtime guard: Ollama said "not found" once, later routes do not ask again ─────────────────────


@pytest.fixture(autouse=True)
def _fresh_missing_state():
    getattr(router, "_local_missing_at", {}).clear()
    yield
    getattr(router, "_local_missing_at", {}).clear()


def _not_found(model: str) -> Exception:
    name = model.split("/", 1)[1]
    return litellm.NotFoundError(
        message=f"OllamaException - {{\"error\":\"model '{name}' not found\"}}",
        model=model, llm_provider="ollama")


def _ok(model: str) -> LLMResponse:
    return LLMResponse(content="def add_phantom(a, b):\n    return a + b\n", model=model,
                       input_tokens=20, output_tokens=12, cost_usd=0.0,
                       latency_ms=50.0, provider=model.split("/", 1)[0])


async def _route(chain, failing=None, calls=None, **kw):
    """One route over a pinned *chain*; returns (calls, response). A fresh prompt per route: a repeated
    one is answered by the semantic cache."""
    calls = [] if calls is None else calls
    failing = failing if failing is not None else {m: _not_found(m) for m in (GHOST, GHOST2)}

    async def _call(model, *a, **k):
        calls.append(model)
        if model in failing:
            raise failing[model]
        return _ok(model)

    with patch("llm_router.router._build_and_filter_chain", new=AsyncMock(return_value=list(chain))), \
         patch("llm_router.router._call_text", new=AsyncMock(side_effect=_call)):
        resp = await route_and_call(TaskType.CODE, next(_PROMPTS), complexity_hint="simple", **kw)
    return calls, resp


@pytest.mark.asyncio
async def test_a_model_ollama_does_not_have_is_not_tried_on_the_next_route(temp_db, mock_env, monkeypatch):
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:7b,real:7b")
    calls, resp = await _route([GHOST, REAL])
    assert calls == [GHOST, REAL] and resp.model == REAL

    calls, resp = await _route([GHOST, REAL])
    assert calls == [REAL], "the second route asked Ollama for the model it had just said it does not have"
    assert resp.model == REAL


@pytest.mark.asyncio
async def test_missing_models_do_not_bench_the_installed_ones_behind_them(temp_db, mock_env, monkeypatch):
    """Two 404s counted as two provider failures (threshold 2) and benched every Ollama model."""
    from llm_router.health import get_tracker

    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:7b,gone:8b,real:7b")
    calls, resp = await _route([GHOST, GHOST2, REAL])
    assert calls == [GHOST, GHOST2, REAL]
    assert resp.model == REAL
    assert get_tracker().is_healthy("ollama")


@pytest.mark.asyncio
async def test_a_genuine_provider_error_still_counts_against_the_provider(temp_db, mock_env, monkeypatch):
    from llm_router.health import get_tracker

    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:7b,gone:8b,real:7b")
    failing = {GHOST: RuntimeError("model failed to load"), GHOST2: RuntimeError("model failed to load")}
    calls, _ = await _route([GHOST, GHOST2, "openai/gpt-4o-mini"], failing)
    assert calls == [GHOST, GHOST2, "openai/gpt-4o-mini"]
    assert not get_tracker().is_healthy("ollama")
    assert router._local_missing_at == {}


@pytest.mark.asyncio
async def test_the_model_is_tried_again_after_the_lifetime(temp_db, mock_env, monkeypatch):
    """A model pulled in the meantime must come back without a restart."""
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:7b,real:7b")
    await _route([GHOST, REAL])
    router._local_missing_at[GHOST] = time.monotonic() - (router._LOCAL_MISSING_TTL_S + 1)

    calls, _ = await _route([GHOST, REAL], failing={})
    assert calls == [GHOST]


@pytest.mark.asyncio
async def test_the_chain_is_never_emptied_by_the_drop(temp_db, mock_env, monkeypatch):
    """When every entry is missing the error the caller sees is Ollama's own, not an empty chain."""
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ghost:7b,gone:8b")
    router._local_missing_at.update({GHOST: time.monotonic(), GHOST2: time.monotonic()})
    calls: list[str] = []
    with pytest.raises(RuntimeError):
        await _route([GHOST, GHOST2], calls=calls)
    assert calls == [GHOST, GHOST2]


def test_only_an_ollama_model_not_found_is_remembered():
    nf = _not_found(GHOST)
    assert router._note_local_missing(GHOST, nf) is True
    router._local_missing_at.clear()
    # another provider's 404, and an Ollama error that is not "model not found"
    assert router._note_local_missing("openai/gpt-4o-mini", nf) is False
    assert router._note_local_missing(GHOST, RuntimeError("model 'ghost:7b' not found")) is False
    wrong_endpoint = litellm.NotFoundError(message="OllamaException - 404 page not found",
                                           model=GHOST, llm_provider="ollama")
    assert router._note_local_missing(GHOST, wrong_endpoint) is False, "a 404 on the route is not a missing model"
    assert router._local_missing_at == {}


def test_drop_missing_local_keeps_order_and_remembers_the_cause():
    router._local_missing_at[GHOST] = time.monotonic()
    assert router._drop_missing_local([GHOST, REAL, "openai/gpt-4o-mini"]) == [REAL, "openai/gpt-4o-mini"]
    assert router._drop_missing_local([REAL]) == [REAL]
    router._local_missing_at[GHOST] = time.monotonic() - (router._LOCAL_MISSING_TTL_S + 1)
    assert router._drop_missing_local([GHOST, REAL]) == [GHOST, REAL]


def test_the_list_is_checked_against_the_daemon_that_serves(monkeypatch):
    """Review of PHANTOM-LOCAL-1: the probe asked OLLAMA_HOST or the default while the hook called LLM_ROUTER_OLLAMA_URL, so
    an operator's model on the serving daemon was dropped."""
    from llm_router import model_discovery as md
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:11999")
    assert md._ollama_url() == "http://127.0.0.1:11999"
    monkeypatch.delenv("LLM_ROUTER_OLLAMA_URL")
    assert md._ollama_url() == "http://127.0.0.1:9"
    monkeypatch.delenv("OLLAMA_HOST")
    assert md._ollama_url() == "http://127.0.0.1:11434"

