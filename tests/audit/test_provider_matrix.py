from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from llm_router.profiles import provider_from_model
from llm_router.router import _build_and_filter_chain
from llm_router.types import Complexity, RoutingProfile, TaskType


class _NoPins:
    block_providers: list[str] = []
    block_models: list[str] = []
    allow_models: list[str] = []
    agentic_model = ""

    def model_override(self, task_type: str) -> None:
        return None

    def provider_override(self, task_type: str) -> None:
        return None


class _MatrixConfig:
    llm_router_gemini_subscription = False
    llm_router_claw_code = False
    llm_router_routing_policy = "balanced"
    llm_router_agentic_model = ""

    def __init__(
        self,
        *,
        available_providers: set[str],
        ollama_models: list[str] | None = None,
        claude_subscription: bool = False,
    ) -> None:
        self.available_providers = available_providers
        self._ollama_models = ollama_models or []
        self.llm_router_claude_subscription = claude_subscription

    def all_ollama_models(self) -> list[str]:
        return list(self._ollama_models)

    def all_openai_compat_models(self) -> list[str]:
        return []


@dataclass(frozen=True)
class _EnvCase:
    name: str
    config: _MatrixConfig
    codex: bool
    gemini_cli: bool
    expected_extra_providers: frozenset[str] = frozenset()


@pytest.fixture(autouse=True)
def isolate_chain_build(monkeypatch):
    monkeypatch.setattr("llm_router.dynamic_routing.get_dynamic_model_chain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("llm_router.router.get_repo_config", lambda: _NoPins())
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.2)
    monkeypatch.setattr("llm_router.cost.get_model_failure_rates", AsyncMock(return_value={}))
    monkeypatch.setattr("llm_router.cost.get_model_latency_stats", AsyncMock(return_value={}))
    monkeypatch.setattr("llm_router.cost.get_model_acceptance_scores", AsyncMock(return_value={}))
    monkeypatch.setattr("llm_router.policy.load_org_policy", lambda: None)


async def _chain(task_type: TaskType, config: _MatrixConfig) -> list[str]:
    return await _build_and_filter_chain(
        task_type,
        RoutingProfile.BALANCED,
        None,
        Complexity.MODERATE,
        Complexity.MODERATE,
        config,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        _EnvCase(
            "ollama_only",
            _MatrixConfig(
                available_providers={"ollama"},
                ollama_models=["ollama/qwen:7b", "ollama/hermes3:8b", "ollama/mistral:latest"],
            ),
            codex=False,
            gemini_cli=False,
        ),
        _EnvCase(
            "codex_only",
            _MatrixConfig(available_providers=set()),
            codex=True,
            gemini_cli=False,
            expected_extra_providers=frozenset({"codex"}),
        ),
        _EnvCase(
            "claude_subscription_only",
            _MatrixConfig(available_providers=set(), claude_subscription=True),
            codex=False,
            gemini_cli=False,
            expected_extra_providers=frozenset({"anthropic"}),
        ),
        _EnvCase(
            "everything_available",
            _MatrixConfig(
                available_providers={"ollama", "openai", "gemini", "anthropic"},
                ollama_models=["ollama/qwen:7b", "ollama/hermes3:8b", "ollama/mistral:latest"],
                claude_subscription=True,
            ),
            codex=True,
            gemini_cli=True,
            expected_extra_providers=frozenset({"codex", "gemini_cli"}),
        ),
        _EnvCase(
            "paid_openai_only",
            _MatrixConfig(available_providers={"openai"}),
            codex=False,
            gemini_cli=False,
        ),
        _EnvCase(
            "unusual_ollama_names",
            _MatrixConfig(
                available_providers={"ollama"},
                ollama_models=["ollama/my-custom-model-v3", "ollama/xyz-7b"],
            ),
            codex=False,
            gemini_cli=False,
        ),
    ],
    ids=lambda case: case.name,
)
async def test_provider_availability_matrix_never_returns_unavailable_providers(
    case: _EnvCase,
    monkeypatch,
):
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: case.codex)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: case.gemini_cli)
    allowed = set(case.config.available_providers) | set(case.expected_extra_providers)

    for task_type in (TaskType.QUERY, TaskType.CODE, TaskType.ANALYZE):
        chain = await _chain(task_type, case.config)
        assert chain, f"{case.name}/{task_type.value} returned empty chain"
        unavailable = [model for model in chain if provider_from_model(model) not in allowed]
        assert not unavailable, f"{case.name}/{task_type.value}: {unavailable}"

    if case.name == "unusual_ollama_names":
        query_chain = await _chain(TaskType.QUERY, case.config)
        assert "ollama/my-custom-model-v3" in query_chain
        assert "ollama/xyz-7b" in query_chain


@pytest.mark.asyncio
async def test_no_pin_default_ordering_varies_by_task_type(monkeypatch):
    """Regression test for the no-pin collapse: without a per-task pin,
    QUERY/CODE/ANALYZE/GENERATE used to ALWAYS produce the identical chain
    (same lead model for every task type). _task_aware_default_order now
    reorders the configured Ollama candidates per task type, so most users —
    who never write a pin — get real variety by default, not just users who
    take the extra step of configuring one.

    ``best_agentic_model`` is FORCED to a member of the configured set rather
    than left to read this machine's registry. Unpatched, it is cache-only over
    the developer's verified models, so the dynamic agentic pin fired locally
    (hermes3 verified) and not in CI (nothing verified) — the test passed and
    failed by machine, and the collapse it guards against was live for every
    user who had a verified agentic model. Forcing the pick makes the pinned
    path the one under test everywhere.
    """
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: True)
    monkeypatch.setattr(
        "llm_router.agentic_registry.best_agentic_model", lambda *a, **k: "ollama/hermes3:8b"
    )
    config = _MatrixConfig(
        available_providers={"ollama", "openai", "gemini"},
        ollama_models=["ollama/qwen:7b", "ollama/hermes3:8b", "ollama/mistral:latest"],
    )

    chains = {
        task_type.value: await _chain(task_type, config)
        for task_type in (TaskType.QUERY, TaskType.CODE, TaskType.ANALYZE, TaskType.GENERATE)
    }

    # Not a total collapse to one identical chain anymore. Two task types
    # sharing a rotation offset by coincidence is acceptable — the bug this
    # guards against is EVERY task type always landing on the same one.
    assert len({tuple(chain) for chain in chains.values()}) > 1, chains
    # The lead (index 0) model must not be identical across every task type.
    assert len({chain[0] for chain in chains.values()}) > 1, chains
