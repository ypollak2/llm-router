"""Gemini CLI local agent tool — llm_gemini."""

from __future__ import annotations

from llm_router.gemini_cli_agent import is_gemini_cli_available, run_gemini_cli


async def llm_gemini(
    prompt: str,
    model: str = "gemini-2.5-flash",
) -> str:
    """Route a task to the local Gemini CLI agent (Google).

    Uses the Gemini CLI to run tasks non-interactively. This uses the user's
    Google One AI Pro subscription (not Claude quota) — ideal as a fallback
    when Claude limits are tight, or for tasks that benefit from Google's
    Gemini models.

    Available models: gemini-2.5-flash, gemini-2.0-flash, gemini-3-flash-preview

    Args:
        prompt: The task or question to send to Gemini.
        model: Google model to use (default: gemini-2.5-flash).
    """
    if not is_gemini_cli_available():
        return (
            "Gemini CLI not found.\n"
            "Install from: https://google.com/gemini\n"
            "Expected at: ~/.local/bin/gemini or /usr/local/bin/gemini"
        )

    result = await run_gemini_cli(prompt, model=model)

    # Log to usage table so dashboard includes direct llm_gemini calls
    try:
        from llm_router import cost
        from llm_router.types import LLMResponse, RoutingProfile, TaskType

        estimated_tokens = max(1, len(result.content) // 4)
        await cost.log_usage(
            LLMResponse(
                content=result.content,
                model=f"gemini_cli/{result.model}",
                input_tokens=max(1, len(prompt) // 4),
                output_tokens=estimated_tokens,
                cost_usd=0.0,  # free via Google One AI Pro subscription
                latency_ms=result.duration_sec * 1000,
                provider="gemini_cli",
            ),
            task_type=TaskType.CODE,
            profile=RoutingProfile.BALANCED,
            success=result.success,
        )
    except Exception:
        pass  # never let logging break the tool

    status = "✅" if result.success else "❌"
    lines = [
        f"{status} **Gemini CLI** (`{result.model}`) — {result.duration_sec:.1f}s",
        "",
        result.content,
    ]
    return "\n".join(lines)


def register(mcp, should_register=None) -> None:
    """Register Gemini CLI tool with the FastMCP instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_gemini"):
        mcp.tool()(llm_gemini)
