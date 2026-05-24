"""Format direct model responses for hook output.

When the hook executes a model directly (bypassing Claude), this module
formats the response for display via {"decision": "block", "message": "..."}.
"""

from __future__ import annotations

from llm_router.hooks.direct_executor import DirectResult


def format_direct_response(result: DirectResult, task_type: str, complexity: str) -> str:
    """Format a DirectResult for user display.

    Shows the model that answered, the response, and a cost indicator.
    """
    model_label = f"{result.model.provider}/{result.model.model}"
    tier = _tier_label(result.model.provider)
    latency = f"{result.latency_ms}ms" if result.latency_ms < 1000 else f"{result.latency_ms / 1000:.1f}s"

    header = f"[{model_label}] {tier} | {task_type}/{complexity} | {latency}"
    separator = "─" * min(len(header), 60)

    return (
        f"{header}\n"
        f"{separator}\n"
        f"{result.text}\n"
        f"{separator}\n"
        f"0 subscription tokens used"
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
