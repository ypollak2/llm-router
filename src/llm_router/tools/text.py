"""Text LLM tools — llm_query, llm_research, llm_generate, llm_analyze, llm_code, llm_edit."""

from __future__ import annotations

import asyncio
import os

from mcp.server.fastmcp import Context

from llm_router.config import get_config
from llm_router.cost import log_cc_hint, log_compression_stat
from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType
from llm_router import state as _state


# ---------------------------------------------------------------------------
# Explainability helper — injected into every routed response when
# LLM_ROUTER_EXPLAIN=1 is set in the environment.
# ---------------------------------------------------------------------------

#: Approximate cost-per-1k-output-tokens for the Sonnet baseline used in
#: "why not Opus/Sonnet?" comparisons shown in explain mode.
_COST_PER_1K = {
    "claude-opus-4-6":            0.075,
    "claude-sonnet-4-6":          0.015,
    "claude-haiku-4-5-20251001":  0.00125,
    "gemini/gemini-2.5-flash":    0.00035,
    "openai/gpt-4o":              0.010,
    "openai/gpt-4o-mini":         0.0006,
    "groq/llama-3.3-70b-versatile": 0.00059,
}
_SONNET_BASELINE = "claude-sonnet-4-6"
_SONNET_COST     = _COST_PER_1K[_SONNET_BASELINE]


def _explain_prefix(resp: LLMResponse, task: str, confidence: float = 0.0) -> str:
    """Return a routing explanation prefix when LLM_ROUTER_EXPLAIN=1.

    Format: ``[→ model-name · task · confidence% · $cost · Nx cheaper]``

    The prefix is prepended to every routed response so the user can see at a
    glance which model handled the request and why it was cheaper than Sonnet.
    Returns an empty string when the env var is not set.
    """
    if os.getenv("LLM_ROUTER_EXPLAIN") != "1":
        return ""

    model_short = resp.model.split("/")[-1] if resp.model else "unknown"
    conf_str = f" · {confidence:.0%}" if confidence > 0 else ""

    # Cost comparison vs Sonnet baseline (per-call savings)
    actual_cost = _COST_PER_1K.get(resp.model, _SONNET_COST)
    if actual_cost < _SONNET_COST and actual_cost > 0:
        ratio = _SONNET_COST / actual_cost
        savings_str = f" · {ratio:.1f}x cheaper than Sonnet"
    elif resp.model == _SONNET_BASELINE:
        savings_str = " · baseline"
    else:
        savings_str = ""

    cost_str = f" · ${resp.cost_usd:.5f}" if resp.cost_usd else ""
    return f"[→ {model_short} · {task}{conf_str}{cost_str}{savings_str}]\n\n"

# Subscription pressure thresholds — mirror the auto-route hook logic exactly.
# When below threshold, tools return a subscription hint instead of an API call.
_SUB_THRESHOLDS = {
    "simple":   ("session",  0.85),  # session ≥ 85% → external
    "moderate": ("sonnet",   0.95),  # sonnet  ≥ 95% → external
    "complex":  ("weekly",   0.95),  # weekly  ≥ 95% (or session ≥ 95%) → external
}
_SUB_MODELS = {
    "simple":   "claude-haiku-4-5-20251001",
    "moderate": None,                 # passthrough — Sonnet is already active
    "complex":  "claude-opus-4-6",
}


