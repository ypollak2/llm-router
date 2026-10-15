"""D-114: keep local Q&A models as an MCP last resort before Claude.

env-doctor (2026-10-15) found a fresh-process llm_route(complexity_override="simple")
Q&A chain collapsed to anthropic/claude-haiku-4-5. With no Anthropic API key and
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true, AuthenticationError ended the call with
"All models failed for query/budget", despite installed Ollama.

Pin the shared filter's provider classification, ordering and hook/SDK behaviour,
plus primary and emergency MCP dispatch through the existing stubbed harness.
"""

import pytest

from llm_router.contracts import CLAUDE_PROVIDERS
from llm_router.hooks.direct_executor import ModelSpec
from llm_router.qa_policy import QA_TASK_TYPES, strip_local_for_qa
from llm_router.types import TaskType
from tests.test_mcp_qa_no_local import _run


@pytest.fixture(autouse=True)
def _models_are_user_configured(monkeypatch):
    monkeypatch.setenv(
        "OLLAMA_BUDGET_MODELS",
        "qwen3.5:latest,hermes3:8b,qwen3-coder:30b,qwen2.5:7b",
    )


@pytest.mark.parametrize("task_type", sorted(QA_TASK_TYPES))
def test_codex_still_strips_local_for_qa(task_type):
    chain = [
        "ollama/qwen3.5:latest",
        "codex/gpt-5.5",
        "anthropic/claude-haiku-4-5",
    ]
    assert strip_local_for_qa(chain, task_type) == chain[1:]


@pytest.mark.parametrize(
    "provider_model",
    [
        "openai/gpt-4o",
        "gemini/gemini-2.5-flash",
        "openrouter/x",
        "gemini_cli/gemini-2.5-pro",
    ],
)
def test_non_claude_provider_still_strips_local(provider_model):
    kept = [provider_model, "anthropic/claude-haiku-4-5"]
    assert strip_local_for_qa(["ollama/qwen3.5:latest", *kept], "query") == kept


@pytest.mark.parametrize("task_type", sorted(QA_TASK_TYPES))
def test_claude_only_keeps_local_before_claude_for_qa(task_type):
    chain = [
        "anthropic/claude-haiku-4-5",
        "ollama/qwen3.5:latest",
        "anthropic/claude-sonnet-4-6",
        "ollama/hermes3:8b",
    ]
    assert strip_local_for_qa(chain, task_type) == [
        "ollama/qwen3.5:latest",
        "ollama/hermes3:8b",
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4-6",
    ]


def test_claude_provider_contract_is_nonempty_and_includes_anthropic():
    assert CLAUDE_PROVIDERS
    assert "anthropic" in CLAUDE_PROVIDERS


@pytest.mark.parametrize("provider", sorted(CLAUDE_PROVIDERS))
def test_every_claude_provider_keeps_local(provider):
    assert strip_local_for_qa([f"{provider}/m", "ollama/q"], "query") == [
        "ollama/q",
        f"{provider}/m",
    ]


def test_model_specs_keep_local_before_claude():
    chain = [ModelSpec("claude", "haiku"), ModelSpec("ollama", "q")]
    assert strip_local_for_qa(chain, "query") == [
        ModelSpec("ollama", "q"),
        ModelSpec("claude", "haiku"),
    ]


@pytest.mark.parametrize("task_type", sorted(QA_TASK_TYPES))
def test_only_local_is_kept_for_qa(task_type):
    chain = ["ollama/a", "lm_studio/b"]
    assert strip_local_for_qa(chain, task_type) == chain


@pytest.mark.parametrize("task_type", ["code", TaskType.CODE])
@pytest.mark.parametrize(
    "chain",
    [
        ["anthropic/claude-haiku-4-5", "ollama/q"],
        ["ollama/q", "codex/gpt-5.5"],
    ],
)
def test_non_qa_is_unchanged(task_type, chain):
    assert strip_local_for_qa(chain, task_type) == chain


@pytest.mark.parametrize(
    ("chain", "expected"),
    [
        (
            ["anthropic/claude-haiku-4-5", "ollama/q"],
            ["anthropic/claude-haiku-4-5"],
        ),
        (
            [ModelSpec("ollama", "q"), ModelSpec("claude", "c")],
            [ModelSpec("claude", "c")],
        ),
        (["ollama/q"], []),
    ],
)
def test_hook_and_sdk_still_strip_all_local(chain, expected):
    assert strip_local_for_qa(chain, "query", keep_if_only_local=False) == expected


def test_claude_only_without_local_is_unchanged():
    chain = ["anthropic/claude-haiku-4-5"]
    assert strip_local_for_qa(chain, "query") == chain


@pytest.mark.asyncio
async def test_mcp_evidence_scenario_serves_local_before_claude():
    called = await _run(
        TaskType.QUERY,
        ["anthropic/claude-haiku-4-5", "ollama/qwen3.5:latest"],
    )
    assert called == ["ollama/qwen3.5:latest"]


@pytest.mark.asyncio
async def test_mcp_local_failure_still_tries_claude():
    called = await _run(
        TaskType.QUERY,
        ["anthropic/claude-haiku-4-5", "ollama/qwen3.5:latest"],
        fail={"ollama/qwen3.5:latest"},
    )
    assert called[:2] == ["ollama/qwen3.5:latest", "anthropic/claude-haiku-4-5"]


@pytest.mark.asyncio
async def test_mcp_non_claude_provider_still_strips_local():
    # openai, not codex: codex dispatches through its CLI, outside the harness's
    # providers.call_llm stub. The codex case is pinned by the pure-filter tests above.
    called = await _run(
        TaskType.QUERY,
        ["ollama/qwen3.5:latest", "openai/gpt-4o", "anthropic/claude-haiku-4-5"],
    )
    assert called == ["openai/gpt-4o"]


@pytest.mark.asyncio
async def test_mcp_emergency_budget_keeps_local_before_claude():
    called = await _run(
        TaskType.QUERY,
        ["openai/gpt-4o"],
        emergency=["anthropic/claude-haiku-4-5", "ollama/qwen3.5:latest"],
        fail={"openai/gpt-4o"},
    )
    assert called == ["openai/gpt-4o", "ollama/qwen3.5:latest"]
