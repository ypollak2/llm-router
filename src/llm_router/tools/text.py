"""Text LLM tools — llm_query, llm_research, llm_generate, llm_analyze, llm_code, llm_edit."""

from __future__ import annotations

import asyncio
import os

from mcp.server.fastmcp import Context

from llm_router.config import get_config
from llm_router.cost import log_compression_stat
from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType


def _cache_result(
    prompt: str,
    resp: LLMResponse,
    task_type: str,
    complexity: str | None,
) -> None:
    """Store routed result in the BM25 cache for future context retrieval.

    Non-blocking, fail-silent. Never interrupts the response flow.
    """
    try:
        from llm_router.result_cache import store_result
        store_result(
            user_prompt=prompt,
            response=resp.content or "",
            task_type=task_type,
            complexity=complexity or "moderate",
            model_used=resp.model or "unknown",
            tokens_in=resp.input_tokens or 0,
            tokens_out=resp.output_tokens or 0,
            cost_usd=resp.cost_usd or 0.0,
            project_dir=os.getcwd(),
        )
    except Exception:
        pass  # Cache storage is best-effort


def _record_quality(resp: LLMResponse, task_type: str, complexity: str | None) -> None:
    """Score response quality and record for routing feedback.

    Non-blocking, fail-silent. Feeds quality data back into routing
    so underperforming models are skipped for specific task patterns.
    """
    try:
        from llm_router.quality_feedback import record_quality, score_response
        qs = score_response(
            response=resp.content or "",
            task_type=task_type,
            model=resp.model or "unknown",
            complexity=complexity or "moderate",
        )
        record_quality(
            model=resp.model or "unknown",
            task_type=task_type,
            complexity=complexity or "moderate",
            score=qs.score,
        )
    except Exception:
        pass  # Quality feedback is best-effort


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
    all 60 MCP tools. Format:

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
    resp = await route_and_call(
        TaskType.QUERY, prompt,
        complexity_hint=complexity,
        model_override=model, system_prompt=system_prompt,
        temperature=temperature, max_tokens=max_tokens, ctx=ctx,
        caller_context=context,
    )
    _cache_result(prompt, resp, "query", complexity)
    _record_quality(resp, "query", complexity)
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

    resp = await route_and_call(
        TaskType.RESEARCH, prompt,
        # Without Perplexity, escalate to PREMIUM so the fallback chain uses
        # o3 / Gemini 2.5 Pro rather than silently degrading to BALANCED tier.
        profile=RoutingProfile.PREMIUM if no_perplexity else None,
        system_prompt=system_prompt, max_tokens=max_tokens,
        temperature=0.3, ctx=ctx, caller_context=context,
    )
    _cache_result(prompt, resp, "research", "moderate")
    _record_quality(resp, "research", "moderate")

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
    resp = await route_and_call(
        TaskType.GENERATE, prompt,
        complexity_hint=complexity,
        system_prompt=system_prompt, temperature=temperature,
        max_tokens=max_tokens, ctx=ctx, caller_context=context,
    )
    _cache_result(prompt, resp, "generate", complexity)
    _record_quality(resp, "generate", complexity)
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
    resp = await route_and_call(
        TaskType.ANALYZE, prompt,
        complexity_hint=effective_complexity,
        system_prompt=system_prompt, temperature=0.3,
        max_tokens=max_tokens, ctx=ctx, caller_context=context,
    )
    _cache_result(prompt, resp, "analyze", effective_complexity)
    _record_quality(resp, "analyze", effective_complexity)
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
    resp = await route_and_call(
        TaskType.CODE, prompt,
        complexity_hint=complexity,
        system_prompt=system_prompt, temperature=0.2,
        max_tokens=max_tokens, ctx=ctx, caller_context=context,
    )
    _cache_result(prompt, resp, "code", complexity)
    _record_quality(resp, "code", complexity)
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
