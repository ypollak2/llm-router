"""Tests for agentic model routing (LLM_ROUTER_AGENTIC_MODEL / routing.yaml agentic_model).

A designated agentic model is pinned at the absolute front of the routing chain
for agentic / tool-reasoning task types (analyze, generate, query, research),
while CODE is intentionally excluded so dedicated coders keep coding tasks.
"""

from __future__ import annotations

import pytest

from llm_router.config import RouterConfig
from llm_router.repo_config import RepoConfig, _dict_to_config, _merge
from llm_router.router import AGENTIC_TASK_TYPES, _build_and_filter_chain
from llm_router.types import Complexity, RoutingProfile, TaskType

AGENTIC = "ollama/hermes3:8b"


# ── config / repo_config plumbing ───────────────────────────────────────────

def test_config_agentic_model_from_env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_AGENTIC_MODEL", AGENTIC)
    assert RouterConfig().llm_router_agentic_model == AGENTIC


def test_repo_config_parses_agentic_model():
    cfg = _dict_to_config({"agentic_model": AGENTIC}, "test")
    assert cfg.agentic_model == AGENTIC


def test_repo_config_merge_override_wins():
    user = _dict_to_config({"agentic_model": AGENTIC}, "user")
    repo = _dict_to_config({"agentic_model": "ollama/qwen3:32b"}, "repo")
    assert _merge(user, repo).agentic_model == "ollama/qwen3:32b"


def test_agentic_task_types_excludes_code():
    assert TaskType.CODE not in AGENTIC_TASK_TYPES
    assert TaskType.ANALYZE in AGENTIC_TASK_TYPES
    assert TaskType.QUERY in AGENTIC_TASK_TYPES


# ── router chain ordering ───────────────────────────────────────────────────

def _isolate(monkeypatch):
    """Make chain building deterministic and independent of the dev machine.

    Also patches out Ollama model discovery so the AGENTIC model (hermes3:8b)
    doesn't appear in chains as a naturally-discovered model. Without this,
    E5 flakes: hermes3:8b is installed on the dev machine, so it shows up in
    CODE chains independently of the agentic pin logic.
    """
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.0)
    monkeypatch.setattr("llm_router.router.get_repo_config", lambda *a, **k: RepoConfig())
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    # Pin the Ollama model list to a deterministic set that excludes the AGENTIC
    # model (hermes3:8b) so agentic-pin assertions aren't machine-dependent.
    # RouterConfig.all_ollama_models() is the source of truth in _build_and_filter_chain.
    monkeypatch.setattr(
        "llm_router.config.RouterConfig.all_ollama_models",
        lambda self: ["ollama/qwen3.5:latest"],
    )


@pytest.mark.asyncio
async def test_agentic_model_pinned_front_for_analyze(monkeypatch):
    _isolate(monkeypatch)
    cfg = RouterConfig()
    cfg.llm_router_agentic_model = AGENTIC
    chain = await _build_and_filter_chain(
        TaskType.ANALYZE, RoutingProfile.BALANCED, None, None, Complexity.MODERATE, cfg,
    )
    assert chain and chain[0] == AGENTIC


@pytest.mark.asyncio
async def test_agentic_model_not_pinned_for_code(monkeypatch):
    _isolate(monkeypatch)
    cfg = RouterConfig()
    cfg.llm_router_agentic_model = AGENTIC
    chain = await _build_and_filter_chain(
        TaskType.CODE, RoutingProfile.BALANCED, None, None, Complexity.MODERATE, cfg,
    )
    # CODE is excluded — the agentic pin must never force itself onto coding tasks.
    assert AGENTIC not in chain


@pytest.mark.asyncio
async def test_yaml_pin_used_when_env_unset(monkeypatch):
    _isolate(monkeypatch)
    # No env var; supply the agentic model via the (merged) repo config instead.
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(agentic_model=AGENTIC),
    )
    cfg = RouterConfig()
    cfg.llm_router_agentic_model = ""  # env path empty
    chain = await _build_and_filter_chain(
        TaskType.GENERATE, RoutingProfile.BALANCED, None, None, Complexity.MODERATE, cfg,
    )
    assert chain and chain[0] == AGENTIC


@pytest.mark.asyncio
async def test_env_overrides_yaml_pin(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(agentic_model="ollama/qwen3:32b"),
    )
    cfg = RouterConfig()
    cfg.llm_router_agentic_model = AGENTIC  # env wins
    chain = await _build_and_filter_chain(
        TaskType.QUERY, RoutingProfile.BALANCED, None, None, Complexity.MODERATE, cfg,
    )
    assert chain and chain[0] == AGENTIC
