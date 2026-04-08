"""Smart router tools — llm_classify, llm_track_usage, llm_route, llm_stream."""

from __future__ import annotations

import os

from mcp.server.fastmcp import Context

from llm_router.classifier import classify_complexity
from llm_router.config import get_config
from llm_router.cost import (
    get_daily_claude_breakdown, get_daily_claude_tokens,
    get_savings_summary, log_claude_usage,
)
from llm_router.model_selector import select_model
from llm_router.profiles import complexity_to_profile
from llm_router.provider_budget import get_provider_budgets, rank_external_models
from llm_router.router import route_and_call
from llm_router.types import (
    ClassificationResult, Complexity, QualityMode,
    RoutingProfile, RoutingRecommendation, TaskType, _budget_bar,
)
from llm_router import state as _state


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
    _last_usage = _state.get_last_usage()
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

    # Explainable routing: "why not Opus/Sonnet?" cost comparison
    # Always shown — this is the core of v2.2 explainability.
    _COST_PER_1K_OUT = {
        "opus":   0.075,
        "sonnet": 0.015,
        "haiku":  0.00125,
    }
    chosen_tier = rec.recommended_model  # "haiku" | "sonnet" | "opus"
    chosen_cost = _COST_PER_1K_OUT.get(chosen_tier, 0.015)
    lines.append(HR)
    lines.append(row("  Why not a more expensive model?"))
    for tier, cost in [("opus", 0.075), ("sonnet", 0.015), ("haiku", 0.00125)]:
        if tier == chosen_tier:
            marker = "✓ chosen"
        elif cost > chosen_cost:
            ratio = cost / chosen_cost
            marker = f"↑ {ratio:.0f}x more expensive — unnecessary for {classification.complexity.value} task"
        else:
            marker = "↓ cheaper option exists"
        lines.append(row(f"    {tier:<8} ${cost:.5f}/1k  {marker}"))

    # Extra tip when LLM_ROUTER_EXPLAIN=1 is not set
    if os.getenv("LLM_ROUTER_EXPLAIN") != "1":
        lines.append(row("  Tip: set LLM_ROUTER_EXPLAIN=1 to see [→ model · task] on every response"))

    lines.append(HR)
    return "\n".join(lines)


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
        lines.append(f"   \U0001f4b0 Saved: **${total_saved:.4f}** | \u23f1\ufe0f Time saved: **{_state._format_time(total_time)}**")

    return "\n".join(lines)


