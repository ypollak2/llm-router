"""FastMCP server — exposes LLM routing tools to Claude Code."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import Context, FastMCP

from llm_router import providers
from llm_router.cache import get_cache
from llm_router.classifier import classify_complexity
from llm_router.codex_agent import is_codex_available, run_codex
from llm_router.provider_budget import get_provider_budgets, rank_external_models
from llm_router.claude_usage import (
    FETCH_USAGE_JS, ClaudeSubscriptionUsage, parse_api_response,
)
from llm_router.config import get_config
from llm_router.cost import (
    get_daily_claude_breakdown, get_daily_claude_tokens,
    get_monthly_spend, get_savings_summary, log_claude_usage,
)
from llm_router.health import get_tracker
from llm_router.model_selector import select_model
from llm_router.orchestrator import PIPELINE_TEMPLATES, auto_orchestrate, run_pipeline
from llm_router.profiles import complexity_to_profile
from llm_router.router import route_and_call
from llm_router.types import (
    ClassificationResult, Complexity, QualityMode, RoutingProfile, RoutingRecommendation,
    TaskType, Tier, PRO_FEATURES, colorize_provider, _budget_bar,
)

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


# ── Smart Router ─────────────────────────────────────────────────────────────


@mcp.tool()
async def llm_classify(
    prompt: str,
    ctx: Context,
    quality: str | None = None,
    min_model: str | None = None,
) -> str:
    """Classify a prompt's complexity and recommend which model to use.

    Returns a smart recommendation considering complexity, daily token budget,
    quality preference, and minimum model floor. Includes budget usage bar.

    Complexity drives model selection at all times:
    - simple → haiku, moderate → sonnet, complex → opus
    Budget pressure is a late safety net only:
    - 0-85%: no downshift — complexity routing handles efficiency
    - 85-95%: downshift by 1 tier (opus→sonnet, sonnet→haiku)
    - 95%+: downshift by 2 tiers, warns user

    Args:
        prompt: The task or question to classify.
        quality: Override quality mode — "best", "balanced", or "conserve".
        min_model: Override minimum model floor — "haiku", "sonnet", or "opus".
    """
    config = get_config()

    # Classify complexity
    try:
        classification = await classify_complexity(prompt)
    except Exception as e:
        await ctx.warning(f"Classification failed: {e}")
        classification = ClassificationResult(
            complexity=Complexity.MODERATE,
            confidence=0.0,
            reasoning=f"error fallback: {e}",
            inferred_task_type=None,
            classifier_model="none",
            classifier_cost_usd=0.0,
            classifier_latency_ms=0.0,
        )

    # Compute budget pressure — prefer live subscription data over token estimates
    budget_pct = 0.0
    daily_tokens = await get_daily_claude_tokens()
    if _last_usage and _last_usage.session:
        # Use time-aware pressure: if session resets soon, reduce urgency
        budget_pct = _last_usage.effective_pressure
    elif config.daily_token_budget > 0:
        budget_pct = daily_tokens / config.daily_token_budget

    # Resolve quality mode and min_model
    q_mode = QualityMode(quality) if quality else config.quality_mode
    floor = min_model or config.min_model

    # Get smart recommendation
    rec = select_model(classification, budget_pct, q_mode, floor)

    # If Claude is tight and task is complex, find best external fallback
    external_fallback = None
    if budget_pct >= 0.85 and classification.complexity in (Complexity.COMPLEX, Complexity.MODERATE):
        budgets = await get_provider_budgets()
        min_q = 0.85 if classification.complexity == Complexity.COMPLEX else 0.70
        ranked = rank_external_models(budgets, min_quality=min_q)
        if ranked:
            external_fallback = ranked[0][0]  # best available model
            # Rebuild rec with external_fallback attached
            rec = RoutingRecommendation(
                classification=rec.classification,
                recommended_model=rec.recommended_model,
                base_model=rec.base_model,
                budget_pct_used=rec.budget_pct_used,
                was_downshifted=rec.was_downshifted,
                quality_mode=rec.quality_mode,
                min_model=rec.min_model,
                reasoning=rec.reasoning,
                external_fallback=external_fallback,
            )

    task = classification.inferred_task_type.value if classification.inferred_task_type else "query"
    profile = complexity_to_profile(classification.complexity)

    W = 58
    HR = "+" + "-" * W + "+"

    def row(text: str) -> str:
        return f"| {text:<{W-1}}|"

    action = f'  Action: Agent(model: "{rec.recommended_model}")  (free)'
    ext_cmd = f'  External: llm_route(profile="{profile.value}")'
    task_line = f"  Task: {task}  |  {rec.reasoning}"

    lines = [HR, row(rec.header()), HR, row(action)]

    if external_fallback:
        lines.append(row(f"  Fallback: {external_fallback}  (preserves Claude quota)"))

    lines.extend([row(ext_cmd), row(task_line)])

    if config.daily_token_budget > 0:
        remaining = max(0, config.daily_token_budget - daily_tokens)
        lines.append(row(f"  Tokens: {daily_tokens:,} / {config.daily_token_budget:,} ({remaining:,} left)"))

    if budget_pct >= 0.95:
        lines.append(HR)
        lines.append(row(f"  !! BUDGET CRITICAL ({budget_pct:.0%}) -- use external or wait for reset"))
        if external_fallback:
            lines.append(row(f"  >> Suggested: route to {external_fallback}"))
    elif budget_pct >= 0.85:
        lines.append(HR)
        lines.append(row(f"  ~~ Budget low ({budget_pct:.0%}) -- downshifted {rec.base_model} -> {rec.recommended_model}"))

    lines.append(HR)
    return "\n".join(lines)


@mcp.tool()
async def llm_track_usage(
    model: str,
    tokens_used: int,
    complexity: str = "moderate",
) -> str:
    """Report Claude Code model token usage for budget tracking.

    Call this after using an Agent with haiku/sonnet to track token consumption
    against the daily budget. This enables progressive model downshifting.
    Shows per-call savings vs opus and cumulative session savings.

    Args:
        model: The Claude model used — "haiku", "sonnet", or "opus".
        tokens_used: Approximate tokens consumed by the Agent call.
        complexity: The task complexity that was routed — "simple", "moderate", "complex".
    """
    if model not in ("haiku", "sonnet", "opus"):
        return f"Invalid model: {model}. Use haiku, sonnet, or opus."

    call_savings = await log_claude_usage(model, tokens_used, complexity)

    config = get_config()
    daily_total = await get_daily_claude_tokens()
    breakdown = await get_daily_claude_breakdown()
    cumulative = await get_savings_summary("today")

    model_icons = {"haiku": "\U0001f7e1", "sonnet": "\U0001f535", "opus": "\U0001f7e3"}
    icon = model_icons.get(model, "\u2b1c")

    lines = [f"{icon} Logged **{tokens_used:,} tokens** for **{model}** ({complexity})"]

    # Per-call savings
    cost_s = call_savings["cost_saved_usd"]
    time_s = call_savings["time_saved_sec"]
    if cost_s > 0 or time_s > 0:
        lines.append(f"\U0001f4b0 **This call saved:** ${cost_s:.4f} and {time_s:.1f}s vs opus")
    elif model == "opus":
        lines.append("\U0001f7e3 Used opus — no savings on this call (max quality)")

    # Budget status
    lines.append("")
    if config.daily_token_budget > 0:
        remaining = max(0, config.daily_token_budget - daily_total)
        pct = daily_total / config.daily_token_budget
        lines.append(f"**Budget:** {_budget_bar(pct)} {pct:.0%} ({remaining:,} tokens remaining)")
    else:
        lines.append(f"**Daily total:** {daily_total:,} tokens")

    if breakdown:
        model_parts = [f"{model_icons.get(m, '')} {m}: {t:,}" for m, t in sorted(breakdown.items())]
        lines.append(f"Breakdown: {' | '.join(model_parts)}")

    # Cumulative savings
    total_saved = cumulative["cost_saved_usd"]
    total_time = cumulative["time_saved_sec"]
    total_calls = cumulative["total_calls"]
    if total_calls > 1:
        lines.append("")
        lines.append(f"\U0001f4ca **Today's totals** ({total_calls} calls):")
        lines.append(f"   \U0001f4b0 Saved: **${total_saved:.4f}** | \u23f1\ufe0f Time saved: **{_format_time(total_time)}**")

    return "\n".join(lines)


def _format_time(seconds: float) -> str:
    """Format seconds into a human-readable string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{seconds / 3600:.1f}h"