def _subscription_hint(task_type_label: str, complexity: str | None, prompt: str) -> str | None:
    """Return a subscription routing hint if CC-mode is active and below threshold.

    When Claude Code subscription has headroom, routing through external API
    calls wastes money. This helper returns a ``/model`` directive so Claude
    Code switches to the right subscription tier instead, or None when the
    threshold is exceeded and an external call is appropriate.

    Args:
        task_type_label: Label for logging (e.g. "query", "code").
        complexity: Task complexity — "simple", "moderate", or "complex".
            Defaults to "moderate" if None.
        prompt: The original prompt (included in the hint for context).

    Returns:
        A routing hint string starting with ``⚡ CC-MODE:``, or None.
    """
    cfg = get_config()
    if not cfg.llm_router_claude_subscription:
        return None

    eff = (complexity or "moderate").lower()
    if eff not in _SUB_THRESHOLDS:
        eff = "moderate"

    bucket, threshold = _SUB_THRESHOLDS[eff]
    target_model = _SUB_MODELS[eff]

    # Read current per-bucket pressure from in-process cache or usage.json
    _last_usage = _state.get_last_usage()
    if _last_usage:
        pct = {"session": _last_usage.session_pct,
               "sonnet":  _last_usage.sonnet_pct,
               "weekly":  _last_usage.weekly_pct}.get(bucket, 0.0)
    else:
        import json
        import os as _os
        try:
            raw = json.loads(open(_os.path.expanduser("~/.llm-router/usage.json")).read())
            v = float(raw.get(f"{bucket}_pct", 0.0))
            pct = v / 100.0 if v > 1.0 else v
        except Exception:
            pct = 0.0  # conservative: assume no pressure → use subscription

    # Also check session for "complex" (session ≥ 95% triggers full external)
    if eff == "complex" and _last_usage and _last_usage.session_pct >= 0.95:
        return None

    if pct >= threshold:
        return None  # over threshold → fall through to external call

    if target_model is None:
        return None  # moderate + below threshold → Sonnet passthrough, no hint needed

    hint = (
        f"⚡ CC-MODE: {eff}/{bucket}={pct:.0%} < {threshold:.0%} — "
        f"use subscription model.\n\n"
        f"/model {target_model}\n\n"
        f"{prompt}"
    )
    # Log asynchronously — fire-and-forget, don't block the hint return
    import asyncio as _asyncio
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(log_cc_hint(task_type_label, target_model))
    except Exception:
        pass
    return hint


def _apply_response_compression(content: str) -> tuple[str, bool]:
    """Apply response compression if enabled and beneficial.
    
    Args:
        content: The response content to potentially compress
        
    Returns:
        Tuple of (possibly_compressed_content, was_compressed)
    """
    # Check if compression is enabled
    if os.getenv("LLM_ROUTER_COMPRESS_RESPONSE", "").lower() != "true":
        return content, False
    
    # Skip compression for very short responses
    if len(content.strip()) < 200:
        return content, False
    
    try:
        from llm_router.compression import ResponseCompressor
        
        compressor = ResponseCompressor(enable=True)
        result = compressor.compress(content, target_reduction=0.5)
        
        # Only use compressed version if meaningful compression achieved
        if result.compression_ratio < 0.95:
            # Log compression stat asynchronously (fire and forget)
            def _log_async():
                try:
                    asyncio.run(
                        log_compression_stat(
                            command="response",
                            layer="token-savior",
                            original_tokens=result.original_tokens,
                            compressed_tokens=result.compressed_tokens,
                            compression_ratio=result.compression_ratio,
                            strategy=",".join(result.stages_applied),
                        )
                    )
                except Exception:
                    pass  # Silent failure on logging
            
            # Try to log in background (non-blocking)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(
                        log_compression_stat(
                            command="response",
                            layer="token-savior",
                            original_tokens=result.original_tokens,
                            compressed_tokens=result.compressed_tokens,
                            compression_ratio=result.compression_ratio,
                            strategy=",".join(result.stages_applied),
                        )
                    )
            except Exception:
                pass  # Silent failure - don't block response
            
            return result.output, True
    except Exception:
        pass  # Silent failure - return original if compression fails
    
    return content, False


def _format_response(resp: LLMResponse, explain: str | None = None) -> str:
    """Format a response with consistent header and optional explanation prefix.

    All tools use this function to ensure uniform response formatting across
    all 48 MCP tools. Format:

        [explain prefix if enabled]
        > 🤖 **model** · tokens · $cost · duration
        [optional empty line]
        [content]
        [optional compression note]

    Applies response compression (Layer 3: Token-Savior) if enabled via
    LLM_ROUTER_COMPRESS_RESPONSE=true environment variable.

    Args:
        resp: The LLM response object with model, tokens, cost, latency.
        explain: Optional explanation prefix (from _explain_prefix).

    Returns:
        Formatted response string.
    """
    parts = []
    if explain:
        parts.append(explain.rstrip())
    parts.append(resp.header())
    if resp.content:
        parts.append("")
        # Apply response compression if enabled
        content, was_compressed = _apply_response_compression(resp.content)
        parts.append(content)
        if was_compressed:
            parts.append("\n[Response compressed via Token-Savior. Original available if needed.]")
    return "\n".join(parts)