async def llm_route(
    prompt: str,
    ctx: Context,
    task_type: str | None = None,
    complexity_override: str | None = None,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
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
        context: Optional conversation context to help the model understand the broader task.
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

    # Step 2: Resolve task type and profile — use select_model() so budget pressure
    # is applied consistently with llm_classify (single decision path, not two).
    resolved_task_type = (
        TaskType(task_type) if task_type
        else classification.inferred_task_type
        or TaskType.QUERY
    )

    budget_pct = 0.0
    _last_usage = _state.get_last_usage()
    if _last_usage and _last_usage.session:
        budget_pct = _last_usage.effective_pressure

    q_mode = get_config().quality_mode
    floor = get_config().min_model
    rec = select_model(classification, budget_pct, q_mode, floor)

    # When under budget pressure, respect the downshifted profile from select_model
    # rather than deriving profile purely from complexity (which ignores pressure).
    if budget_pct >= 0.85 and rec.was_downshifted:
        profile = complexity_to_profile(rec.classification.complexity)
        # Map recommended_model tier back to profile if it's lower
        _tier_to_profile = {"haiku": RoutingProfile.BUDGET, "sonnet": RoutingProfile.BALANCED, "opus": RoutingProfile.PREMIUM}
        profile = _tier_to_profile.get(rec.recommended_model, profile)
    else:
        profile = complexity_to_profile(classification.complexity)

    await ctx.info(
        f"Classified as {classification.complexity.value} "
        f"({classification.confidence:.0%}) → {profile.value} profile"
        + (f" [downshifted, budget={budget_pct:.0%}]" if rec.was_downshifted else "")
    )

    # Step 3: Build classification metadata for quality logging
    if complexity_override:
        _clf_type = "override"
    elif classification.classifier_model == "none":
        _clf_type = "fallback"
    elif classification.classifier_latency_ms == 0.0 and classification.confidence > 0:
        _clf_type = "cached"
    else:
        _clf_type = "llm"

    _classification_data = {
        "task_type": resolved_task_type.value,
        "profile": profile.value,
        "classifier_type": _clf_type,
        "classifier_model": classification.classifier_model,
        "classifier_confidence": classification.confidence,
        "classifier_latency_ms": classification.classifier_latency_ms,
        "complexity": classification.complexity.value,
        "recommended_model": rec.recommended_model,
        "base_model": rec.base_model,
        "was_downshifted": rec.was_downshifted,
        "budget_pct_used": budget_pct,
        "quality_mode": q_mode.value if hasattr(q_mode, "value") else str(q_mode),
    }

    # Step 4: Route and call
    resp = await route_and_call(
        resolved_task_type, prompt,
        profile=profile,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        caller_context=context,
        ctx=ctx,
        classification_data=_classification_data,
    )

    # Step 5: Build response with classification + routing info
    total_cost = classification.classifier_cost_usd + resp.cost_usd
    lines = [
        classification.header(),
        resp.header(),
        f"> **Total cost: ${total_cost:.6f}**",
        "",
        resp.content,
    ]

    return "\n".join(lines)


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
    from llm_router.providers import call_llm_stream
    from llm_router.cost import log_usage

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


async def llm_select_agent(
    prompt: str,
    profile: str = "balanced",
) -> str:
    """Classify a task prompt and return the recommended agent CLI + model for session-level routing.

    Use this BEFORE starting a Claude Code / Codex / Gemini CLI session to pick the right
    agent runtime for the task. This is session-level routing — it selects which agent to
    invoke, not which model to call mid-session.

    Decision tree (profile × complexity):
      budget  + simple/moderate  → codex  + gpt-4o-mini
      budget  + complex          → codex  + gpt-4o (Codex handles most coding; escalate if needed)
      balanced + simple          → codex  + gpt-4o-mini
      balanced + moderate        → claude_code + sonnet
      balanced + complex         → claude_code + opus
      premium + any              → claude_code + opus

    Returns JSON with:
      primary          — agent binary name: "claude_code" | "codex" | "gemini_cli"
      primary_model    — model flag value (pass via -m or --model)
      fallback         — fallback agent if primary unavailable
      fallback_model   — model for fallback
      task_type        — classified task type (code / analyze / generate / research / query)
      complexity       — simple | moderate | complex
      confidence       — classifier confidence 0–1
      reason           — one-line classification rationale
      env_check        — dict of required env vars and whether they're set

    Args:
        prompt: The task description to classify (same text you'd pass to the agent).
        profile: Routing profile — "budget", "balanced", or "premium" (default: "balanced").
    """
    import json as _json
    import shutil as _shutil
    import os as _os

    from llm_router.codex_agent import is_codex_available

    valid_profiles = {"budget", "balanced", "premium"}
    if profile not in valid_profiles:
        profile = "balanced"

    try:
        classification = await classify_complexity(prompt)
        task_type_val = classification.inferred_task_type or TaskType.CODE
        complexity_val = classification.complexity
        confidence = classification.confidence
        reason = classification.reasoning or "heuristic"
    except Exception:
        task_type_val = TaskType.CODE
        complexity_val = Complexity.MODERATE
        confidence = 0.0
        reason = "classification failed — defaulted to moderate code task"

    task_type_str = task_type_val.value if hasattr(task_type_val, "value") else str(task_type_val)
    complexity_str = complexity_val.value if hasattr(complexity_val, "value") else str(complexity_val)

    # Decision tree: profile × complexity → (agent, model)
    _AGENT_MAP = {
        ("budget",   "simple"):   ("codex",       "gpt-4o-mini",            "claude_code", "claude-sonnet-4-6"),
        ("budget",   "moderate"): ("codex",       "gpt-4o-mini",            "claude_code", "claude-sonnet-4-6"),
        ("budget",   "complex"):  ("codex",       "gpt-4o",                 "claude_code", "claude-sonnet-4-6"),
        ("balanced", "simple"):   ("codex",       "gpt-4o-mini",            "claude_code", "claude-sonnet-4-6"),
        ("balanced", "moderate"): ("claude_code", "claude-sonnet-4-6",      "codex",       "gpt-4o"),
        ("balanced", "complex"):  ("claude_code", "claude-opus-4-6",        "codex",       "gpt-4o"),
        ("premium",  "simple"):   ("claude_code", "claude-sonnet-4-6",      "codex",       "gpt-4o-mini"),
        ("premium",  "moderate"): ("claude_code", "claude-opus-4-6",        "codex",       "gpt-4o"),
        ("premium",  "complex"):  ("claude_code", "claude-opus-4-6",        "codex",       "gpt-4o"),
    }

    key = (profile, complexity_str)
    primary, primary_model, fallback, fallback_model = _AGENT_MAP.get(
        key, ("claude_code", "claude-sonnet-4-6", "codex", "gpt-4o")
    )

    # Override: research tasks → always claude_code (needs web access via Perplexity)
    if task_type_str == "research":
        primary, primary_model = "claude_code", "claude-sonnet-4-6"
        fallback, fallback_model = "codex", "gpt-4o"

    # Environment check
    codex_ok = is_codex_available()
    claude_ok = bool(_shutil.which("claude"))
    openai_key = bool(_os.environ.get("OPENAI_API_KEY"))
    gemini_key = bool(_os.environ.get("GEMINI_API_KEY"))

    env_check = {
        "claude_code_binary": claude_ok,
        "codex_binary": codex_ok,
        "OPENAI_API_KEY": openai_key,
        "GEMINI_API_KEY": gemini_key,
    }

    # Fallback if primary unavailable
    primary_available = (primary == "claude_code" and claude_ok) or (primary == "codex" and codex_ok)
    if not primary_available and primary != fallback:
        primary, primary_model, fallback, fallback_model = fallback, fallback_model, primary, primary_model

    result = {
        "primary": primary,
        "primary_model": primary_model,
        "fallback": fallback,
        "fallback_model": fallback_model,
        "task_type": task_type_str,
        "complexity": complexity_str,
        "confidence": round(confidence, 2),
        "reason": reason,
        "env_check": env_check,
    }

    # CLI usage hint
    if primary == "claude_code":
        invocation = f'claude --model {primary_model} -p "{prompt[:60]}..."'
    else:
        invocation = f'codex exec --model {primary_model} "{prompt[:60]}..."'

    return (
        f"**Session-level routing recommendation** (profile={profile})\n\n"
        f"```json\n{_json.dumps(result, indent=2)}\n```\n\n"
        f"**Suggested invocation:**\n```bash\n{invocation}\n```\n\n"
        f"*Classification: {task_type_str}/{complexity_str} · confidence={confidence:.0%} · {reason}*"
    )


def register(mcp) -> None:
    """Register smart router tools with the FastMCP instance."""
    mcp.tool()(llm_classify)
    mcp.tool()(llm_track_usage)
    mcp.tool()(llm_route)
    mcp.tool()(llm_stream)
    mcp.tool()(llm_select_agent)
