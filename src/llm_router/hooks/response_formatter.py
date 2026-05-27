"""Format direct model responses for hook output.

Supports three render modes:
- "block": Returns {"decision": "block", "reason": text} — zero cost, warning-styled in TUI
- "echo":  Returns {"decision": "approve"} + contextForAgent — costs 1 turn, renders as normal text
- "echo-legacy": Returns {"decision": "approve"} + additionalContext (low priority, often ignored)
"""

from __future__ import annotations

import os

from llm_router.hooks.direct_executor import DirectResult

# Render mode: "block" (free, warning-styled) or "echo" (1 turn, normal text)
RENDER_MODE = os.environ.get("LLM_ROUTER_RENDER_MODE", "echo").lower()


def format_direct_response(result: DirectResult, task_type: str, complexity: str) -> str:
    """Format a DirectResult for user display (used in block mode).

    Shows the response directly, with a compact metadata footer.
    """
    model_label = f"{result.model.provider}/{result.model.model}"
    tier = _tier_label(result.model.provider)
    latency = f"{result.latency_ms}ms" if result.latency_ms < 1000 else f"{result.latency_ms / 1000:.1f}s"

    tokens = f"{result.input_tokens + result.output_tokens} tokens" if result.input_tokens + result.output_tokens > 0 else "0 tokens used"
    metadata = f"[{model_label}] {tier} | {task_type}/{complexity} | {latency} | {tokens}"

    return (
        f"{result.text}\n\n"
        f"{metadata}"
    )


def format_echo_context(result: DirectResult, task_type: str, complexity: str) -> str:
    """Format a DirectResult as a contextForAgent directive for Claude (echo mode).

    Uses contextForAgent so Claude treats it as an authoritative per-turn
    instruction. The framing is cooperative (explains the goal, permits
    corrections) rather than adversarial — earlier versions used
    "OVERRIDE ALL OTHER INSTRUCTIONS / Do NOT acknowledge" wording that
    matched prompt-injection patterns and Claude's safety training resisted
    it. See docs/decisions.md 2026-05-27.
    """
    model_label = f"{result.model.provider}/{result.model.model}"
    tier = _tier_label(result.model.provider)
    latency = f"{result.latency_ms}ms" if result.latency_ms < 1000 else f"{result.latency_ms / 1000:.1f}s"
    tokens = f"{result.input_tokens + result.output_tokens} tokens" if result.input_tokens + result.output_tokens > 0 else "0 tokens used"
    metadata = f"[{model_label}] {tier} | {task_type}/{complexity} | {latency} | {tokens}"

    return (
        f"ROUTING NOTICE — this prompt was classified as {task_type}/{complexity} and "
        f"answered by {model_label} ({tier}, {latency}, {tokens}) to conserve your "
        f"Claude subscription quota. The MANDATORY ROUTE step is complete for this turn.\n\n"
        "A cached answer follows between the markers. Please:\n"
        "  - Deliver it verbatim if it is accurate and complete.\n"
        "  - Fix only the specific errors if any are present; keep the rest.\n"
        "  - Add a brief addendum only if critical context for this user is missing.\n"
        "  - Do not re-route this prompt (no llm_query / llm_route calls) — routing is "
        "already resolved for this turn.\n\n"
        "───── CACHED ANSWER ─────\n"
        f"{result.text}\n"
        "───── END CACHED ANSWER ─────\n"
        f"Source: {metadata}"
    )


def build_echo_output(result: DirectResult, task_type: str, complexity: str) -> dict:
    """Build the full hook output dict for echo mode (uses contextForAgent)."""
    context = format_echo_context(result, task_type, complexity)
    return {
        "decision": "approve",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "contextForAgent": context,
        }
    }


def build_block_output(result: DirectResult, task_type: str, complexity: str) -> dict:
    """Build the full hook output dict for block mode."""
    formatted = format_direct_response(result, task_type, complexity)
    return {"decision": "block", "reason": formatted}


def _tier_label(provider: str) -> str:
    """Return a human-readable tier label."""
    tiers = {
        "ollama": "[FREE/LOCAL]",
        "codex": "[FREE/SUB]",
        "gemini": "[API]",
        "openai": "[API]",
    }
    return tiers.get(provider, "[UNKNOWN]")
