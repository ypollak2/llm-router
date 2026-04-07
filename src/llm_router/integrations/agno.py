"""Agno integration for llm-router — RouteredModel and RouteredTeam.

Install:
    pip install "claude-code-llm-router[agno]"

Usage::

    from llm_router.integrations.agno import RouteredModel, RouteredTeam
    from agno.agent import Agent

    agent = Agent(
        model=RouteredModel(task_type="code"),
        instructions="You are a coding assistant.",
    )
    agent.print_response("Write a Python quicksort.")

    # Multi-agent team with shared budget cap
    team = RouteredTeam(
        members=[coder_agent, researcher_agent],
        monthly_budget_usd=20.0,
        downshift_at=0.80,
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterator, List, Optional, Type, Union

log = logging.getLogger(__name__)

try:
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.team.team import Team
    from agno.agent import Agent
except ImportError as e:
    raise ImportError(
        "agno is required for RouteredModel. "
        "Install it with: pip install 'claude-code-llm-router[agno]'"
    ) from e

from llm_router.types import LLMResponse, RoutingProfile, TaskType
from llm_router.router import route_and_call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="routered-sync")

_TASK_TYPE_MAP: dict[str, TaskType] = {
    "query": TaskType.QUERY,
    "research": TaskType.RESEARCH,
    "generate": TaskType.GENERATE,
    "analyze": TaskType.ANALYZE,
    "code": TaskType.CODE,
    "image": TaskType.IMAGE,
    "video": TaskType.VIDEO,
    "audio": TaskType.AUDIO,
}

_PROFILE_MAP: dict[str, RoutingProfile] = {
    "budget": RoutingProfile.BUDGET,
    "balanced": RoutingProfile.BALANCED,
    "premium": RoutingProfile.PREMIUM,
}


def _resolve_task_type(value: str | TaskType) -> TaskType:
    if isinstance(value, TaskType):
        return value
    return _TASK_TYPE_MAP.get(value.lower(), TaskType.QUERY)


def _resolve_profile(value: str | RoutingProfile) -> RoutingProfile:
    if isinstance(value, RoutingProfile):
        return value
    return _PROFILE_MAP.get(value.lower(), RoutingProfile.BALANCED)


def _run_coroutine_sync(coro) -> Any:
    """Run an async coroutine from a sync context, even inside a running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in an async context — offload to a thread with its own loop
        future = _EXECUTOR.submit(asyncio.run, coro)
        return future.result()
    else:
        return asyncio.run(coro)


def _messages_to_prompt_and_system(
    messages: List[Message],
) -> tuple[str, Optional[str]]:
    """Extract (prompt, system_prompt) from an Agno message list."""
    system_prompt: Optional[str] = None
    turns: list[str] = []

    for msg in messages:
        content = msg.content or ""
        if isinstance(content, list):
            # Handle multimodal content — extract text parts
            content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )

        if msg.role == "system":
            system_prompt = content
        elif msg.role == "user":
            turns.append(f"User: {content}")
        elif msg.role == "assistant":
            turns.append(f"Assistant: {content}")

    if not turns:
        return "", system_prompt

    if len(turns) == 1:
        # Single user message — strip the "User: " prefix
        prompt = turns[0].removeprefix("User: ")
    else:
        # Multi-turn: send full history as the prompt
        prompt = "\n".join(turns)

    return prompt, system_prompt


def _llm_response_to_model_response(resp: LLMResponse) -> ModelResponse:
    """Convert llm-router LLMResponse to an Agno ModelResponse."""
    model_resp = ModelResponse()
    model_resp.role = "assistant"
    model_resp.content = resp.content
    model_resp.input_tokens = resp.input_tokens
    model_resp.output_tokens = resp.output_tokens
    model_resp.total_tokens = (resp.input_tokens or 0) + (resp.output_tokens or 0) or None
    model_resp.provider_data = {
        "model": resp.model,
        "provider": resp.provider,
        "cost_usd": resp.cost_usd,
        "latency_ms": resp.latency_ms,
    }
    return model_resp


# ---------------------------------------------------------------------------
# RouteredModel
# ---------------------------------------------------------------------------

