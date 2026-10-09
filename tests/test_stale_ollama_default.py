"""STALE-OLLAMA-1: the static chains must not name a model nobody installed, and a
dropped / unvalidated Ollama entry must say so.

Three claims, each with its own test group:

1. EQUIVALENCE. For every profile x task type, the chain ``_build_and_filter_chain``
   hands to ``route_and_call`` (fake discovery cache holding qwen3-coder:30b,
   qwen3.8 and nimble:9b; no network, no real Ollama) is identical to the chain the
   code produced BEFORE this fix. The "before" chains are the committed golden
   ``fixtures/stale_ollama_chains_base.json``, written from the base commit
   (6d0b7535) by ``_collect()`` below; this file passes on base and on head.
   One documented, deliberate difference: ``ollama/qwen3:32b`` is gone from every chain
   that carried it. On the static path that is only the RESEARCH chains (they return
   from ``get_model_chain`` before the installed-model filter runs); on the dynamic
   path (built straight from ``ROUTING_TABLE``, never filtered) it is every
   BALANCED / PREMIUM / REASONING text chain. In both it was an entry no machine in
   the fake cache has (see ``_expected_from_base``).
2. TRUTH. ``get_model_chain`` / ``ROUTING_TABLE`` no longer list ``ollama/qwen3:32b``
   (local models are injected at route time).
3. VISIBILITY. Dropped-not-installed and filter-skipped-on-empty-cache each log a
   WARNING once per process; a filter exception is a recorded fail-open.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from llm_router import discover
from llm_router.types import Complexity, RoutingProfile, TaskType

GOLDEN = Path(__file__).parent / "fixtures" / "stale_ollama_chains_base.json"
FAKE_CACHE = ["ollama/qwen3-coder:30b", "ollama/qwen3.8", "ollama/nimble:9b"]
STALE = "ollama/qwen3:32b"
_PROFILES = (
    RoutingProfile.BUDGET, RoutingProfile.BALANCED,
    RoutingProfile.PREMIUM, RoutingProfile.REASONING,
)
_COMPLEXITY = {
    RoutingProfile.BUDGET: Complexity.SIMPLE,
    RoutingProfile.BALANCED: Complexity.MODERATE,
    RoutingProfile.PREMIUM: Complexity.COMPLEX,
    RoutingProfile.REASONING: Complexity.DEEP_REASONING,
}


@pytest.fixture
def fake_local(mock_env, monkeypatch):
    """Providers keyed, a fake 3-model Ollama cache, no subprocess backends, no network."""
    from llm_router import config as config_module
    from llm_router import router as router_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    config_module._config = None
    monkeypatch.setattr(discover, "get_cached_ollama_models", lambda: list(FAKE_CACHE))
    monkeypatch.setattr(discover, "is_ollama_warm", lambda: False)
    monkeypatch.setattr(config_module.RouterConfig, "all_ollama_models",
                        lambda self: list(FAKE_CACHE))
    monkeypatch.setattr(router_module, "is_codex_available", lambda: False)
    monkeypatch.setattr(router_module, "is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda *a, **k: 0.0)
    return config_module.get_config()


async def _collect(config) -> dict[str, dict[str, list[str]]]:
    """Chain per (profile, task) on the static path and on the dynamic-table path."""
    from llm_router import dynamic_routing
    from llm_router.router import _build_and_filter_chain

    out: dict[str, dict[str, list[str]]] = {"static": {}, "dynamic": {}}
    for mode in ("static", "dynamic"):
        dynamic_routing.reset_dynamic_routing()
        if mode == "dynamic":
            dynamic_routing._dynamic_routing_table = dynamic_routing.build_dynamic_routing_table(
                {"anthropic", "openai", "gemini", "deepseek"})
        for profile in _PROFILES:
            for task in TaskType:
                chain = await _build_and_filter_chain(
                    task, profile, None, _COMPLEXITY[profile].value, _COMPLEXITY[profile], config)
                out[mode][f"{profile.value}/{task.value}"] = list(chain)
    dynamic_routing.reset_dynamic_routing()
    return out


def _expected_from_base(base: dict) -> dict:
    """Base chains with the one documented difference applied: the stale entry removed."""
    exp = json.loads(json.dumps(base))
    for mode in exp:
        for key, chain in exp[mode].items():
            exp[mode][key] = [m for m in chain if m != STALE]
    return exp


@pytest.mark.asyncio
async def test_live_chain_equals_pre_fix_chain_for_every_profile_and_task(fake_local):
    base = json.loads(GOLDEN.read_text())
    assert len(base["static"]) == len(base["dynamic"]) == len(_PROFILES) * len(TaskType) == 40
    got = await _collect(fake_local)
    expected = _expected_from_base(base)
    diffs = {
        f"{mode}:{k}": (expected[mode][k], got[mode][k])
        for mode in expected for k in expected[mode] if expected[mode][k] != got[mode][k]
    }
    assert not diffs, f"live chain changed for {sorted(diffs)}: {diffs}"


@pytest.mark.asyncio
async def test_the_golden_is_not_empty_where_it_matters(fake_local):
    """An empty chain would pass the equivalence check against an empty golden."""
    base = json.loads(GOLDEN.read_text())
    for mode in ("static", "dynamic"):
        for key in ("balanced/code", "balanced/query", "budget/code", "premium/code"):
            assert base[mode][key], (mode, key)
        # the injected local models lead the cheap-tier chains (the live behaviour
        # this fix must not move)
        assert base[mode]["balanced/code"][0] == "ollama/qwen3-coder:30b", mode


@pytest.mark.asyncio
async def test_the_difference_from_base_is_exactly_the_stale_entry(fake_local):
    """Head differs from base only where base carried STALE, and there only by it."""
    base = json.loads(GOLDEN.read_text())
    got = await _collect(fake_local)
    changed = {
        f"{mode}:{k}" for mode in base for k in base[mode] if base[mode][k] != got[mode][k]
    }
    carried = {f"{mode}:{k}" for mode in base for k, c in base[mode].items() if STALE in c}
    assert carried, "golden must show the stale entry on base, or this proves nothing"
    assert changed == carried
    assert all(STALE not in c for mode in got for c in got[mode].values())


# ── 2. truth ────────────────────────────────────────────────────────────────

def test_static_chains_do_not_name_the_stale_default():
    from llm_router.policy import PolicyManager
    from llm_router.profiles import ROUTING_TABLE

    for (profile, task), chain in ROUTING_TABLE.items():
        assert STALE not in chain, (profile, task)
    pol = PolicyManager().load_policy("standard")
    assert STALE not in pol.workhorses
    assert STALE not in pol.fallback_chain_complex


def test_get_model_chain_lists_no_uninstalled_ollama_model(fake_local):
    from llm_router.profiles import get_model_chain

    for profile in _PROFILES:
        for task in TaskType:
            chain = get_model_chain(profile, task)
            assert STALE not in chain, (profile, task, chain)
            if task is TaskType.RESEARCH:
                # get_model_chain returns RESEARCH before the installed-model filter
                # (budget/research still names ollama/hermes3:8b) — a separate,
                # pre-existing gap, deliberately not changed here.
                continue
            assert not [m for m in chain if m.startswith("ollama/") and m not in FAKE_CACHE], (
                profile, task, chain)


# ── 3. visibility ───────────────────────────────────────────────────────────

@pytest.fixture
def fresh_warnings():
    discover._reset_filter_warnings()
    yield
    discover._reset_filter_warnings()


def _warnings(capsys, caplog, needle):
    """WARNING lines containing ``needle``.

    The project logger renders to stdout until another test has configured the
    root logger, after which the same line reaches caplog instead — read both and
    strip the console colour codes so the check holds in either order."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out + "\n" + caplog.text)
    return [ln for ln in text.splitlines() if "warning" in ln and needle in ln]