async def llm_query(
    prompt: str,
    ctx: Context,
    complexity: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
) -> str:
    """Send a general query to the best available LLM.

    Routes by complexity: simple→Haiku/Flash, moderate→Sonnet/GPT-4o, complex→Opus/o3.

    Args:
        prompt: The question or prompt to send.
        complexity: Task complexity — "simple", "moderate", or "complex". Drives model
            selection: simple→cheap (Haiku/Flash), moderate→balanced (Sonnet/GPT-4o),
            complex→premium (Opus/o3). Auto-detected from prompt length when omitted.
        model: Explicit model override, bypasses complexity routing entirely.
        system_prompt: Optional system instructions.
        temperature: Sampling temperature (0.0-2.0).
        max_tokens: Maximum output tokens.
        context: Optional conversation context to help the model understand the broader task.
    """
    if hint := _subscription_hint("query", complexity, prompt):
        return hint
    resp = await route_and_call(
        TaskType.QUERY, prompt,
        complexity_hint=complexity,
        model_override=model, system_prompt=system_prompt,
        temperature=temperature, max_tokens=max_tokens, ctx=ctx,
        caller_context=context,
    )
    return _format_response(resp, _explain_prefix(resp, "query"))


async def llm_research(
    prompt: str,
    ctx: Context,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
) -> str:
    """Search-augmented research query — routes to Perplexity for web-grounded answers.

    Best for: fact-checking, current events, finding sources, market research.

    Args:
        prompt: The research question.
        system_prompt: Optional system instructions.
        max_tokens: Maximum output tokens.
        context: Optional conversation context to help the model understand the broader task.
    """
    _cfg = get_config()
    no_perplexity = not _cfg.perplexity_api_key

    # In subscription mode with no Perplexity, prefer Opus subscription over
    # costly external fallback (o3/gpt-4o) when pressure allows it.
    if no_perplexity and _cfg.llm_router_claude_subscription:
        from llm_router.claude_usage import get_claude_pressure
        pressure = get_claude_pressure()
        if pressure < 0.85:
            return (
                "⚡ CC-MODE: No Perplexity key — routing to Opus subscription (pressure "
                f"{pressure:.0%} < 85% threshold).\n\n"
                "/model claude-opus-4-6\n\n"
                f"{prompt}"
            )

    resp = await route_and_call(
        TaskType.RESEARCH, prompt,
        # Without Perplexity, escalate to PREMIUM so the fallback chain uses
        # o3 / Gemini 2.5 Pro rather than silently degrading to BALANCED tier.
        profile=RoutingProfile.PREMIUM if no_perplexity else None,
        system_prompt=system_prompt, max_tokens=max_tokens,
        temperature=0.3, ctx=ctx, caller_context=context,
    )
    
    result = _format_response(resp, _explain_prefix(resp, "research"))
    
    if resp.citations:
        result += "\n\n**Sources:**\n" + "\n".join(f"- {c}" for c in resp.citations)
    
    if no_perplexity and "perplexity" not in resp.model.lower():
        result += (
            "\n\n⚠️  No PERPLEXITY_API_KEY — web search unavailable. "
            "Escalated to PREMIUM non-web model (results may be stale). "
            "Set PERPLEXITY_API_KEY for live web search."
        )
    return result


async def llm_generate(
    prompt: str,
    ctx: Context,
    complexity: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
) -> str:
    """Generate creative or long-form content — routes to the best generation model.

    Best for: writing, summarization, brainstorming, content creation.

    Args:
        prompt: What to generate.
        complexity: Task complexity — "simple", "moderate", or "complex". Drives model
            selection. Simple tasks (short summaries) use cheap models; complex tasks
            (long-form, nuanced writing) use premium models.
        system_prompt: Optional system instructions (tone, format, audience).
        temperature: Sampling temperature (higher = more creative).
        max_tokens: Maximum output tokens.
        context: Optional conversation context to help the model understand the broader task.
    """
    if hint := _subscription_hint("generate", complexity, prompt):
        return hint
    resp = await route_and_call(
        TaskType.GENERATE, prompt,
        complexity_hint=complexity,
        system_prompt=system_prompt, temperature=temperature,
        max_tokens=max_tokens, ctx=ctx, caller_context=context,
    )
    
    return _format_response(resp, _explain_prefix(resp, "generate"))


