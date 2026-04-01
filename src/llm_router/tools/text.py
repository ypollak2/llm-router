"""Text LLM tools — llm_query, llm_research, llm_generate, llm_analyze, llm_code, llm_edit."""

from __future__ import annotations

from mcp.server.fastmcp import Context

from llm_router.config import get_config
from llm_router.cost import log_cc_hint
from llm_router.router import route_and_call
from llm_router.types import RoutingProfile, TaskType
from llm_router import state as _state

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
    return f"{resp.header()}\n\n{resp.content}"


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
    result = resp.header() + "\n\n" + resp.content
    if resp.citations:
        result += "\n\n**Sources:**\n" + "\n".join(f"- {c}" for c in resp.citations)
    if no_perplexity and "perplexity" not in resp.model.lower():
        result += (
            "\n\n---\n⚠️  No PERPLEXITY_API_KEY — web search unavailable. "
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
    return f"{resp.header()}\n\n{resp.content}"


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
    return f"{resp.header()}\n\n{resp.content}"


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
    return f"{resp.header()}\n\n{resp.content}"


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


def register(mcp) -> None:
    """Register text LLM tools with the FastMCP instance."""
    mcp.tool()(llm_query)
    mcp.tool()(llm_research)
    mcp.tool()(llm_generate)
    mcp.tool()(llm_analyze)
    mcp.tool()(llm_code)
    mcp.tool()(llm_edit)
