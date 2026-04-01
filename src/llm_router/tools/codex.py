"""Codex local agent tool — llm_codex."""

from __future__ import annotations

from llm_router.codex_agent import is_codex_available, run_codex


async def llm_codex(
    prompt: str,
    model: str = "gpt-5.4",
) -> str:
    """Route a task to the local Codex desktop agent (OpenAI).

    Uses the Codex CLI to run tasks non-interactively. This uses the user's
    OpenAI subscription (not Claude quota) — ideal as a fallback when Claude
    limits are tight, or for tasks that benefit from OpenAI's models.

    Available models: gpt-5.4, o3, o4-mini, gpt-4o, gpt-4o-mini

    Args:
        prompt: The task or question to send to Codex.
        model: OpenAI model to use (default: gpt-5.4).
    """
    if not is_codex_available():
        return (
            "Codex CLI not found.\n"
            "Install from: https://openai.com/codex\n"
            "Expected at: /Applications/Codex.app"
        )

    result = await run_codex(prompt, model=model)

    status = "\u2705" if result.success else "\u274c"
    lines = [
        f"{status} **Codex** (`{result.model}`) — {result.duration_sec:.1f}s",
        "",
        result.content,
    ]
    return "\n".join(lines)


def register(mcp) -> None:
    """Register Codex tool with the FastMCP instance."""
    mcp.tool()(llm_codex)