async def llm_analyze(
    prompt: str,
    ctx: Context,
    complexity: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
) -> str:
    """Deep analysis task — routes to the strongest reasoning model.

    Best for: data analysis, code review, problem decomposition, debugging.

    Args:
        prompt: What to analyze.
        complexity: Task complexity — "simple", "moderate", or "complex". Analysis tasks
            default to at least moderate. Pass "complex" for multi-file reviews or
            architecture decisions that warrant Opus/o3.
        system_prompt: Optional system instructions.
        max_tokens: Maximum output tokens.
        context: Optional conversation context to help the model understand the broader task.
    """
    # Analysis is never trivially simple — floor at moderate so Haiku is never
    # chosen for a task that inherently requires reasoning.
    effective_complexity = complexity or "moderate"
    if hint := _subscription_hint("analyze", effective_complexity, prompt):
        return hint
    resp = await route_and_call(
        TaskType.ANALYZE, prompt,
        complexity_hint=effective_complexity,
        system_prompt=system_prompt, temperature=0.3,
        max_tokens=max_tokens, ctx=ctx, caller_context=context,
    )
    
    return _format_response(resp, _explain_prefix(resp, "analyze"))


async def llm_code(
    prompt: str,
    ctx: Context,
    complexity: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
) -> str:
    """Coding task — routes to the best coding model.

    Best for: code generation, refactoring suggestions, algorithm design.

    Args:
        prompt: The coding task or question.
        complexity: Task complexity — "simple", "moderate", or "complex". Drives model
            selection: simple questions use Haiku/Flash, actual implementation tasks use
            Sonnet/GPT-4o, large refactors or architecture work use Opus/o3.
        system_prompt: Optional system instructions (language, framework, style).
        max_tokens: Maximum output tokens.
        context: Optional conversation context to help the model understand the broader task.
    """
    if hint := _subscription_hint("code", complexity, prompt):
        return hint
    resp = await route_and_call(
        TaskType.CODE, prompt,
        complexity_hint=complexity,
        system_prompt=system_prompt, temperature=0.2,
        max_tokens=max_tokens, ctx=ctx, caller_context=context,
    )
    
    return _format_response(resp, _explain_prefix(resp, "code"))


async def llm_edit(
    task: str,
    files: list[str],
    ctx: Context,
    context: str | None = None,
) -> str:
    """Route code-edit reasoning to a cheap model and return exact edit instructions.

    Instead of Opus reasoning about what to change (expensive), a cheap model
    reads the files, figures out the edits, and returns JSON ``{file, old_string,
    new_string}`` pairs that Claude can apply mechanically via the Edit tool.

    **How to use the result**: After calling this tool, apply each edit instruction
    using the Edit tool with the exact old_string → new_string pairs provided.

    Best for: refactoring, bug fixes, adding small features to existing files.

    Args:
        task: Natural-language description of what to change (e.g.
            "Add type hints to all public functions in router.py").
        files: List of file paths to read and include in the prompt.
            Relative paths are resolved from the current working directory.
            Files larger than 32 KB are truncated with a note.
        context: Optional conversation context to help the model understand the task.
    """
    from llm_router.edit import (
        build_edit_prompt, format_edit_result,
        parse_edit_response, read_file_for_edit,
    )

    # Read all requested files
    file_contents: dict[str, str] = {}
    read_notes: list[str] = []
    for path in files:
        content, truncated = read_file_for_edit(path)
        file_contents[path] = content
        if truncated:
            read_notes.append(f"{path}: truncated to 32 KB")

    # Build the prompt and route to cheap code model
    prompt = build_edit_prompt(task, file_contents)
    if context:
        prompt = f"{context}\n\n---\n\n{prompt}"

    resp = await route_and_call(
        TaskType.CODE, prompt,
        system_prompt=(
            "You are a precise code editor. Return ONLY a JSON array of edit "
            "instructions. No prose, no explanation outside the JSON."
        ),
        temperature=0.1,
        ctx=ctx,
    )

    instructions, warnings = parse_edit_response(resp.content)
    if read_notes:
        warnings = [f"File truncated: {n}" for n in read_notes] + warnings

    return format_edit_result(instructions, warnings, resp.header())


def register(mcp, should_register=None) -> None:
    """Register text LLM tools with the FastMCP instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_query"):
        mcp.tool()(llm_query)
    if gate("llm_research"):
        mcp.tool()(llm_research)
    if gate("llm_generate"):
        mcp.tool()(llm_generate)
    if gate("llm_analyze"):
        mcp.tool()(llm_analyze)
    if gate("llm_code"):
        mcp.tool()(llm_code)
    if gate("llm_edit"):
        mcp.tool()(llm_edit)
