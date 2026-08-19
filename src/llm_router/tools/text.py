"""Text LLM tools — llm_query, llm_research, llm_generate, llm_analyze, llm_reason, llm_code, llm_edit."""

from __future__ import annotations

import asyncio
import os

from mcp.server.mcpserver import Context

from llm_router.config import get_config
from llm_router.cost import log_compression_stat
from llm_router.response_router import route_response
from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType


async def _announce_routing(ctx: Context, task_type: str, complexity: str) -> None:
    """Fire an immediate notification so users see activity within ~1s of tool call.

    Claude Code shows a "Calling llm_router..." spinner but gives no further feedback
    until the tool returns. This fires before any routing work begins so the user
    knows which task/complexity pair was received, and that routing is starting.
    """
    try:
        await ctx.info(f"⚡ llm_router routing {task_type}/{complexity}...")
        await ctx.report_progress(0, 100, f"routing {task_type}/{complexity}")
    except Exception:
        pass


def _read_hook_complexity_hint(max_age_sec: float = 120.0) -> str | None:
    """Return the complexity classified by the auto-route hook, or None.

    The hook writes ``~/.llm-router/last_classification_<session_id>.json``
    on every UserPromptSubmit. MCP tools read it here so the hook's
    verdict survives the boundary into the MCP server — without this,
    the router falls back to a length heuristic on the wrapped prompt
    (which has grown past the short-prompt boundary) and routes
    everything to the moderate tier even when the user's prompt was
    obviously simple.

    Session isolation (INV-007 / ROU-001):

    * The reader picks the per-session file using ``CLAUDE_SESSION_ID``
      from the environment — Claude Code sets this when spawning the
      MCP server, so each MCP process is naturally pinned to its own
      session. Two concurrent Claude Code sessions write and read
      independent shards; neither sees the other's verdict.
    * If ``CLAUDE_SESSION_ID`` is missing (manual MCP invocation,
      test harness, etc.) the reader returns ``None`` — the router
      falls back to the length heuristic, which is the correct
      conservative default.
    * The pre-INV-007 shared ``last_classification.json`` is no longer
      consulted. It allowed any same-user process to forge a
      classification for any session within the 120s freshness window.

    Stale entries (older than ``max_age_sec``) are still ignored: a
    second user prompt arriving before the previous tool call finishes
    must let the fresh classification win.
    """
    import json
    import os
    from pathlib import Path
    import time

    session_id = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if not session_id:
        return None

    path = Path.home() / ".llm-router" / f"last_classification_{session_id}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # Belt-and-braces: even if the file is per-session-named, verify the
    # session_id inside matches the env. A forged file at the right path
    # would also have to forge the inner session_id, and a forger that
    # already knows the target session id has bypassed the file system
    # gate entirely — so this is a cheap consistency check, not a
    # security boundary.
    inner_sid = data.get("session_id")
    if isinstance(inner_sid, str) and inner_sid and inner_sid != session_id:
        return None

    issued_at = data.get("issued_at")
    if not isinstance(issued_at, (int, float)):
        return None
    if time.time() - issued_at > max_age_sec:
        return None
    complexity = data.get("complexity")
    if complexity not in ("simple", "moderate", "complex", "deep_reasoning"):
        return None
    return complexity


def _read_hook_route_directive(max_age_sec: float = 120.0) -> str | None:
    """Return the route_id the auto-route hook minted for this turn, or None.

    Phase 0.5 (Option A, sidecar directive-id bridge): the hook now writes a
    ``route_id`` into the SAME ``last_classification_<session_id>.json``
    sidecar that :func:`_read_hook_complexity_hint` already reads, minted
    identically to (and shared with) ``pending['route_id']`` — the id
    ``enforce-route.py``'s ``_record_realization_used`` uses when it writes
    the adoption row. Doors that pass this value through as
    ``route_directive_id`` to ``route_and_call`` let the execution ledger's
    billable rows land under that SAME id, so its route_id join against the
    adoption row actually fires instead of comparing two independently
    minted ids that never matched.

    Reuses the identical guarded-read gates as ``_read_hook_complexity_hint``
    (same file, same session-match check, same freshness window) rather than
    performing a second, unguarded read — a stale or session-mismatched
    sidecar must not leak a directive id into an unrelated turn any more than
    it may leak a complexity hint. Returns ``None`` on any miss (missing
    file, session mismatch, stale, absent/non-string route_id) — the caller
    falls back to ``route_directive_id=None``, i.e. today's behavior
    (billable rows keyed on ``correlation_id``, unchanged).
    """
    import json
    import os
    from pathlib import Path
    import time

    session_id = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if not session_id:
        return None

    path = Path.home() / ".llm-router" / f"last_classification_{session_id}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    inner_sid = data.get("session_id")
    if isinstance(inner_sid, str) and inner_sid and inner_sid != session_id:
        return None

    issued_at = data.get("issued_at")
    if not isinstance(issued_at, (int, float)):
        return None
    if time.time() - issued_at > max_age_sec:
        return None

    route_id = data.get("route_id")
    if not isinstance(route_id, str) or not route_id:
        return None
    return route_id


