"""Tests for Agno integration — RouteredModel and RouteredTeam."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.types import LLMResponse, RoutingProfile, TaskType


@pytest.fixture
def mock_llm_response():
    return LLMResponse(
        content="Routed response",
        model="gemini/gemini-2.5-flash",
        input_tokens=50,
        output_tokens=25,
        cost_usd=0.000001,
        latency_ms=150.0,
        provider="gemini",
    )


@pytest.fixture
def mock_route_and_call(mock_llm_response):
    with patch(
        "llm_router.integrations.agno.route_and_call",
        new_callable=AsyncMock,
        return_value=mock_llm_response,
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# RouteredModel — basic construction
# ---------------------------------------------------------------------------

def test_routered_model_defaults():
    from llm_router.integrations.agno import RouteredModel
    m = RouteredModel()
    assert m.id == "llm-router"
    assert m.task_type == "query"
    assert m.profile == "balanced"
    assert m.model_override is None


def test_routered_model_string_params():
    from llm_router.integrations.agno import RouteredModel
    m = RouteredModel(task_type="code", profile="budget")
    assert m.task_type == "code"
    assert m.profile == "budget"


def test_routered_model_enum_params():
    from llm_router.integrations.agno import RouteredModel
    m = RouteredModel(task_type=TaskType.RESEARCH, profile=RoutingProfile.PREMIUM)
    assert m.task_type == "research"
    assert m.profile == "premium"


def test_routered_model_with_override():
    from llm_router.integrations.agno import RouteredModel
    m = RouteredModel(model_override="openai/gpt-4o")
    assert m.model_override == "openai/gpt-4o"


# ---------------------------------------------------------------------------
# RouteredModel — invoke (sync)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_calls_route_and_call(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel(task_type="query", profile="budget")
    messages = [Message(role="user", content="Hello")]
    assistant_msg = Message(role="assistant", content="")

    response = m.invoke(messages, assistant_msg)

    assert response.content == "Routed response"
    assert response.role == "assistant"
    mock_route_and_call.assert_called_once()
    call_kwargs = mock_route_and_call.call_args.kwargs
    assert call_kwargs["task_type"] == TaskType.QUERY
    assert call_kwargs["profile"] == RoutingProfile.BUDGET
    assert call_kwargs["prompt"] == "Hello"


@pytest.mark.asyncio
async def test_ainvoke_calls_route_and_call(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel(task_type="code", profile="premium")
    messages = [Message(role="user", content="Write a sort function")]
    assistant_msg = Message(role="assistant", content="")

    response = await m.ainvoke(messages, assistant_msg)

    assert response.content == "Routed response"
    call_kwargs = mock_route_and_call.call_args.kwargs
    assert call_kwargs["task_type"] == TaskType.CODE
    assert call_kwargs["profile"] == RoutingProfile.PREMIUM


@pytest.mark.asyncio
async def test_invoke_with_model_override(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel(model_override="openai/gpt-4o")
    messages = [Message(role="user", content="test")]
    assistant_msg = Message(role="assistant", content="")

    m.invoke(messages, assistant_msg)

    call_kwargs = mock_route_and_call.call_args.kwargs
    assert call_kwargs["model_override"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_system_prompt_extracted_from_messages(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel()
    messages = [
        Message(role="system", content="You are a coding assistant"),
        Message(role="user", content="What is recursion?"),
    ]
    assistant_msg = Message(role="assistant", content="")

    m.invoke(messages, assistant_msg)

    call_kwargs = mock_route_and_call.call_args.kwargs
    assert call_kwargs["system_prompt"] == "You are a coding assistant"
    assert call_kwargs["prompt"] == "What is recursion?"


@pytest.mark.asyncio
async def test_multi_turn_conversation_concatenated(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel()
    messages = [
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Hi there"),
        Message(role="user", content="How are you?"),
    ]
    assistant_msg = Message(role="assistant", content="")

    m.invoke(messages, assistant_msg)

    call_kwargs = mock_route_and_call.call_args.kwargs
    prompt = call_kwargs["prompt"]
    # Multi-turn should include full history
    assert "Hello" in prompt
    assert "Hi there" in prompt
    assert "How are you?" in prompt


# ---------------------------------------------------------------------------
# RouteredModel — stream
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_stream_yields_response(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel()
    messages = [Message(role="user", content="Stream this")]
    assistant_msg = Message(role="assistant", content="")

    chunks = list(m.invoke_stream(messages, assistant_msg))
    assert len(chunks) == 1
    assert chunks[0].content == "Routed response"


@pytest.mark.asyncio
async def test_ainvoke_stream_yields_response(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel()
    messages = [Message(role="user", content="Stream this async")]
    assistant_msg = Message(role="assistant", content="")

    chunks = []
    async for chunk in m.ainvoke_stream(messages, assistant_msg):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].content == "Routed response"


# ---------------------------------------------------------------------------
# RouteredModel — token metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_response_includes_token_counts(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel()
    messages = [Message(role="user", content="test")]
    assistant_msg = Message(role="assistant", content="")

    response = await m.ainvoke(messages, assistant_msg)

    assert response.input_tokens == 50
    assert response.output_tokens == 25
    assert response.total_tokens == 75


@pytest.mark.asyncio
async def test_response_provider_data_contains_model(mock_route_and_call):
    from agno.models.message import Message
    from llm_router.integrations.agno import RouteredModel

    m = RouteredModel()
    messages = [Message(role="user", content="test")]
    assistant_msg = Message(role="assistant", content="")

    response = await m.ainvoke(messages, assistant_msg)

    assert response.provider_data is not None
    assert response.provider_data["model"] == "gemini/gemini-2.5-flash"
    assert response.provider_data["provider"] == "gemini"


# ---------------------------------------------------------------------------
# RouteredTeam
# ---------------------------------------------------------------------------

def test_routered_team_defaults():
    from agno.agent import Agent
    from llm_router.integrations.agno import RouteredModel, RouteredTeam

    agent = Agent(model=RouteredModel(task_type="query"), instructions="test")
    team = RouteredTeam(members=[agent])
    assert team.monthly_budget_usd == 0.0
    assert team.downshift_at == 0.80


def test_routered_team_budget_params():
    from agno.agent import Agent
    from llm_router.integrations.agno import RouteredModel, RouteredTeam

    agent = Agent(model=RouteredModel(), instructions="test")
    team = RouteredTeam(members=[agent], monthly_budget_usd=50.0, downshift_at=0.75)
    assert team.monthly_budget_usd == 50.0
    assert team.downshift_at == 0.75


def test_get_routered_models_finds_members():
    from agno.agent import Agent
    from llm_router.integrations.agno import RouteredModel, RouteredTeam

    m1 = RouteredModel(task_type="code")
    m2 = RouteredModel(task_type="research")
    a1 = Agent(model=m1, instructions="code")
    a2 = Agent(model=m2, instructions="research")
    team = RouteredTeam(members=[a1, a2])

    models = team._get_routered_models()
    assert len(models) == 2
    task_types = {m.task_type for m in models}
    assert task_types == {"code", "research"}


@pytest.mark.asyncio
async def test_budget_pressure_downshifts_models():
    from agno.agent import Agent
    from llm_router.integrations.agno import RouteredModel, RouteredTeam

    model = RouteredModel(task_type="code", profile="premium")
    agent = Agent(model=model, instructions="test")
    team = RouteredTeam(members=[agent], monthly_budget_usd=10.0, downshift_at=0.80)

    # Simulate spend at 85% of budget ($8.50 of $10.00)
    with patch(
        "llm_router.integrations.agno.RouteredTeam._apply_budget_pressure",
        wraps=team._apply_budget_pressure,
    ):
        with patch(
            "llm_router.cost.get_quality_report",
            new_callable=AsyncMock,
            return_value={"total_cost_usd": 8.5},
        ):
            downshifted = await team._apply_budget_pressure()

    assert downshifted is True
    assert model.profile == "budget"


@pytest.mark.asyncio
async def test_budget_pressure_no_downshift_under_threshold():
    from agno.agent import Agent
    from llm_router.integrations.agno import RouteredModel, RouteredTeam

    model = RouteredModel(task_type="code", profile="premium")
    agent = Agent(model=model, instructions="test")
    team = RouteredTeam(members=[agent], monthly_budget_usd=10.0, downshift_at=0.80)

    # Spend at 50% — should NOT downshift
    with patch(
        "llm_router.cost.get_quality_report",
        new_callable=AsyncMock,
        return_value={"total_cost_usd": 5.0},
    ):
        downshifted = await team._apply_budget_pressure()

    assert downshifted is False
    assert model.profile == "premium"  # unchanged


@pytest.mark.asyncio
async def test_budget_pressure_disabled_when_zero_budget():
    from agno.agent import Agent
    from llm_router.integrations.agno import RouteredModel, RouteredTeam

    model = RouteredModel(task_type="code", profile="premium")
    agent = Agent(model=model, instructions="test")
    team = RouteredTeam(members=[agent], monthly_budget_usd=0.0)  # disabled

    downshifted = await team._apply_budget_pressure()

    assert downshifted is False
    assert model.profile == "premium"  # unchanged
