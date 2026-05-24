"""Format direct model responses for hook output.

When the hook executes a model directly (bypassing Claude), this module
formats the response for display via {"decision": "block", "reason": "..."}.
"""

from __future__ import annotations

from llm_router.hooks.direct_executor import DirectResult


def format_direct_response(result: DirectResult, task_type: str, complexity: str) -> str:
    """Format a DirectResult for user display.

    Shows the response directly, with a compact metadata footer.
    """
    model_label = f"{result.model.provider}/{result.model.model}"
    tier = _tier_label(result.model.provider)
    latency = f"{result.latency_ms}ms" if result.latency_ms < 1000 else f"{result.latency_ms / 1000:.1f}s"

    # Use a minimal, non-blocking-looking format
    tokens = f"{result.input_tokens + result.output_tokens} tokens" if result.input_tokens + result.output_tokens > 0 else "0 tokens used"
    metadata = f"[{model_label}] {tier} | {task_type}/{complexity} | {latency} | {tokens}"
    
    return (
        f"{result.text}\n\n"
        f"\033[90m{metadata}\033[0m"
    )


def _tier_label(provider: str) -> str:
    """Return a human-readable tier label."""
    tiers = {
        "ollama": "[FREE/LOCAL]",
        "codex": "[FREE/SUB]",
        "gemini": "[API]",
        "openai": "[API]",
    }
    return tiers.get(provider, "[UNKNOWN]")