def _effective_complexity(caller_hint: str | None, floor: str | None = None) -> str | None:
    """Pick the best complexity hint: caller arg > hook verdict > floor.

    Each MCP tool has its own ``complexity`` keyword. If the caller
    passed one explicitly, that wins (they know what they want). If
    they didn't, fall back to the hook's last classification. If neither
    exists, use ``floor`` (e.g. ``llm_analyze`` floors at ``moderate``
    because pure-simple analysis isn't a real task).
    """
    if caller_hint:
        return caller_hint
    hook_hint = _read_hook_complexity_hint()
    if hook_hint:
        return hook_hint
    return floor


async def _apply_response_router(formatted: str) -> str:
    """Pipe the MCP tool's response through llm_router.response_router.

    The response router parses the text into critical sections (code
    blocks, file paths, tool invocations, headers) and explanation
    paragraphs, then routes the explanations through a cheaper model.
    Critical sections are preserved verbatim so codegen / instructions
    don't get rewritten in lossy ways.

    Two reasons this is a hot saver:

    * The MCP tool's response goes into Claude (Opus 4.7) for further
      reasoning. Every token in that response is a token Opus has to
      *process* — shrinking explanations directly reduces Opus's
      context-window cost.
    * Repeated turns compound: a 50% smaller llm_query reply means 50%
      fewer Opus tokens to read every subsequent turn until compaction.

    Disabled by default? No — ``LLM_ROUTER_RESPONSE_ROUTER`` defaults to
    "on". MIN_TOKENS (default 300) gates the optimisation so short
    responses skip the overhead. Failure-safe: ``route_response``
    returns the original string on any internal error.
    """
    if not formatted:
        return formatted
    try:
        return await route_response(formatted)
    except Exception:
        # Never let response-routing failures mask the underlying tool
        # response — the user is owed an answer even if the optimiser
        # falls over.
        return formatted


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
# Explainability (v8.2.0) — routing rationale on every response.
# Controlled by LLM_ROUTER_EXPLAIN config: "footer" (default), "header",
# "verbose", "off". Legacy LLM_ROUTER_EXPLAIN=1 maps to "header".
# ---------------------------------------------------------------------------

#: Cost-per-1k-output-tokens, for the "vs Opus" comparison in the footer.
#
# WP-03: derived from llm_router.pricing instead of hand-listed. The old literals
# were not even internally consistent about what they measured — Opus 0.075 and
# gpt-4o 0.010 are output rates, o3's 0.040 matched neither its input nor its
# output rate, and the two Gemini entries were an order of magnitude below the
# published rate. Normalising them all to output/1000 is the point; several
# displayed figures move because several were wrong.
_MODEL_IDS = (
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-haiku-4-5",
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.5-pro",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o3",
    "groq/llama-3.3-70b-versatile",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "mistral/mistral-large-latest",
    "xai/grok-3",
)
from llm_router import pricing as _pricing  # noqa: E402

_COST_PER_1K = {
    _mid: _v for _mid in _MODEL_IDS if (_v := _pricing.output_per_1k(_mid)) is not None
}
# Family-based so the host (Opus) baseline survives version bumps even if the
# exact id in _COST_PER_1K changes (e.g. opus-4-6 -> opus-4-8 -> opus-5).
from llm_router.model_aliases import family_lookup  # noqa: E402
_HOST_COST = family_lookup(
    _COST_PER_1K, "anthropic/claude-opus", _pricing.output_per_1k("opus") or 0.0
)


