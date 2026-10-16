"""D-114/D-115: keep local Q&A models as a last resort before Claude.

env-doctor (2026-10-15) found a fresh-process llm_route(complexity_override="simple")
Q&A chain collapsed to anthropic/claude-haiku-4-5. With no Anthropic API key and
LLM_ROUTER_CLAUDE_SUBSCRIPTION=true, AuthenticationError ended the call with
"All models failed for query/budget", despite installed Ollama.

Pin the shared filter, normal hook stripping, and primary/emergency MCP dispatch.
`test_build_chain_qa_last_resort` and `test_sdk_qa_last_resort` cover the builder
and real SDK route. Daily caps are pinned in tests/test_mcp_qa_no_local.py;
`test_qa_prompt_is_served_by_local_as_last_resort_in_zero_claude` in
tests/test_zero_claude_scenarios.py covers zero-Claude hook dispatch.
"""

import pytest

from llm_router.contracts import CLAUDE_PROVIDERS
from llm_router.hooks import chain_builder, direct_executor
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
def test_normal_hook_still_strips_all_local(chain, expected):
    """Only normal hook mode uses keep_if_only_local=False, even without externals."""
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


@pytest.fixture
def ollama_only_chain(monkeypatch):
    monkeypatch.setattr(
        chain_builder, "_ollama_models",
        lambda *a, **k: ([ModelSpec("ollama", "ollama-model")], []),
    )
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "ollama-model")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.parametrize("task_type", sorted(QA_TASK_TYPES))
@pytest.mark.parametrize("complexity", ("simple", "moderate"))
def test_build_chain_qa_last_resort(monkeypatch, ollama_only_chain, task_type, complexity):
    """All Q&A types share the filter; research's separate web handoff stays empty."""
    if task_type == "research":
        assert chain_builder.build_chain(
            complexity, "green", task_type, qa_last_resort=True,
        ) == []
        # Supply a draft chain to exercise the policy for research too, without
        # changing the builder's deliberate web-research handoff.
        build = chain_builder._build_chain
        monkeypatch.setattr(
            chain_builder, "_build_chain", lambda c, z, tt: build(c, z, "query"),
        )
    chain = chain_builder.build_chain(
        complexity, "green", task_type, qa_last_resort=True,
    )
    assert ModelSpec("ollama", "ollama-model") in chain
    local = [i for i, spec in enumerate(chain) if spec.provider == "ollama"]
    claude = [i for i, spec in enumerate(chain) if spec.provider == "claude"]
    assert all(i < j for i in local for j in claude)
    default = chain_builder.build_chain(complexity, "green", task_type)
    assert not any(spec.provider == "ollama" for spec in default)


def test_build_chain_external_provider_still_strips_local(monkeypatch, ollama_only_chain):
    """D-14 still applies when a non-Claude provider is listed in the chain."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    chain = chain_builder.build_chain("simple", "green", "query", qa_last_resort=True)
    assert not any(spec.provider == "ollama" for spec in chain)


@pytest.mark.parametrize("external", (False, True), ids=("ollama-only", "gemini"))
def test_sdk_qa_last_resort(monkeypatch, ollama_only_chain, external):
    """Real SDK routing serves local only when no non-Claude provider remains."""
    calls = {"ollama": 0, "gemini": 0}

    def fake_ollama(prompt, model, timeout, history, system_prompt):
        calls["ollama"] += 1
        return "Local answer about X that would pass the quality gate.", {}

    def fake_gemini(prompt, model, timeout, history, system_prompt):
        calls["gemini"] += 1
        return "External answer about X that passes the quality gate.", {}

    if external:
        monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    monkeypatch.setitem(direct_executor._PROVIDER_CALLS, "ollama", fake_ollama)
    monkeypatch.setitem(direct_executor._PROVIDER_CALLS, "gemini", fake_gemini)
    monkeypatch.setattr(
        direct_executor, "available_ollama_models", lambda timeout=0.5: {"ollama-model"},
    )
    from llm_router import ollama_watchdog

    monkeypatch.setattr(ollama_watchdog, "gate", lambda *a, **k: None)
    monkeypatch.setattr(direct_executor, "_paid_budget_exhausted", lambda provider: False)
    monkeypatch.setattr(direct_executor, "_provider_reset_blocked", lambda provider: False)

    from llm_router.sdk import route

    result = route("what is X", task_type="query")
    assert calls == {"ollama": int(not external), "gemini": int(external)}
    assert result.provider == ("gemini" if external else "ollama")
