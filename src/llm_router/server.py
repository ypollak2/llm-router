"""FastMCP server — exposes LLM routing tools to Claude Code."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from llm_router.config import get_config
from llm_router.cost import get_usage_summary
from llm_router.health import get_tracker
from llm_router.orchestrator import PIPELINE_TEMPLATES, auto_orchestrate, run_pipeline
from llm_router.router import route_and_call
from llm_router.types import PipelineStep, RoutingProfile, TaskType, Tier, PRO_FEATURES

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

mcp = FastMCP("llm-router")


def _check_tier(feature: str) -> str | None:
    """Check if the current tier allows a feature. Returns error message or None."""
    config = get_config()
    if config.llm_router_tier == Tier.FREE and feature in PRO_FEATURES:
        return (
            f"'{feature}' requires Pro tier ($12/mo). "
            f"Current tier: free. Upgrade at https://llm-router.dev/pricing"
        )
    return None


# ── Text LLM Tools ───────────────────────────────────────────────────────────


@mcp.tool()
async def llm_query(
    prompt: str,
    model: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send a general query to the best available LLM.

    Auto-routes based on the active profile. Supports 10+ text LLM providers.

    Args:
        prompt: The question or prompt to send.
        model: Optional model override (e.g. "openai/gpt-4o", "gemini/gemini-2.0-flash", "anthropic/claude-sonnet-4-6", "deepseek/deepseek-chat").
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


# ── Media Tools ──────────────────────────────────────────────────────────────


@mcp.tool()
async def llm_image(
    prompt: str,
    model: str | None = None,
    size: str = "1024x1024",
    quality: str = "standard",
) -> str:
    """Generate an image — auto-routes to DALL-E, Flux, or Stable Diffusion.

    Args:
        prompt: Description of the image to generate.
        model: Optional model override (e.g. "openai/dall-e-3", "fal/flux-pro", "stability/stable-diffusion-3").
        size: Image size (e.g. "1024x1024", "1792x1024").
        quality: Image quality — "standard" or "hd" (DALL-E only).
    """
    resp = await route_and_call(
        TaskType.IMAGE, prompt,
        model_override=model,
        media_params={"size": size, "quality": quality},
    )
    result = resp.content
    if resp.media_url:
        result += f"\n\nImage URL: {resp.media_url}"
    result += f"\n\n---\n{resp.summary()}"
    return result


@mcp.tool()
async def llm_video(
    prompt: str,
    model: str | None = None,
    duration: int = 5,
) -> str:
    """Generate a video — routes to Runway, Kling, Pika, or other video models.

    Args:
        prompt: Description of the video to generate.
        model: Optional model override (e.g. "runway/gen3a_turbo", "fal/kling-video").
        duration: Video duration in seconds (default: 5).
    """
    resp = await route_and_call(
        TaskType.VIDEO, prompt,
        model_override=model,
        media_params={"duration": duration},
    )
    result = resp.content
    if resp.media_url:
        result += f"\n\nVideo URL: {resp.media_url}"
    result += f"\n\n---\n{resp.summary()}"
    return result


@mcp.tool()
async def llm_audio(
    text: str,
    model: str | None = None,
    voice: str = "alloy",
) -> str:
    """Generate speech/audio — routes to ElevenLabs or OpenAI TTS.

    Args:
        text: Text to convert to speech.
        model: Optional model override (e.g. "openai/tts-1-hd", "elevenlabs/eleven_multilingual_v2").
        voice: Voice selection (OpenAI: alloy/echo/fable/onyx/nova/shimmer. ElevenLabs: voice ID).
    """
    resp = await route_and_call(
        TaskType.AUDIO, text,
        model_override=model,
        media_params={"voice": voice},
    )
    result = resp.content
    if resp.media_url:
        result += f"\n\nAudio: {resp.media_url}"
    result += f"\n\n---\n{resp.summary()}"
    return result


# ── Orchestration Tools (Pro feature, works free for now) ────────────────────


@mcp.tool()
async def llm_orchestrate(
    task: str,
    template: str | None = None,
) -> str:
    """Multi-step orchestration — automatically decomposes complex tasks across multiple LLMs.

    Chains research, analysis, generation, and coding steps together, routing each
    to the optimal model. Use templates for common patterns or let the AI decompose.

    Args:
        task: Description of the complex task to accomplish.
        template: Optional pipeline template: "research_report", "competitive_analysis", "content_pipeline", "code_review_fix". Omit for auto-decomposition.
    """
    if template and template in PIPELINE_TEMPLATES:
        steps = PIPELINE_TEMPLATES[template]
        result = await run_pipeline(steps, task)
    else:
        result = await auto_orchestrate(task)

    output = result.final_content
    output += f"\n\n---\n{result.summary()}"
    return output


@mcp.tool()
async def llm_pipeline_templates() -> str:
    """List available pipeline templates for multi-step orchestration."""
    lines = ["## Available Pipeline Templates\n"]
    descriptions = {
        "research_report": "Research → Analyze → Write Report (3 steps)",
        "competitive_analysis": "Research competitors → Find reviews → SWOT analysis → Report (4 steps)",
        "content_pipeline": "Research → Write → Review → Polish (4 steps)",
        "code_review_fix": "Review code → Fix issues → Write tests (3 steps)",
    }
    for name, desc in descriptions.items():
        step_types = [s.task_type.value for s in PIPELINE_TEMPLATES[name]]
        lines.append(f"- **{name}**: {desc}")
        lines.append(f"  Steps: {' → '.join(step_types)}")
    lines.append("")
    lines.append('Use: `llm_orchestrate(task="...", template="research_report")`')
    return "\n".join(lines)


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
        f"Configured: {len(config.available_providers)} providers — {', '.join(sorted(config.available_providers)) or 'none'}",
        f"Text: {', '.join(sorted(config.text_providers)) or 'none'}",
        f"Media: {', '.join(sorted(config.media_providers)) or 'none'}",
        "",
    ]
    if not report:
        lines.append("No providers configured. Run `llm-router-onboard` to set up API keys.")
    else:
        for provider, status in report.items():
            lines.append(f"- **{provider}**: {status}")

    return "\n".join(lines)


@mcp.tool()
async def llm_providers() -> str:
    """List all supported providers and which ones are configured."""
    config = get_config()
    available = config.available_providers

    text_providers = {
        "openai": "GPT-4o, GPT-4o-mini, o3, o4-mini",
        "gemini": "Gemini 2.5 Pro, 2.0 Flash",
        "perplexity": "Sonar, Sonar Pro (search-augmented)",
        "anthropic": "Claude Opus, Sonnet, Haiku",
        "mistral": "Mistral Large, Medium, Small",
        "deepseek": "DeepSeek V3, DeepSeek Reasoner",
        "groq": "Llama 3.3 70B, Mixtral (ultra-fast)",
        "together": "Llama 3, CodeLlama, open-source models",
        "xai": "Grok 3",
        "cohere": "Command R+",
    }
    media_providers = {
        "openai": "DALL-E 3, TTS, Whisper",
        "fal": "Flux Pro/Dev, Kling Video, minimax",
        "stability": "Stable Diffusion 3, SDXL",
        "elevenlabs": "Multilingual v2 (voice cloning)",
        "runway": "Gen-3 Alpha (video)",
        "replicate": "Various open-source models",
    }

    lines = ["## Supported Providers\n", "### Text & Code LLMs"]
    for provider, models in text_providers.items():
        status = "configured" if provider in available else "not configured"
        lines.append(f"- **{provider}** ({status}): {models}")

    lines.append("\n### Media Generation")
    for provider, models in media_providers.items():
        status = "configured" if provider in available else "not configured"
        lines.append(f"- **{provider}** ({status}): {models}")

    configured = len(available)
    total = len(set(text_providers) | set(media_providers))
    lines.append(f"\n{configured}/{total} providers configured")
    return "\n".join(lines)


# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("llm-router://status")
def router_status() -> str:
    """Current router status — profile, providers, tier, health."""
    config = get_config()
    tracker = get_tracker()
    report = tracker.status_report()
    lines = [
        f"Profile: {config.llm_router_profile.value}",
        f"Tier: {config.llm_router_tier.value}",
        f"Providers: {len(config.available_providers)} configured",
        f"Text: {', '.join(sorted(config.text_providers))}",
        f"Media: {', '.join(sorted(config.media_providers))}",
    ]
    if config.llm_router_monthly_budget > 0:
        lines.append(f"Budget: ${config.llm_router_monthly_budget:.2f}/mo")
    for provider, status in report.items():
        lines.append(f"  {provider}: {status}")
    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