def _get_explain_mode() -> str:
    """Resolve explainability mode from env/config."""
    # Legacy compat: LLM_ROUTER_EXPLAIN=1 → "header" (old behavior)
    legacy = os.getenv("LLM_ROUTER_EXPLAIN", "")
    if legacy == "1":
        return "header"
    if legacy.lower() in ("off", "header", "footer", "verbose"):
        return legacy.lower()
    try:
        from llm_router.config import get_config

        return getattr(get_config(), "llm_router_explain", "footer")
    except Exception:
        return "footer"


def _savings_info(resp: LLMResponse) -> tuple[str, float]:
    """Calculate savings vs host (Opus) baseline. Returns (display_str, saved_usd)."""
    # WP-03: ask llm_router.pricing first. It already normalises "anthropic/x", "x"
    # and family aliases, so the three-step key hunt below is only needed for
    # ids the price table does not know at all. Resolving against the table's
    # literal keys was silently wrong once those keys moved: a model missing
    # from the dict fell through to _HOST_COST, i.e. "exactly as expensive as
    # Opus", and so reported no saving rather than an unknown one.
    actual_cost = _pricing.output_per_1k(resp.model)

    if actual_cost is None:
        model_key = resp.model if resp.model in _COST_PER_1K else None
        if model_key is None:
            # Try without provider prefix
            for k in _COST_PER_1K:
                if k.endswith("/" + resp.model) or k == resp.model:
                    model_key = k
                    break
        # family_lookup lets a new version (opus-4-8, opus-5, …) inherit its
        # family's cost instead of silently falling back to the host baseline.
        actual_cost = (
            family_lookup(_COST_PER_1K, model_key, _HOST_COST) if model_key else _HOST_COST
        )
    if actual_cost < _HOST_COST and actual_cost > 0:
        ratio = _HOST_COST / actual_cost
        saved = resp.cost_usd * (ratio - 1) / ratio if resp.cost_usd else 0.0
        return f"{ratio:.0f}x cheaper", saved
    return "", 0.0


def _routing_explanation(resp: LLMResponse, task: str) -> str:
    """Build routing explanation string based on configured mode.

    Always-on by default (footer mode). Returns empty string only when off.
    """
    mode = _get_explain_mode()
    if mode == "off":
        return ""

    # Semantic cache hit (v8.4.0) — special short-circuit footer
    if resp.cache_hit:
        cache_model = (
            resp.model.replace("cache/", "") if resp.model.startswith("cache/") else resp.model
        )
        sim_pct = f"{resp.cache_similarity:.0%}"
        if mode == "verbose":
            return f"\n─────\n→ Semantic cache hit ({sim_pct} match) · original model: {cache_model} · $0 · 0ms"
        compact = f"cache hit ({sim_pct}) · {cache_model} · $0"
        if mode == "header":
            return f"[→ {compact}]\n\n"
        return f"\n─────\n→ {compact}"

    model_short = resp.model.split("/")[-1] if resp.model else "unknown"
    savings_label, saved_usd = _savings_info(resp)
    cost_str = f"${resp.cost_usd:.5f}" if resp.cost_usd else "$0"

    # Context optimization stats (v8.3.0)
    ctx_info = ""
    try:
        from llm_router.context import get_last_optimization

        opt = get_last_optimization()
        if opt and opt.tokens_saved > 0:
            ctx_info = f" | ctx {opt.original_tokens}→{opt.compressed_tokens}tok ({opt.reduction_pct:.0f}% saved)"
    except Exception:
        pass

    if mode == "verbose":
        # Full breakdown with chain walk
        conf_str = f"{resp.confidence:.0%}" if resp.confidence > 0 else "n/a"
        method = resp.classification_method or "unknown"
        complexity = resp.complexity or "unknown"
        lines = [
            f"→ Model: {resp.model} (via {method}, {conf_str} confidence)",
            f"→ Task: {task}/{complexity}",
        ]
        if savings_label:
            lines.append(f"→ Cost: {cost_str} ({savings_label}, saved ${saved_usd:.5f})")
        else:
            lines.append(f"→ Cost: {cost_str}")
        if resp.chain_attempts:
            chain_display = []
            for m in resp.chain_attempts[:-1]:
                chain_display.append(f"{m.split('/')[-1]} [✗]")
            chain_display.append(f"{model_short} [✓]")
            lines.append(f"→ Chain: {' → '.join(chain_display)}")
        if ctx_info:
            lines.append(f"→ Context{ctx_info.replace(' | ctx ', ': ')}")
        return "\n─────\n" + "\n".join(lines)

    # Compact one-line format for footer and header
    parts = [model_short]
    if resp.complexity:
        parts.append(resp.complexity)
    parts.append(cost_str)
    if savings_label:
        parts.append(f"({savings_label})")
    compact = " · ".join(parts) + ctx_info

    if mode == "header":
        return f"[→ {compact}]\n\n"
    # footer (default)
    return f"\n─────\n→ {compact}"


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


