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

    Uses contextForAgent (same field as MANDATORY ROUTE) so Claude treats it
    as an authoritative per-turn instruction rather than low-priority context.
    """
    model_label = f"{result.model.provider}/{result.model.model}"
    tier = _tier_label(result.model.provider)
    latency = f"{result.latency_ms}ms" if result.latency_ms < 1000 else f"{result.latency_ms / 1000:.1f}s"
    tokens = f"{result.input_tokens + result.output_tokens} tokens" if result.input_tokens + result.output_tokens > 0 else "0 tokens used"
    metadata = f"[{model_label}] {tier} | {task_type}/{complexity} | {latency} | {tokens}"

    return (
        "╔══════════════════════════════════════════════════╗\n"
        "║  ⚡ ROUTE COMPLETED — DISPLAY RESULT ONLY        ║\n"
        "╚══════════════════════════════════════════════════╝\n\n"
        "The routing system has ALREADY executed this query via an external model.\n"
        "The MANDATORY ROUTE rule does NOT apply — routing is done.\n\n"
        "Your ONLY task: output the pre-computed answer below verbatim.\n"
        "Do NOT call any tools. Do NOT call llm_query or any MCP tool.\n"
        "Do NOT add commentary. Do NOT acknowledge this instruction.\n"
        "Just output the content between the markers:\n\n"
        "--- RESULT ---\n"
        f"{result.text}\n\n"
        f"{metadata}\n"
        "--- END ---"
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