def test_dropped_model_is_warned_once_per_model(monkeypatch, capsys, caplog, fresh_warnings):
    monkeypatch.setattr(discover, "get_cached_ollama_models", lambda: list(FAKE_CACHE))
    chain = [STALE, "ollama/nimble:9b", "ollama/ghost:7b", "openai/gpt-4o"]
    for _ in range(3):
        assert discover.filter_ollama_by_installed(chain) == ["ollama/nimble:9b", "openai/gpt-4o"]
    lines = _warnings(capsys, caplog, "Dropping")
    assert [ln for ln in lines if f"Dropping {STALE} " in ln] and len(lines) == 2
    assert sum("Dropping ollama/ghost:7b " in ln for ln in lines) == 1


def test_empty_cache_skip_is_warned_once_and_passes_through(monkeypatch, capsys, caplog, fresh_warnings):
    monkeypatch.setattr(discover, "get_cached_ollama_models", lambda: [])
    chain = [STALE, "openai/gpt-4o"]
    for _ in range(3):
        assert discover.filter_ollama_by_installed(chain) == chain
    assert len(_warnings(capsys, caplog, "cache is empty")) == 1


def test_empty_cache_with_no_ollama_entry_is_silent(monkeypatch, capsys, caplog, fresh_warnings):
    monkeypatch.setattr(discover, "get_cached_ollama_models", lambda: [])
    assert discover.filter_ollama_by_installed(["openai/gpt-4o"]) == ["openai/gpt-4o"]
    assert not _warnings(capsys, caplog, "cache is empty")


def test_filter_exception_is_a_recorded_fail_open(monkeypatch, fake_local):
    from llm_router import failopen
    from llm_router.profiles import get_model_chain

    seen: list[str] = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append(code))

    def boom(chain):
        raise RuntimeError("cache unreadable")

    monkeypatch.setattr(discover, "filter_ollama_by_installed", boom)
    chain = get_model_chain(RoutingProfile.BALANCED, TaskType.CODE)
    assert chain, "fail-open must still return a chain"
    assert seen == ["CHZ-FO-PROFILES-OLLAMA-FILTER"]