def _format_response(resp: LLMResponse, task: str = "") -> str:
    """Format a response with consistent header and routing explanation.

    All tools use this function to ensure uniform response formatting across
    all 60 MCP tools. Format:

        [header explanation if mode=header]
        > 🤖 **model** · tokens · $cost · duration
        [content]
        [footer explanation if mode=footer (default)]
        [optional compression note]

    Args:
        resp: The LLM response object with model, tokens, cost, latency.
        task: Task type string for explainability (e.g. "query", "code").

    Returns:
        Formatted response string.
    """
    explanation = _routing_explanation(resp, task)
    mode = _get_explain_mode()

    parts = []
    if mode == "header" and explanation:
        parts.append(explanation.rstrip())
    parts.append(resp.header())
    if resp.content:
        parts.append("")
        # Apply response compression if enabled
        content, was_compressed = _apply_response_compression(resp.content)
        parts.append(content)
        if was_compressed:
            parts.append("\n[Response compressed via Token-Savior. Original available if needed.]")
    if mode in ("footer", "verbose") and explanation:
        parts.append(explanation)
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
    effective = _effective_complexity(complexity)
    await _announce_routing(ctx, "query", effective or "auto")
    resp = await route_and_call(
        TaskType.QUERY,
        prompt,
        complexity_hint=effective,
        model_override=model,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        ctx=ctx,
        caller_context=context,
        route_directive_id=_read_hook_route_directive(),
    )
    _cache_result(prompt, resp, "query", effective)
    _record_quality(resp, "query", effective)
    return await _apply_response_router(_format_response(resp, "query"))


def _is_web_grounded(model: str | None) -> bool:
    """True only when a genuinely web-searching model produced the answer.

    Web grounding is what makes research citations trustworthy. Perplexity's
    sonar models search the live web; anything else (a local Ollama model, o3,
    Gemini 2.5 Pro used as a no-web fallback) is answering from parametric
    memory — its 'citations' are model-generated and may be fabricated.
    """
    return "perplexity" in (model or "").lower() or "sonar" in (model or "").lower()