class RouteredModel(Model):
    """Drop-in Agno Model that routes each call through llm-router.

    Every invocation is classified by complexity and routed to the cheapest
    capable provider — Ollama (free) → Codex (free) → paid APIs.

    Parameters
    ----------
    task_type:
        Hint for the classifier.  One of: query, research, generate, analyze,
        code, image, video, audio.  Default: ``"query"`` (auto-classified).
    profile:
        Routing profile.  One of: budget, balanced, premium.
        Default: ``"balanced"``.
    model_override:
        Pin a specific model (e.g. ``"openai/gpt-4o"``).  Bypasses routing.
    """

    def __init__(
        self,
        task_type: str | TaskType = "query",
        profile: str | RoutingProfile = "balanced",
        model_override: Optional[str] = None,
        **kwargs,
    ) -> None:
        task_type_str = task_type.value if isinstance(task_type, TaskType) else str(task_type)
        profile_str = profile.value if isinstance(profile, RoutingProfile) else str(profile)
        # Pass only base Model fields to super(); extra kwargs (e.g. instructions) are forwarded
        super().__init__(id="llm-router", provider="llm-router", name="RouteredModel", **kwargs)
        # Plain Python class, set instance attributes directly
        self.task_type = task_type_str
        self.profile = profile_str
        self.model_override = model_override

    # ------------------------------------------------------------------
    # Internal routing call
    # ------------------------------------------------------------------

    async def _aroute(self, messages: List[Message]) -> LLMResponse:
        prompt, system_prompt = _messages_to_prompt_and_system(messages)
        return await route_and_call(
            task_type=_resolve_task_type(self.task_type),
            prompt=prompt,
            system_prompt=system_prompt,
            profile=_resolve_profile(self.profile),
            model_override=self.model_override,
        )

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def invoke(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[Union[RunOutput, TeamRunOutput]] = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        assistant_message.metrics.start_timer()
        llm_resp = _run_coroutine_sync(self._aroute(messages))
        assistant_message.metrics.stop_timer()
        return _llm_response_to_model_response(llm_resp)

    async def ainvoke(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[Union[RunOutput, TeamRunOutput]] = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        assistant_message.metrics.start_timer()
        llm_resp = await self._aroute(messages)
        assistant_message.metrics.stop_timer()
        return _llm_response_to_model_response(llm_resp)

    def invoke_stream(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[Union[RunOutput, TeamRunOutput]] = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        # llm-router doesn't stream natively — return full response as one chunk
        assistant_message.metrics.start_timer()
        llm_resp = _run_coroutine_sync(self._aroute(messages))
        assistant_message.metrics.stop_timer()
        yield _llm_response_to_model_response(llm_resp)

    async def ainvoke_stream(
        self,
        messages: List[Message],
        assistant_message: Message,
        response_format: Optional[Union[Dict, Type]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
        run_response: Optional[Union[RunOutput, TeamRunOutput]] = None,
        compress_tool_results: bool = False,
    ):
        assistant_message.metrics.start_timer()
        llm_resp = await self._aroute(messages)
        assistant_message.metrics.stop_timer()
        yield _llm_response_to_model_response(llm_resp)

    def _parse_provider_response(self, response: Any, **kwargs) -> ModelResponse:
        # Not used in our direct-routing code path
        if isinstance(response, LLMResponse):
            return _llm_response_to_model_response(response)
        model_resp = ModelResponse()
        model_resp.content = str(response)
        return model_resp

    def _parse_provider_response_delta(self, response_delta: Any) -> ModelResponse:
        # Not used in our direct-routing code path
        model_resp = ModelResponse()
        model_resp.content = str(response_delta)
        return model_resp


# ---------------------------------------------------------------------------
# RouteredTeam
# ---------------------------------------------------------------------------

class RouteredTeam(Team):
    """Agno Team with shared budget enforcement across all RouteredModel members.

    When cumulative monthly spend reaches ``downshift_at * monthly_budget_usd``,
    all member ``RouteredModel`` instances are automatically switched to the
    ``budget`` routing profile to keep costs under control.

    Parameters
    ----------
    members:
        List of Agno Agent (or Team) instances.  Members using RouteredModel
        benefit from budget-aware profile downshifting.
    monthly_budget_usd:
        Total USD budget for this team across all calls this month.
        Set to ``0`` to disable enforcement (default).
    downshift_at:
        Fraction of ``monthly_budget_usd`` at which to downshift to the
        ``budget`` routing profile.  Default: ``0.80`` (80%).
    """

    monthly_budget_usd: float = 0.0
    downshift_at: float = 0.80

    def __init__(
        self,
        members: List[Union[Agent, "RouteredTeam"]],
        monthly_budget_usd: float = 0.0,
        downshift_at: float = 0.80,
        **kwargs,
    ) -> None:
        super().__init__(members=members, **kwargs)
        self.monthly_budget_usd = monthly_budget_usd
        self.downshift_at = downshift_at

    # ------------------------------------------------------------------
    # Budget enforcement
    # ------------------------------------------------------------------

    def _get_routered_models(self) -> list[RouteredModel]:
        models: list[RouteredModel] = []
        for member in self.members or []:
            if isinstance(member, Agent) and isinstance(getattr(member, "model", None), RouteredModel):
                models.append(member.model)  # type: ignore[arg-type]
        return models

    async def _apply_budget_pressure(self) -> bool:
        """Check spend and downshift RouteredModel members if needed.

        Returns True if downshift was applied.
        """
        if self.monthly_budget_usd <= 0:
            return False

        try:
            from llm_router.cost import get_quality_report
            report = await get_quality_report(days=30)
            spend = float(report.get("total_cost_usd", 0.0))
            threshold = self.monthly_budget_usd * self.downshift_at

            if spend >= threshold:
                models = self._get_routered_models()
                if models:
                    log.warning(
                        "RouteredTeam: monthly spend $%.4f >= threshold $%.4f — "
                        "downshifting %d model(s) to budget profile",
                        spend,
                        threshold,
                        len(models),
                    )
                    for m in models:
                        m.profile = "budget"
                    return True
        except Exception as e:
            log.debug("RouteredTeam budget check failed (non-fatal): %s", e)

        return False

    def run(self, *args, **kwargs):
        _run_coroutine_sync(self._apply_budget_pressure())
        return super().run(*args, **kwargs)

    async def arun(self, *args, **kwargs):
        await self._apply_budget_pressure()
        return await super().arun(*args, **kwargs)