@mcp.tool()
async def llm_route(
    prompt: str,
    ctx: Context,
    task_type: str | None = None,
    complexity_override: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Smart router — classifies task complexity, then routes to the optimal external LLM.

    Uses a cheap classifier to assess complexity, then picks the right model tier:
    - simple → budget models (Gemini Flash, GPT-4o-mini)
    - moderate → balanced models (GPT-4o, Sonnet, Gemini Pro)
    - complex → premium models (o3, Opus)

    For routing to Claude Code's own models (haiku/sonnet) without API keys,
    use llm_classify instead and follow its recommendation.

    Args:
        prompt: The task or question to route.
        task_type: Optional hint — "query", "research", "generate", "analyze", "code". Auto-detected if omitted.
        complexity_override: Skip classification — force "simple", "moderate", or "complex".
        system_prompt: Optional system instructions.
        temperature: Sampling temperature (0.0-2.0).
        max_tokens: Maximum output tokens.
    """
    # Step 1: Classify complexity (or use override)
    if complexity_override:
        try:
            complexity = Complexity(complexity_override.lower())
        except ValueError:
            return f"Invalid complexity: {complexity_override}. Choose: simple, moderate, complex."
        classification = ClassificationResult(
            complexity=complexity,
            confidence=1.0,
            reasoning="user override",
            inferred_task_type=TaskType(task_type) if task_type else None,
            classifier_model="override",
            classifier_cost_usd=0.0,
            classifier_latency_ms=0.0,
        )
    else:
        try:
            classification = await classify_complexity(prompt)
        except Exception as e:
            await ctx.warning(f"Classification failed: {e} — defaulting to moderate")
            classification = ClassificationResult(
                complexity=Complexity.MODERATE,
                confidence=0.0,
                reasoning=f"error fallback: {e}",
                inferred_task_type=None,
                classifier_model="none",
                classifier_cost_usd=0.0,
                classifier_latency_ms=0.0,
            )

    # Step 2: Resolve task type and profile
    resolved_task_type = (
        TaskType(task_type) if task_type
        else classification.inferred_task_type
        or TaskType.QUERY
    )
    profile = complexity_to_profile(classification.complexity)

    await ctx.info(
        f"Classified as {classification.complexity.value} "
        f"({classification.confidence:.0%}) → {profile.value} profile"
    )

    # Step 3: Route and call
    resp = await route_and_call(
        resolved_task_type, prompt,
        profile=profile,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        ctx=ctx,
    )

    # Step 4: Build response with classification + routing info
    total_cost = classification.classifier_cost_usd + resp.cost_usd
    lines = [
        classification.header(),
        resp.header(),
        f"> **Total cost: ${total_cost:.6f}**",
        "",
        resp.content,
    ]

    return "\n".join(lines)


# ── Streaming ────────────────────────────────────────────────────────────────


@mcp.tool()
async def llm_stream(
    prompt: str,
    ctx: Context,
    task_type: str = "query",
    model: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Stream an LLM response for long-running tasks — shows output as it arrives.

    Uses the same routing logic as llm_route but streams chunks instead of
    waiting for the full response. Ideal for long-form generation, research
    summaries, or any task where seeing partial output early is valuable.

    Args:
        prompt: The task or question to stream.
        task_type: Task type hint — "query", "research", "generate", "analyze", "code".
        model: Optional model override (e.g. "openai/gpt-4o", "gemini/gemini-2.5-flash").
        system_prompt: Optional system instructions.
        temperature: Sampling temperature (0.0-2.0).
        max_tokens: Maximum output tokens.
    """
    import json as _json

    from llm_router.profiles import get_model_chain, provider_from_model

    resolved_task = TaskType(task_type) if task_type else TaskType.QUERY
    config = get_config()
    profile = config.llm_router_profile

    # Determine model to use
    if model:
        target_model = model
    else:
        chain = get_model_chain(resolved_task, profile)
        target_model = chain[0] if chain else "gemini/gemini-2.5-flash"

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    await ctx.info(f"Streaming from {target_model}...")

    # Collect streamed content
    from llm_router.providers import call_llm_stream

    collected: list[str] = []
    meta = {}

    async for chunk in call_llm_stream(
        target_model,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
    ):
        if chunk.startswith("\n[META]"):
            meta = _json.loads(chunk[7:])
        else:
            collected.append(chunk)

    content = "".join(collected)

    # Log usage
    if meta:
        from llm_router.cost import log_usage
        await log_usage(
            provider=meta.get("provider", provider_from_model(target_model)),
            model=target_model,
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
            cost_usd=meta.get("cost_usd", 0.0),
            task_type=resolved_task.value,
        )

    cost_str = f"${meta.get('cost_usd', 0):.6f}" if meta else "$?.??????"
    latency_str = f"{meta.get('latency_ms', 0):.0f}ms" if meta else "?ms"
    header = f"> **Streamed from {target_model}** | {cost_str} | {latency_str}"

    return f"{header}\n\n{content}"


# ── Text LLM Tools ───────────────────────────────────────────────────────────


@mcp.tool()
async def llm_query(
    prompt: str,
    ctx: Context,
    model: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send a general query to the best available LLM.

    Auto-routes based on the active profile. Supports 10+ text LLM providers.

    Args:
        prompt: The question or prompt to send.
        model: Optional model override (e.g. "openai/gpt-4o", "gemini/gemini-2.5-flash", "anthropic/claude-sonnet-4-6", "deepseek/deepseek-chat").
        system_prompt: Optional system instructions.
        temperature: Sampling temperature (0.0-2.0).
        max_tokens: Maximum output tokens.
    """
    resp = await route_and_call(
        TaskType.QUERY, prompt,
        model_override=model, system_prompt=system_prompt,
        temperature=temperature, max_tokens=max_tokens, ctx=ctx,
    )
    return f"{resp.header()}\n\n{resp.content}"


@mcp.tool()
async def llm_research(
    prompt: str,
    ctx: Context,
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
        temperature=0.3, ctx=ctx,
    )
    result = resp.header() + "\n\n" + resp.content
    if resp.citations:
        result += "\n\n**Sources:**\n" + "\n".join(f"- {c}" for c in resp.citations)
    return result


@mcp.tool()
async def llm_generate(
    prompt: str,
    ctx: Context,
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
        max_tokens=max_tokens, ctx=ctx,
    )
    return f"{resp.header()}\n\n{resp.content}"


@mcp.tool()
async def llm_analyze(
    prompt: str,
    ctx: Context,
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
        max_tokens=max_tokens, ctx=ctx,
    )
    return f"{resp.header()}\n\n{resp.content}"


@mcp.tool()
async def llm_code(
    prompt: str,
    ctx: Context,
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
        max_tokens=max_tokens, ctx=ctx,
    )
    return f"{resp.header()}\n\n{resp.content}"


# ── Media Tools ──────────────────────────────────────────────────────────────


@mcp.tool()
async def llm_image(
    prompt: str,
    ctx: Context,
    model: str | None = None,
    size: str = "1024x1024",
    quality: str = "standard",
) -> str:
    """Generate an image — auto-routes to Gemini Imagen, DALL-E, Flux, or Stable Diffusion.

    Args:
        prompt: Description of the image to generate.
        model: Optional model override (e.g. "gemini/imagen-3", "openai/dall-e-3", "fal/flux-pro", "stability/stable-diffusion-3").
        size: Image size (e.g. "1024x1024", "1792x1024").
        quality: Image quality — "standard" or "hd" (DALL-E only).
    """
    resp = await route_and_call(
        TaskType.IMAGE, prompt,
        model_override=model,
        media_params={"size": size, "quality": quality}, ctx=ctx,
    )
    result = resp.header() + "\n\n" + resp.content
    if resp.media_url:
        result += f"\n\nImage URL: {resp.media_url}"
    return result


@mcp.tool()
async def llm_video(
    prompt: str,
    ctx: Context,
    model: str | None = None,
    duration: int = 5,
) -> str:
    """Generate a video — routes to Gemini Veo, Runway, Kling, or other video models.

    Args:
        prompt: Description of the video to generate.
        model: Optional model override (e.g. "gemini/veo-2", "runway/gen3a_turbo", "fal/kling-video").
        duration: Video duration in seconds (default: 5).
    """
    resp = await route_and_call(
        TaskType.VIDEO, prompt,
        model_override=model,
        media_params={"duration": duration}, ctx=ctx,
    )
    result = resp.header() + "\n\n" + resp.content
    if resp.media_url:
        result += f"\n\nVideo URL: {resp.media_url}"
    return result


@mcp.tool()
async def llm_audio(
    text: str,
    ctx: Context,
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
        media_params={"voice": voice}, ctx=ctx,
    )
    result = resp.header() + "\n\n" + resp.content
    if resp.media_url:
        result += f"\n\nAudio: {resp.media_url}"
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

    Free tier: up to 2-step pipelines. Pro tier: unlimited steps + auto-decomposition.

    Args:
        task: Description of the complex task to accomplish.
        template: Optional pipeline template: "research_report", "competitive_analysis", "content_pipeline", "code_review_fix". Omit for auto-decomposition.
    """
    config = get_config()

    # Auto-decomposition requires Pro
    if not template:
        tier_error = _check_tier("multi_step")
        if tier_error:
            return tier_error

    # Free tier: templates limited to 2-step max
    if template and template in PIPELINE_TEMPLATES:
        steps = PIPELINE_TEMPLATES[template]
        if config.llm_router_tier == Tier.FREE and len(steps) > 2:
            return (
                f"Template '{template}' has {len(steps)} steps — free tier allows up to 2. "
                "Upgrade to Pro for unlimited pipeline steps: https://llm-router.dev/pricing"
            )

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
    """Unified usage dashboard — Claude subscription, Codex, external APIs, and savings.

    Shows a complete picture of all LLM usage across all providers in one view.

    Args:
        period: Time period — "today", "week", "month", or "all".
    """
    from llm_router.claude_usage import _bar, _row, _time_until

    W = 58  # box inner width
    HR = "+" + "-" * W + "+"

    def row(text: str) -> str:
        return f"| {text:<{W-1}}|"

    def section(title: str) -> str:
        return "|" + f" {title} ".center(W, "-") + "|"

    lines: list[str] = [HR, "|" + " LLM Usage Dashboard ".center(W) + "|", HR]

    # ── Section 1: Claude Subscription ──
    lines.append(section("CLAUDE SUBSCRIPTION"))

    if _last_usage:
        if _last_usage.session:
            lines.append(_row("Session", _last_usage.session_pct, _time_until(_last_usage.session.resets_at), W + 2))
        if _last_usage.weekly_all:
            lines.append(_row("Weekly (all)", _last_usage.weekly_pct, _time_until(_last_usage.weekly_all.resets_at), W + 2))
        if _last_usage.weekly_sonnet:
            lines.append(_row("Sonnet only", _last_usage.sonnet_pct, _time_until(_last_usage.weekly_sonnet.resets_at), W + 2))

        raw_pressure = _last_usage.highest_pressure
        effective = _last_usage.effective_pressure
        mins = _last_usage.minutes_until_session_reset

        if effective >= 0.90:
            p_line = f"  !! PRESSURE {raw_pressure:.0%} -- downshifting to externals"
        elif raw_pressure >= 0.85 and effective < 0.85:
            p_line = f"  OK {raw_pressure:.0%} raw -> {effective:.0%} effective (reset {int(mins or 0)}m)"
        elif effective >= 0.70:
            p_line = f"  ~~ PRESSURE {raw_pressure:.0%} -- approaching threshold"
        else:
            p_line = f"  OK {raw_pressure:.0%} pressure -- full model selection"
        lines.append(row(p_line))
    else:
        lines.append(row("  (no live data -- run /claude-usage to fetch)"))

    lines.append(HR)

    # ── Section 2: Codex ──
    lines.append(section("CODEX (LOCAL)"))
    if is_codex_available():
        lines.append(row("  Status:  READY    Model: gpt-5.4    Cost: FREE"))
    else:
        lines.append(row("  Status:  NOT INSTALLED"))
    lines.append(HR)

    # ── Section 3: External API Spend ──
    api_title = f"EXTERNAL APIs ({period})"
    lines.append(section(api_title))
    budgets = await get_provider_budgets()
    if budgets:
        for provider, b in sorted(budgets.items(), key=lambda x: -x[1].spent_this_month):
            if b.monthly_limit > 0:
                bar = _bar(b.pct_used, 10)
                cell = f"  {provider:<12} ${b.spent_this_month:>7.2f} / ${b.monthly_limit:<6.2f} {bar} {b.pct_used:>3.0%}"
            elif b.spent_this_month > 0:
                cell = f"  {provider:<12} ${b.spent_this_month:>7.2f}   (no limit)"
            else:
                cell = f"  {provider:<12} $   0.00"
            lines.append(row(cell))
    else:
        lines.append(row("  (no external API usage)"))
    lines.append(HR)

    # ── Section 4: Routing Savings ──
    savings = await get_savings_summary(period)
    if savings["total_calls"] > 0:
        s = savings
        savings_title = f"ROUTING SAVINGS ({period})"
        lines.append(section(savings_title))

        calls_str = str(s["total_calls"])
        tokens_str = f"{s['total_tokens']:,}"
        lines.append(row(f"  Calls: {calls_str}    Tokens: {tokens_str}"))

        cost_str = f"${s['cost_saved_usd']:.4f}"
        time_str = _format_time(s["time_saved_sec"])
        lines.append(row(f"  Saved:  {cost_str}  |  Time: {time_str}"))

        if s["by_model"]:
            lines.append(row(""))
            for model, data in s["by_model"].items():
                tag = {"haiku": "H", "sonnet": "S", "opus": "O"}.get(model, "?")
                tok_str = f"{data['tokens']:,}"
                cell = f"  [{tag}] {model:<8} {data['calls']:>3} calls  {tok_str:>7} tok  saved ${data['cost_saved']:.4f}"
                lines.append(row(cell))

        from llm_router.types import MODEL_COST_PER_1K
        total_tokens_k = s["total_tokens"] / 1000
        opus_would_cost = total_tokens_k * MODEL_COST_PER_1K["opus"]
        if opus_would_cost > 0:
            actual_cost = opus_would_cost - s["cost_saved_usd"]
            pct_saved = (s["cost_saved_usd"] / opus_would_cost) * 100
            lines.append(row(""))
            lines.append(row(f"  Opus would cost: ${opus_would_cost:.4f}  ->  Actual: ${actual_cost:.4f}  ({pct_saved:.0f}% saved)"))
        lines.append(HR)

    # ── Section 5: Monthly Budget ──
    config = get_config()
    if config.llm_router_monthly_budget > 0:
        monthly_spend = await get_monthly_spend()
        budget = config.llm_router_monthly_budget
        remaining = max(0, budget - monthly_spend)
        pct = monthly_spend / budget if budget > 0 else 0
        lines.append(section("MONTHLY BUDGET"))
        bar = _bar(pct, 16)
        lines.append(row(f"  ${monthly_spend:.2f} / ${budget:.2f}  {bar}  ${remaining:.2f} left"))
        lines.append(HR)

    return "\n".join(lines)


@mcp.tool()
async def llm_cache_stats() -> str:
    """Show prompt classification cache statistics — hit rate, entries, memory usage.

    The cache stores ClassificationResult objects keyed by SHA-256(prompt + quality_mode + min_model).
    Budget pressure is always applied fresh, so cached classifications stay valid.
    """
    cache = get_cache()
    stats = await cache.get_stats()

    hit_rate = stats["hit_rate"]
    lines = [
        "## Classification Cache",
        "",
        f"Entries:     {stats['entries']} / {stats['max_entries']}",
        f"TTL:         {stats['ttl_seconds']}s",
        f"Hit rate:    {hit_rate} ({stats['hits']} hits, {stats['misses']} misses)",
        f"Evictions:   {stats['evictions']}",
        f"Memory:      ~{stats['memory_estimate_kb']} KB",
    ]
    if stats["entries"] > 0:
        lines.append(f"Oldest:      {stats['oldest_entry_age_hours']:.2f}h ago")

    return "\n".join(lines)


@mcp.tool()
async def llm_cache_clear() -> str:
    """Clear the prompt classification cache."""
    cache = get_cache()
    count = await cache.clear()
    return f"Cleared {count} cached classification entries."


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
            lines.append(f"- **{colorize_provider(provider)}**: {status}")

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
        "gemini": "Imagen 3 (images), Veo 2 (video)",
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
        lines.append(f"- **{colorize_provider(provider)}** ({status}): {models}")

    lines.append("\n### Media Generation")
    for provider, models in media_providers.items():
        status = "configured" if provider in available else "not configured"
        lines.append(f"- **{colorize_provider(provider)}** ({status}): {models}")

    configured = len(available)
    total = len(set(text_providers) | set(media_providers))
    lines.append(f"\n{configured}/{total} providers configured")
    return "\n".join(lines)


# ── Subscription Usage (Live) ─────────────────────────────────────────────────


# Cache to avoid hammering claude.ai on every call
_last_usage: ClaudeSubscriptionUsage | None = None


@mcp.tool()
async def llm_check_usage() -> str:
    """Check real-time Claude subscription usage (session limits, weekly limits, extra spend).

    Shows cached data if available. If no data cached, returns the JS snippet
    to run via Playwright's browser_evaluate (one call, no page navigation needed).

    The budget pressure from this data feeds directly into model routing —
    higher usage = more aggressive downshifting to cheaper models.
    """
    if _last_usage:
        return _last_usage.summary()

    return (
        "No cached usage data yet.\n\n"
        "**Quick setup** (one Playwright call):\n"
        "1. `browser_navigate` to `https://claude.ai` (any page, just need auth cookies)\n"
        "2. `browser_evaluate` with this JS:\n"
        f"```js\n{FETCH_USAGE_JS}\n```\n"
        "3. Pass the result to `llm_update_usage`\n\n"
        "This fetches the same JSON API that claude.ai's settings page uses."
    )


@mcp.tool()
async def llm_update_usage(data: dict) -> str:
    """Update cached Claude usage from the JSON API response.

    Call this with the result from browser_evaluate(FETCH_USAGE_JS).
    Accepts the full JSON object from the claude.ai internal API.

    The cached data is used by llm_classify for real budget pressure
    instead of token-based estimates.

    Args:
        data: JSON response from the claude.ai usage API (via browser_evaluate).
    """
    import os
    import time

    global _last_usage
    _last_usage = parse_api_response(data)

    # Write refresh timestamp for the usage-refresh hook
    state_dir = os.path.expanduser("~/.llm-router")
    os.makedirs(state_dir, exist_ok=True)
    state_file = os.path.join(state_dir, "usage_last_refresh.txt")
    with open(state_file, "w") as f:
        f.write(str(time.time()))

    return _last_usage.summary()


# ── Codex Local Agent ─────────────────────────────────────────────────────────


@mcp.tool()
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


# ── API Setup & Discovery ────────────────────────────────────────────────────


# Provider registry — signup URLs, free tier info, and capabilities
_PROVIDER_REGISTRY: dict[str, dict] = {
    "gemini": {
        "name": "Google Gemini",
        "signup_url": "https://aistudio.google.com/apikey",
        "env_var": "GEMINI_API_KEY",
        "free_tier": "Yes — generous free tier (1500 req/day for Flash)",
        "capabilities": ["text", "code", "images (Imagen 3)", "video (Veo 2)"],
        "pricing": "Free tier available, then pay-as-you-go",
        "recommended": True,
    },
    "groq": {
        "name": "Groq",
        "signup_url": "https://console.groq.com/keys",
        "env_var": "GROQ_API_KEY",
        "free_tier": "Yes — fast inference, free tier available",
        "capabilities": ["text", "code"],
        "pricing": "Free tier with rate limits, then pay-as-you-go",
        "recommended": True,
    },
    "openai": {
        "name": "OpenAI",
        "signup_url": "https://platform.openai.com/api-keys",
        "env_var": "OPENAI_API_KEY",
        "free_tier": "No — $5 free credit for new accounts",
        "capabilities": ["text", "code", "images (DALL-E)", "audio (TTS/Whisper)"],
        "pricing": "Pay-as-you-go, starts at ~$0.15/1M tokens (GPT-4o-mini)",
        "recommended": True,
    },
    "deepseek": {
        "name": "DeepSeek",
        "signup_url": "https://platform.deepseek.com/api_keys",
        "env_var": "DEEPSEEK_API_KEY",
        "free_tier": "No — but extremely cheap ($0.14/1M input tokens)",
        "capabilities": ["text", "code", "reasoning"],
        "pricing": "Pay-as-you-go, cheapest for quality ratio",
        "recommended": True,
    },
    "perplexity": {
        "name": "Perplexity",
        "signup_url": "https://www.perplexity.ai/settings/api",
        "env_var": "PERPLEXITY_API_KEY",
        "free_tier": "No — API requires credits",
        "capabilities": ["search-augmented research"],
        "pricing": "Pay-as-you-go, ~$1/1000 searches (Sonar)",
        "recommended": False,
    },
    "anthropic": {
        "name": "Anthropic",
        "signup_url": "https://console.anthropic.com/settings/keys",
        "env_var": "ANTHROPIC_API_KEY",
        "free_tier": "No — $5 free credit for new accounts",
        "capabilities": ["text", "code", "analysis"],
        "pricing": "Pay-as-you-go, ~$3/1M tokens (Sonnet)",
        "recommended": False,
    },
    "mistral": {
        "name": "Mistral AI",
        "signup_url": "https://console.mistral.ai/api-keys",
        "env_var": "MISTRAL_API_KEY",
        "free_tier": "No — pay-as-you-go",
        "capabilities": ["text", "code"],
        "pricing": "Pay-as-you-go, ~$0.15/1M tokens (Small)",
        "recommended": False,
    },
    "together": {
        "name": "Together AI",
        "signup_url": "https://api.together.xyz/settings/api-keys",
        "env_var": "TOGETHER_API_KEY",
        "free_tier": "Yes — $5 free credit",
        "capabilities": ["text", "code", "open-source models"],
        "pricing": "Pay-as-you-go, cheap open-source hosting",
        "recommended": False,
    },
    "xai": {
        "name": "xAI (Grok)",
        "signup_url": "https://console.x.ai/",
        "env_var": "XAI_API_KEY",
        "free_tier": "No",
        "capabilities": ["text", "code"],
        "pricing": "Pay-as-you-go",
        "recommended": False,
    },
    "cohere": {
        "name": "Cohere",
        "signup_url": "https://dashboard.cohere.com/api-keys",
        "env_var": "COHERE_API_KEY",
        "free_tier": "Yes — free trial tier",
        "capabilities": ["text", "RAG"],
        "pricing": "Free trial, then pay-as-you-go",
        "recommended": False,
    },
    "fal": {
        "name": "fal.ai",
        "signup_url": "https://fal.ai/dashboard/keys",
        "env_var": "FAL_KEY",
        "free_tier": "Yes — limited free credits",
        "capabilities": ["images (Flux)", "video (Kling, minimax)"],
        "pricing": "Pay-per-generation, ~$0.01-0.10/image",
        "recommended": False,
    },
    "stability": {
        "name": "Stability AI",
        "signup_url": "https://platform.stability.ai/account/keys",
        "env_var": "STABILITY_API_KEY",
        "free_tier": "Yes — 25 free credits",
        "capabilities": ["images (Stable Diffusion 3)"],
        "pricing": "Credit-based, ~$0.02-0.06/image",
        "recommended": False,
    },
    "elevenlabs": {
        "name": "ElevenLabs",
        "signup_url": "https://elevenlabs.io/app/settings/api-keys",
        "env_var": "ELEVENLABS_API_KEY",
        "free_tier": "Yes — 10k characters/month free",
        "capabilities": ["voice synthesis", "voice cloning"],
        "pricing": "Free tier, then $5/mo+",
        "recommended": False,
    },
    "runway": {
        "name": "Runway",
        "signup_url": "https://dev.runwayml.com/",
        "env_var": "RUNWAY_API_KEY",
        "free_tier": "No — credit-based",
        "capabilities": ["video generation (Gen-3)"],
        "pricing": "Credit-based, ~$0.05/sec of video",
        "recommended": False,
    },
    "replicate": {
        "name": "Replicate",
        "signup_url": "https://replicate.com/account/api-tokens",
        "env_var": "REPLICATE_API_TOKEN",
        "free_tier": "No — pay-per-prediction",
        "capabilities": ["various open-source models"],
        "pricing": "Pay-per-prediction, varies by model",
        "recommended": False,
    },
}


@mcp.tool()
async def llm_setup(
    action: str = "status",
    provider: str | None = None,
    api_key: str | None = None,
) -> str:
    """Set up and manage API providers — discover keys, add new providers, check status.

    Actions:
    - "status": Show which providers are configured and which are missing
    - "guide": Step-by-step guide to add recommended free/cheap providers
    - "discover": Scan for existing API keys in environment (safe, read-only)
    - "add": Add an API key for a provider (writes to .env file securely)
    - "test": Validate API keys with a minimal call (tests configured or specific provider)
    - "provider": Show details about a specific provider

    Args:
        action: What to do — "status", "guide", "discover", "add", "test", or "provider".
        provider: Provider name (for "add", "test", and "provider" actions).
        api_key: API key value (for "add" action only). Key is validated before saving.
    """
    if action == "status":
        return _setup_status()
    elif action == "guide":
        return _setup_guide()
    elif action == "discover":
        return await _setup_discover()
    elif action == "test":
        return await _setup_test(provider)
    elif action == "add":
        if not provider:
            return "Specify a provider name. Run `llm_setup(action='status')` to see available providers."
        if not api_key:
            reg = _PROVIDER_REGISTRY.get(provider)
            if reg:
                return (
                    f"To add **{reg['name']}**:\n"
                    f"1. Sign up at: {reg['signup_url']}\n"
                    f"2. Copy your API key\n"
                    f"3. Run: `llm_setup(action='add', provider='{provider}', api_key='your-key-here')`\n\n"
                    f"Free tier: {reg['free_tier']}"
                )
            return f"Unknown provider: {provider}. Run `llm_setup(action='status')` for the list."
        return _setup_add(provider, api_key)
    elif action == "provider":
        return _setup_provider_detail(provider)
    else:
        return f"Unknown action: {action}. Use: status, guide, discover, add, test, or provider."


def _setup_status() -> str:
    """Show current provider configuration status."""
    config = get_config()
    configured = config.available_providers
    lines = ["# API Provider Status\n"]

    # Configured providers
    if configured:
        lines.append(f"## Configured ({len(configured)})")
        for name in sorted(configured):
            reg = _PROVIDER_REGISTRY.get(name, {})
            caps = ", ".join(reg.get("capabilities", ["unknown"]))
            lines.append(f"- **{name}**: {caps}")
        lines.append("")

    # Missing providers
    missing = set(_PROVIDER_REGISTRY.keys()) - configured
    if missing:
        # Show recommended first
        recommended = [p for p in sorted(missing) if _PROVIDER_REGISTRY[p].get("recommended")]
        others = [p for p in sorted(missing) if not _PROVIDER_REGISTRY[p].get("recommended")]

        if recommended:
            lines.append("## Recommended to Add")
            for name in recommended:
                reg = _PROVIDER_REGISTRY[name]
                lines.append(f"- **{name}**: {reg['free_tier']} — {reg['signup_url']}")
            lines.append("")

        if others:
            lines.append(f"## Other Available ({len(others)})")
            for name in others:
                reg = _PROVIDER_REGISTRY[name]
                lines.append(f"- {name}: {reg['signup_url']}")
            lines.append("")

    lines.append(f"**Total: {len(configured)}/{len(_PROVIDER_REGISTRY)} providers configured**")
    lines.append("\nRun `llm_setup(action='guide')` for step-by-step setup.")
    return "\n".join(lines)


def _setup_guide() -> str:
    """Step-by-step guide to get started with the cheapest/free providers."""
    return """# Quick Start Guide — Get Running in 5 Minutes

## Step 1: Gemini (FREE — best starting point)
1. Go to https://aistudio.google.com/apikey
2. Click "Create API Key" (Google account required)
3. Copy the key
4. Run: `llm_setup(action='add', provider='gemini', api_key='your-key')`
5. You now have: text, code, images (Imagen 3), and video (Veo 2)!

## Step 2: Groq (FREE — ultra-fast inference)
1. Go to https://console.groq.com/keys
2. Sign up and create an API key
3. Run: `llm_setup(action='add', provider='groq', api_key='your-key')`
4. Adds: blazing fast Llama 3.3 for classification and simple tasks

## Step 3: DeepSeek (CHEAP — best quality/price)
1. Go to https://platform.deepseek.com/api_keys
2. Sign up and add $5 credit (lasts weeks of heavy use)
3. Run: `llm_setup(action='add', provider='deepseek', api_key='your-key')`
4. Adds: excellent coding and reasoning at 1/20th the cost of GPT-4o

## Step 4 (Optional): OpenAI
1. Go to https://platform.openai.com/api-keys
2. Add billing ($5 minimum)
3. Run: `llm_setup(action='add', provider='openai', api_key='your-key')`
4. Adds: GPT-4o, o3, DALL-E 3, TTS, Whisper

## After Setup
- Run `llm_setup(action='status')` to see what's configured
- Run `llm_setup(action='discover')` to find keys already on your machine
- Use `llm_health()` to verify all providers are working

## Budget Protection
Set per-provider monthly limits:
```
LLM_ROUTER_BUDGET_OPENAI=10.00
LLM_ROUTER_BUDGET_GEMINI=5.00
LLM_ROUTER_MONTHLY_BUDGET=20.00
```

## Security Notes
- Keys are stored in `.env` (local only, never committed to git)
- `.env` should be in `.gitignore` (the router checks this)
- Keys are loaded into environment variables at runtime only
- No keys are ever logged or sent to third parties
"""


async def _setup_discover() -> str:
    """Scan for existing API keys in environment and common config files."""
    import os
    from pathlib import Path

    lines = ["# API Key Discovery\n"]
    lines.append("Scanning for existing API keys on your machine...\n")

    found: list[tuple[str, str, str]] = []  # (provider, source, masked_key)

    # 1. Check current environment variables
    for provider, reg in _PROVIDER_REGISTRY.items():
        env_var = reg["env_var"]
        val = os.environ.get(env_var, "")
        if val:
            masked = _mask_key(val)
            found.append((provider, f"env: ${env_var}", masked))

    # 2. Check common .env file locations (read-only, no writes)
    env_paths = [
        Path.home() / ".env",
        Path.cwd() / ".env",
        Path.home() / ".config" / "llm-router" / ".env",
    ]
    for env_path in env_paths:
        if env_path.exists():
            try:
                content = env_path.read_text()
                for provider, reg in _PROVIDER_REGISTRY.items():
                    env_var = reg["env_var"]
                    for line in content.splitlines():
                        stripped = line.strip()
                        if stripped.startswith(env_var + "="):
                            val = stripped.split("=", 1)[1].strip().strip("'\"")
                            if val:
                                masked = _mask_key(val)
                                source = f"file: {env_path}"
                                # Avoid duplicates
                                if not any(p == provider and s == source for p, s, _ in found):
                                    found.append((provider, source, masked))
            except PermissionError:
                pass

    if found:
        lines.append(f"## Found {len(found)} API Key(s)\n")
        for provider, source, masked in found:
            status = "configured" if provider in get_config().available_providers else "found but not loaded"
            lines.append(f"- **{provider}** ({status}): `{masked}` — {source}")
        lines.append("")
        lines.append("Keys marked 'found but not loaded' exist on your machine but aren't in the router's .env file.")
        lines.append("Run `llm_setup(action='add', provider='<name>', api_key='<key>')` to add them.")
    else:
        lines.append("No existing API keys found in environment or common config files.")
        lines.append("\nRun `llm_setup(action='guide')` for setup instructions.")

    lines.append("\n## Security")
    lines.append("- This scan only checked environment variables and .env files")
    lines.append("- No keys were transmitted — all checks are local")
    lines.append("- Keys are masked in this output for safety")

    return "\n".join(lines)


def _mask_key(key: str) -> str:
    """Mask an API key for safe display — show only first 4 and last 4 chars."""
    if len(key) <= 12:
        return key[:3] + "***" + key[-2:]
    return key[:4] + "***" + key[-4:]


def _setup_add(provider: str, api_key: str) -> str:
    """Add an API key to the .env file securely."""
    from pathlib import Path

    reg = _PROVIDER_REGISTRY.get(provider)
    if not reg:
        return f"Unknown provider: {provider}. Run `llm_setup(action='status')` for the list."

    env_var = reg["env_var"]
    api_key = api_key.strip()

    # Basic key format validation
    if len(api_key) < 10:
        return f"API key seems too short ({len(api_key)} chars). Please check and try again."
    if " " in api_key or "\n" in api_key:
        return "API key contains whitespace. Please check and try again."

    # Find the .env file
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        # Try project root
        env_path = Path(__file__).parent.parent.parent / ".env"

    if env_path.exists():
        content = env_path.read_text()
        # Check if key already exists
        new_lines = []
        replaced = False
        for line in content.splitlines():
            if line.strip().startswith(env_var + "="):
                new_lines.append(f"{env_var}={api_key}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{env_var}={api_key}")
        env_path.write_text("\n".join(new_lines) + "\n")
    else:
        env_path.write_text(f"{env_var}={api_key}\n")

    # Verify .gitignore protection
    gitignore_warning = ""
    gitignore_path = env_path.parent / ".gitignore"
    if gitignore_path.exists():
        gitignore_content = gitignore_path.read_text()
        if ".env" not in gitignore_content:
            gitignore_warning = "\n\n**WARNING**: `.env` is NOT in `.gitignore` — add it to prevent leaking keys!"
    else:
        gitignore_warning = "\n\n**WARNING**: No `.gitignore` found — create one with `.env` to prevent leaking keys!"

    # Reload config to pick up new key
    import os
    import llm_router.config as _cfg
    os.environ[env_var] = api_key
    _cfg._config = None  # Force reload on next get_config()

    masked = _mask_key(api_key)
    return (
        f"Added **{reg['name']}** (`{masked}`) to `{env_path}`\n\n"
        f"Run `llm_health()` to verify the key works."
        f"{gitignore_warning}"
    )


# Minimal test models per provider — cheapest/fastest for key validation
_TEST_MODELS: dict[str, str] = {
    "openai": "openai/gpt-4o-mini",
    "gemini": "gemini/gemini-2.5-flash-lite",
    "groq": "groq/llama-3.3-70b-versatile",
    "deepseek": "deepseek/deepseek-chat",
    "mistral": "mistral/mistral-small-latest",
    "perplexity": "perplexity/sonar",
    "anthropic": "anthropic/claude-haiku-4-5-20251001",
    "together": "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "xai": "xai/grok-2-latest",
    "cohere": "cohere/command-r",
}


async def _setup_test(provider: str | None) -> str:
    """Validate API key(s) with a minimal LLM call (~$0.0001 each)."""
    config = get_config()
    configured = config.available_providers

    if provider:
        # Test a specific provider
        if provider not in _TEST_MODELS:
            return f"No test model configured for '{provider}'. Testable: {', '.join(sorted(_TEST_MODELS))}"
        if provider not in configured:
            return f"Provider '{provider}' is not configured. Add a key first: `llm_setup(action='add', provider='{provider}')`"
        providers_to_test = [provider]
    else:
        # Test all configured text providers
        providers_to_test = [p for p in sorted(configured) if p in _TEST_MODELS]
        if not providers_to_test:
            return "No testable text providers configured. Run `llm_setup(action='status')` to see available providers."

    results: list[str] = ["## API Key Validation\n"]
    test_prompt = "Reply with exactly: OK"
    test_messages = [{"role": "user", "content": test_prompt}]

    for p in providers_to_test:
        model = _TEST_MODELS[p]
        try:
            resp = await providers.call_llm(
                model=model, messages=test_messages, temperature=0, max_tokens=5,
            )
            results.append(f"- **{p}**: Valid ({model}, ${resp.cost_usd:.6f}, {resp.latency_ms:.0f}ms)")
        except Exception as e:
            err_str = str(e)
            if "auth" in err_str.lower() or "api key" in err_str.lower() or "invalid" in err_str.lower():
                results.append(f"- **{p}**: INVALID KEY ({e})")
            elif "rate" in err_str.lower() or "429" in err_str:
                results.append(f"- **{p}**: Valid (rate-limited, key works but quota exceeded)")
            else:
                results.append(f"- **{p}**: ERROR ({e})")

    return "\n".join(results)


def _setup_provider_detail(provider: str | None) -> str:
    """Show detailed info about a specific provider."""
    if not provider:
        return "Specify a provider name. Example: `llm_setup(action='provider', provider='gemini')`"

    reg = _PROVIDER_REGISTRY.get(provider)
    if not reg:
        available = ", ".join(sorted(_PROVIDER_REGISTRY.keys()))
        return f"Unknown provider: {provider}. Available: {available}"

    config = get_config()
    is_configured = provider in config.available_providers

    lines = [
        f"# {reg['name']}",
        "",
        f"**Status**: {'Configured' if is_configured else 'Not configured'}",
        f"**Free tier**: {reg['free_tier']}",
        f"**Pricing**: {reg['pricing']}",
        f"**Capabilities**: {', '.join(reg['capabilities'])}",
        f"**Sign up**: {reg['signup_url']}",
        f"**Env var**: `{reg['env_var']}`",
    ]

    if not is_configured:
        lines.extend([
            "",
            "## How to Add",
            f"1. Go to {reg['signup_url']}",
            "2. Create an account and generate an API key",
            f"3. Run: `llm_setup(action='add', provider='{provider}', api_key='your-key')`",
        ])

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