def _apply_research_trust_contract(
    body: str,
    citations,
    model: str | None,
    *,
    no_perplexity: bool,
) -> str:
    """Enforce the research-output trust contract.

    When a web-grounded model answered, citations are real → render them under
    an authoritative **Sources:** header. When NOT web-grounded (no web backend,
    or a non-web fallback answered), the answer and any 'citations' are
    UNVERIFIED: lead with a prominent banner and never present the citations as
    sources — a confidently fabricated reference is worse than none. This closes
    G-RESEARCH-NOKEY: the local fallback previously emitted a hallucinated arXiv
    id under **Sources:**.
    """
    citations = [c for c in (citations or []) if c]

    if _is_web_grounded(model):
        if citations:
            body += "\n\n**Sources:**\n" + "\n".join(f"- {c}" for c in citations)
        return body

    hint = (
        " Set PERPLEXITY_API_KEY for live web search." if no_perplexity else ""
    )
    banner = (
        "> ⚠️ **UNVERIFIED — not web-grounded.** This answer came from a "
        f"non-web model (`{model or 'unknown'}`), which cannot search the web. "
        "Treat every claim — and any references below — as unverified and "
        f"possibly fabricated; confirm independently before relying on them.{hint}"
    )
    out = banner + "\n\n" + body
    if citations:
        out += (
            "\n\n**Unverified references (model-generated, NOT web-sourced):**\n"
            + "\n".join(f"- {c}  _(unverified — verify before use)_" for c in citations)
        )
    return out


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
    await _announce_routing(ctx, "research", "moderate")

    resp = await route_and_call(
        TaskType.RESEARCH,
        prompt,
        # Without Perplexity, escalate to PREMIUM so the fallback chain uses
        # o3 / Gemini 2.5 Pro rather than silently degrading to BALANCED tier.
        profile=RoutingProfile.PREMIUM if no_perplexity else None,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        temperature=0.3,
        ctx=ctx,
        caller_context=context,
        route_directive_id=_read_hook_route_directive(),
    )
    _cache_result(prompt, resp, "research", "moderate")
    _record_quality(resp, "research", "moderate")

    result = _apply_research_trust_contract(
        _format_response(resp, "research"),
        resp.citations,
        resp.model,
        no_perplexity=no_perplexity,
    )
    return await _apply_response_router(result)


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
    effective = _effective_complexity(complexity)
    await _announce_routing(ctx, "generate", effective or "auto")
    resp = await route_and_call(
        TaskType.GENERATE,
        prompt,
        complexity_hint=effective,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        ctx=ctx,
        caller_context=context,
        route_directive_id=_read_hook_route_directive(),
    )
    _cache_result(prompt, resp, "generate", effective)
    _record_quality(resp, "generate", effective)
    return await _apply_response_router(_format_response(resp, "generate"))


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
    # chosen for a task that inherently requires reasoning. The hook's hint
    # is allowed to raise (moderate → complex) but not lower below moderate.
    effective_complexity = _effective_complexity(complexity, floor="moderate")
    if effective_complexity == "simple":
        effective_complexity = "moderate"
    await _announce_routing(ctx, "analyze", effective_complexity)
    resp = await route_and_call(
        TaskType.ANALYZE,
        prompt,
        complexity_hint=effective_complexity,
        system_prompt=system_prompt,
        temperature=0.3,
        max_tokens=max_tokens,
        ctx=ctx,
        caller_context=context,
        route_directive_id=_read_hook_route_directive(),
    )
    _cache_result(prompt, resp, "analyze", effective_complexity)
    _record_quality(resp, "analyze", effective_complexity)
    return await _apply_response_router(_format_response(resp, "analyze"))


async def llm_reason(
    prompt: str,
    ctx: Context,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
) -> str:
    """Deep reasoning task — routes to extended-thinking models with the REASONING profile.

    Best for: formal proofs, mathematical derivations, multi-step deductive chains,
    philosophical analysis, first-principles explanations, and any task that requires
    explicit step-by-step chain-of-thought reasoning to be correct.

    Unlike ``llm_analyze`` (which floors at moderate and uses the BALANCED→PREMIUM chain),
    ``llm_reason`` always uses ``complexity="deep_reasoning"`` which routes to the
    dedicated REASONING profile:
      • DeepSeek-R1 (cheapest native reasoner, $0.0014/1K)
      • OpenAI o3 (frontier reasoning for the hardest problems)
      • Gemini 2.5 Pro (thinkingConfig enabled, 8192 thinking-token budget)
      • Claude Opus (use_thinking=True, 16K extended-thinking budget)

    Args:
        prompt: The reasoning task or question requiring step-by-step deduction.
        system_prompt: Optional system instructions (e.g. "Think step-by-step").
        max_tokens: Maximum output tokens (defaults to model maximum).
        context: Optional conversation context to help the model understand the broader task.
    """
    await _announce_routing(ctx, "analyze", "deep_reasoning")
    resp = await route_and_call(
        TaskType.ANALYZE,
        prompt,
        complexity_hint="deep_reasoning",
        system_prompt=system_prompt,
        temperature=0.3,
        max_tokens=max_tokens,
        ctx=ctx,
        caller_context=context,
        route_directive_id=_read_hook_route_directive(),
    )
    _cache_result(prompt, resp, "analyze", "deep_reasoning")
    _record_quality(resp, "analyze", "deep_reasoning")
    return await _apply_response_router(_format_response(resp, "analyze"))


