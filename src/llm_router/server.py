"""FastMCP server — exposes LLM routing tools to Claude Code."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from llm_router.config import get_config
from llm_router.cost import get_usage_summary
from llm_router.health import get_tracker
from llm_router.router import route_and_call
from llm_router.types import RoutingProfile, TaskType

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

mcp = FastMCP(
    "llm-router",
    description="Multi-LLM router — query, research, generate, analyze, code across Gemini/GPT/Perplexity",
)


# ── Task Tools ───────────────────────────────────────────────────────────────


@mcp.tool()
async def llm_query(
    prompt: str,
    model: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send a general query to the best available LLM.

    Auto-routes based on the active profile, or use a specific model.

    Args:
        prompt: The question or prompt to send.
        model: Optional model override (e.g. "openai/gpt-4o", "gemini/gemini-2.0-flash").
        system_prompt: Optional system instructions.
        temperature: Sampling temperature (0.0-2.0).
        max_tokens: Maximum output tokens.
    """
    resp = await route_and_call(
        TaskType.QUERY, prompt,
        model_override=model, system_prompt=system_prompt,
        temperature=temperature, max_tokens=max_tokens,
    )
    return f"{resp.content}\n\n---\n{resp.summary()}"


@mcp.tool()
async def llm_research(
    prompt: str,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Search-augmented research query — routes to Perplexity for web-grounded answers.

    Best for: fact-checking, current events, finding sources, market research.

    Args:
        prompt: The research question.
        system_prompt: Optional system instructions.
        max_tokens: Maximum output tokens.
    """
    resp = await route_and_call(
        TaskType.RESEARCH, prompt,
        system_prompt=system_prompt, max_tokens=max_tokens,
        temperature=0.3,
    )
    result = resp.content
    if resp.citations:
        result += "\n\n**Sources:**\n" + "\n".join(f"- {c}" for c in resp.citations)
    result += f"\n\n---\n{resp.summary()}"
    return result


@mcp.tool()
async def llm_generate(
    prompt: str,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Generate creative or long-form content — routes to the best generation model.

    Best for: writing, summarization, brainstorming, content creation.

    Args:
        prompt: What to generate.
        system_prompt: Optional system instructions (tone, format, audience).
        temperature: Sampling temperature (higher = more creative).
        max_tokens: Maximum output tokens.
    """
    resp = await route_and_call(
        TaskType.GENERATE, prompt,
        system_prompt=system_prompt, temperature=temperature,
        max_tokens=max_tokens,
    )
    return f"{resp.content}\n\n---\n{resp.summary()}"


@mcp.tool()
async def llm_analyze(
    prompt: str,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Deep analysis task — routes to the strongest reasoning model.

    Best for: data analysis, code review, problem decomposition, debugging.

    Args:
        prompt: What to analyze.
        system_prompt: Optional system instructions.
        max_tokens: Maximum output tokens.
    """
    resp = await route_and_call(
        TaskType.ANALYZE, prompt,
        system_prompt=system_prompt, temperature=0.3,
        max_tokens=max_tokens,
    )
    return f"{resp.content}\n\n---\n{resp.summary()}"


@mcp.tool()
async def llm_code(
    prompt: str,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Coding task — routes to the best coding model.

    Best for: code generation, refactoring suggestions, algorithm design.

    Args:
        prompt: The coding task or question.
        system_prompt: Optional system instructions (language, framework, style).
        max_tokens: Maximum output tokens.
    """
    resp = await route_and_call(
        TaskType.CODE, prompt,
        system_prompt=system_prompt, temperature=0.2,
        max_tokens=max_tokens,
    )
    return f"{resp.content}\n\n---\n{resp.summary()}"


# ── Management Tools ─────────────────────────────────────────────────────────


@mcp.tool()
async def llm_set_profile(profile: str) -> str:
    """Switch the active routing profile.

    Args:
        profile: One of "budget", "balanced", or "premium".
    """
    try:
        new_profile = RoutingProfile(profile.lower())
    except ValueError:
        return f"Invalid profile: {profile}. Choose: budget, balanced, premium."

    config = get_config()
    # Mutate the singleton config's profile
    object.__setattr__(config, "llm_router_profile", new_profile)
    return f"Profile switched to: {new_profile.value}"


@mcp.tool()
async def llm_usage(period: str = "today") -> str:
    """View cost and token usage statistics.

    Args:
        period: Time period — "today", "week", "month", or "all".
    """
    return await get_usage_summary(period)


@mcp.tool()
async def llm_health() -> str:
    """Check the health status of all configured LLM providers."""
    config = get_config()
    tracker = get_tracker()
    report = tracker.status_report()

    lines = [
        f"## Provider Health (profile: {config.llm_router_profile.value})",
        f"Configured providers: {', '.join(sorted(config.available_providers)) or 'none'}",
        "",
    ]
    if not report:
        lines.append("No providers configured. Run `llm-router-onboard` to set up API keys.")
    else:
        for provider, status in report.items():
            lines.append(f"- **{provider}**: {status}")

    return "\n".join(lines)


# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("llm-router://status")
def router_status() -> str:
    """Current router status — profile, providers, health."""
    config = get_config()
    tracker = get_tracker()
    report = tracker.status_report()
    lines = [
        f"Profile: {config.llm_router_profile.value}",
        f"Providers: {', '.join(sorted(config.available_providers)) or 'none'}",
    ]
    for provider, status in report.items():
        lines.append(f"  {provider}: {status}")
    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