# Ponytail system prompt — "lazy senior dev" mode (DietrichGebert/ponytail).
# Injected by default into llm_code calls to minimise output tokens and prevent
# over-engineering. Users who need full creative latitude pass use_ponytail=False.
_PONYTAIL_PROMPT = """You are a lazy senior developer. Lazy means efficient, not careless. The best code is the code never written.

Before writing any code, stop at the first rung that holds:
1. Does this need to be built at all? (YAGNI)
2. Does the standard library already do this? Use it.
3. Does a native platform feature cover it? Use it.
4. Does an already-installed dependency solve it? Use it.
5. Can this be one line? Make it one line.
6. Only then: write the minimum code that works.

Rules:
- No abstractions that weren't explicitly requested.
- No new dependency if it can be avoided.
- No boilerplate nobody asked for.
- Deletion over addition. Boring over clever. Fewest files possible.
- Question complex requests: "Do you actually need X, or does Y cover it?"
- Pick the edge-case-correct option when two stdlib approaches are the same size.
- Mark intentional simplifications with a `ponytail:` comment.

Not lazy about: input validation at trust boundaries, error handling that prevents data loss, security, accessibility, anything explicitly requested. Non-trivial logic leaves ONE runnable check behind."""


async def llm_code(
    prompt: str,
    ctx: Context,
    complexity: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    context: str | None = None,
    use_ponytail: bool = True,
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
        use_ponytail: Apply lazy-senior-dev constraints (YAGNI, stdlib-first, minimum code).
            Defaults to true — reduces output tokens 40-75% by preventing over-engineering.
            Set to false for creative/exploratory tasks where full latitude is needed.
    """
    effective = _effective_complexity(complexity)
    await _announce_routing(ctx, "code", effective or "auto")
    # Compose system prompt: ponytail (default) + any caller-supplied instructions
    if use_ponytail:
        effective_system = (
            _PONYTAIL_PROMPT + "\n\n" + system_prompt if system_prompt else _PONYTAIL_PROMPT
        )
    else:
        effective_system = system_prompt
    resp = await route_and_call(
        TaskType.CODE,
        prompt,
        complexity_hint=effective,
        system_prompt=effective_system,
        temperature=0.2,
        max_tokens=max_tokens,
        ctx=ctx,
        caller_context=context,
        route_directive_id=_read_hook_route_directive(),
    )
    _cache_result(prompt, resp, "code", effective)
    _record_quality(resp, "code", effective)
    # llm_code responses include code blocks (marked critical by the
    # response router and preserved verbatim) plus surrounding prose.
    # The prose explains "why this implementation works"; routing it
    # through a cheaper model risks losing the rationale a downstream
    # caller may rely on. Users who want llm_code prose compressed too
    # can set LLM_ROUTER_RESPONSE_ROUTER=on (default) — it's already on; we
    # rely on the router's CRITICAL_PATTERNS to preserve code blocks.
    return await _apply_response_router(_format_response(resp, "code"))


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
        build_edit_prompt,
        format_edit_result,
        parse_edit_response,
        read_file_for_edit,
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
        TaskType.CODE,
        prompt,
        system_prompt=(
            "You are a precise code editor. Return ONLY a JSON array of edit "
            "instructions. No prose, no explanation outside the JSON."
        ),
        temperature=0.1,
        ctx=ctx,
        route_directive_id=_read_hook_route_directive(),
    )

    instructions, warnings = parse_edit_response(resp.content)
    if read_notes:
        warnings = [f"File truncated: {n}" for n in read_notes] + warnings

    return format_edit_result(instructions, warnings, resp.header())


def register(mcp, should_register=None) -> None:
    """Register text LLM tools with the MCPServer instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_query"):
        mcp.tool()(llm_query)
    if gate("llm_research"):
        mcp.tool()(llm_research)
    if gate("llm_generate"):
        mcp.tool()(llm_generate)
    if gate("llm_analyze"):
        mcp.tool()(llm_analyze)
    if gate("llm_reason"):
        mcp.tool()(llm_reason)
    if gate("llm_code"):
        mcp.tool()(llm_code)
    if gate("llm_edit"):
        mcp.tool()(llm_edit)
