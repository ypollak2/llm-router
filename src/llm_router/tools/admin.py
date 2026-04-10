"""Management tools — llm_save_session, llm_set_profile, llm_usage, llm_cache_stats,
llm_cache_clear, llm_quality_report, llm_health, llm_providers."""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import Context

from llm_router.cache import get_cache
from llm_router.codex_agent import is_codex_available
from llm_router.config import get_config
from llm_router.cost import (
    get_model_acceptance_scores, get_model_latency_stats,
    get_monthly_spend, get_quality_report,
    get_routing_savings_vs_sonnet, get_savings_summary,
    import_savings_log,
)
from llm_router.health import get_tracker
from llm_router.provider_budget import get_provider_budgets
from llm_router.types import RoutingProfile, colorize_provider, MODEL_COST_PER_1K
from llm_router import state as _state


async def llm_save_session(ctx: Context) -> str:
    """Summarize and save the current session for cross-session context.

    Uses a cheap model to generate a compact summary of the session's exchanges,
    then persists it to SQLite. Future routed calls will include this summary
    as context, giving external models awareness of prior work.

    Call this before ending a session or when switching to a different task.
    Sessions with fewer than 3 exchanges are skipped.
    """
    from llm_router.context import auto_summarize_session

    await ctx.info("Summarizing session...")
    summary = await auto_summarize_session(min_messages=3)

    if summary is None:
        return "Session too short (< 3 exchanges) — nothing to save."

    return f"Session saved.\n\n**Summary:** {summary}"


async def llm_set_profile(profile: str) -> str:
    """Switch the active routing profile.

    Args:
        profile: One of "budget", "balanced", or "premium".
    """
    try:
        p = RoutingProfile(profile.lower())
    except ValueError:
        return f"Invalid profile: {profile}. Choose: budget, balanced, premium."

    _state.set_active_profile(p)
    return f"Profile switched to: {p.value}"


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

    _last_usage = _state.get_last_usage()
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
    # Flush any pending hook JSONL records into SQLite before querying
    await import_savings_log()
    savings = await get_savings_summary(period)
    if savings["total_calls"] > 0:
        s = savings
        savings_title = f"ROUTING SAVINGS ({period})"
        lines.append(section(savings_title))

        calls_str = str(s["total_calls"])
        tokens_str = f"{s['total_tokens']:,}"
        lines.append(row(f"  Calls: {calls_str}    Tokens: {tokens_str}"))

        cost_str = f"${s['cost_saved_usd']:.4f}"
        time_str = _state._format_time(s["time_saved_sec"])
        lines.append(row(f"  Saved:  {cost_str}  |  Time: {time_str}"))

        if s["by_model"]:
            lines.append(row(""))
            for model, data in s["by_model"].items():
                tag = {"haiku": "H", "sonnet": "S", "opus": "O"}.get(model, "?")
                tok_str = f"{data['tokens']:,}"
                cell = f"  [{tag}] {model:<8} {data['calls']:>3} calls  {tok_str:>7} tok  saved ${data['cost_saved']:.4f}"
                lines.append(row(cell))

        total_tokens_k = s["total_tokens"] / 1000
        opus_would_cost = total_tokens_k * MODEL_COST_PER_1K["opus"]
        if opus_would_cost > 0:
            actual_cost = opus_would_cost - s["cost_saved_usd"]
            pct_saved = (s["cost_saved_usd"] / opus_would_cost) * 100
            lines.append(row(""))
            lines.append(row(f"  Opus would cost: ${opus_would_cost:.4f}  ->  Actual: ${actual_cost:.4f}  ({pct_saved:.0f}% saved)"))
        lines.append(HR)

    # ── Section 5: Lifetime Savings (from routing_decisions SQLite table) ──
    real_lifetime = await get_routing_savings_vs_sonnet(days=0)
    if real_lifetime["total_calls"] > 0:
        lt = real_lifetime
        lines.append(section("LIFETIME SAVINGS (vs Sonnet 4.6 baseline)"))
        tok_str = f"{lt['input_tokens']:,} in + {lt['output_tokens']:,} out"
        lines.append(row(f"  Calls:    {lt['total_calls']}    Tokens: {tok_str}"))
        lines.append(row(f"  Actual:   ${lt['actual_cost']:.4f}    Baseline: ${lt['baseline_cost']:.4f}"))
        lines.append(row(f"  Saved:    ~${lt['saved']:.4f}"))
        if lt["by_model"]:
            lines.append(row(""))
            lines.append(row("  Per model:"))
            for model, md in sorted(lt["by_model"].items(), key=lambda x: -x[1]["calls"])[:6]:
                short = model.split("/")[-1][:16] if "/" in model else model[:16]
                lines.append(row(
                    f"    {short:<16}  {md['calls']:>4}x  "
                    f"${md['actual_cost']:.4f} actual  ~${md['saved']:.4f} saved"
                ))
        lines.append(HR)

    # ── Section 6: Model Performance (latency + acceptance) ──
    latency_stats, acceptance_scores = await asyncio.gather(
        get_model_latency_stats(window_days=7),
        get_model_acceptance_scores(window_days=30),
    )
    # Only show models that have latency OR acceptance data
    perf_models = sorted(
        set(latency_stats.keys()) | set(acceptance_scores.keys()),
        key=lambda m: -(latency_stats.get(m, {}).get("count", 0)),
    )
    if perf_models:
        lines.append(section("MODEL PERFORMANCE (7d latency / 30d acceptance)"))
        header = f"  {'Model':<24} {'P50':>6} {'P95':>6}  {'Accept':>7}  {'Calls':>5}"
        lines.append(row(header))
        for m in perf_models[:8]:  # cap at 8 rows to keep the box compact
            short = m.split("/")[-1][:22] if "/" in m else m[:22]
            ls = latency_stats.get(m)
            p50_s = f"{ls['p50']/1000:.1f}s" if ls else "  n/a "
            p95_s = f"{ls['p95']/1000:.1f}s" if ls else "  n/a "
            cnt_s = f"{ls['count']}" if ls else "  -  "
            rate = acceptance_scores.get(m)
            acc_s = f"{rate:.0%}" if rate is not None else "  n/a "
            lines.append(row(f"  {short:<24} {p50_s:>6} {p95_s:>6}  {acc_s:>7}  {cnt_s:>5}"))
        lines.append(HR)

    # ── Section 7: Monthly Budget ──
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

    lines.append(row("  Tip: use llm_dashboard to open the visual web dashboard"))
    lines.append(HR)
    return "\n".join(lines)


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


async def llm_cache_clear() -> str:
    """Clear the prompt classification cache."""
    cache = get_cache()
    count = await cache.clear()
    return f"Cleared {count} cached classification entries."


async def llm_quality_report(days: int = 7) -> str:
    """Show routing quality metrics — classification accuracy, savings, model distribution.

    Analyzes routing decisions over the specified period to show how the
    classifier is performing, which models are being selected, downshift
    rates, and cost efficiency.

    Args:
        days: Number of days to include in the report (default 7).
    """
    report = await get_quality_report(days)

    if report["total_decisions"] == 0:
        return f"No routing decisions recorded in the last {days} days."

    W = 58
    HR = "+" + "-" * W + "+"

    def row(text: str) -> str:
        return f"| {text:<{W - 1}}|"

    def section(title: str) -> str:
        return "|" + f" {title} ".center(W, "-") + "|"

    lines = [HR, "|" + f" Routing Quality Report ({days}d) ".center(W) + "|", HR]

    # Overview
    lines.append(section("OVERVIEW"))
    lines.append(row(f"  Decisions:     {report['total_decisions']}"))
    lines.append(row(f"  Avg confidence: {report['avg_confidence']:.0%}"))
    lines.append(row(f"  Success rate:  {report['success_rate']:.0%}"))
    lines.append(row(f"  Downshift rate: {report['downshift_rate']:.0%}"))
    lines.append(row(f"  Avg latency:   {report['avg_latency_ms']:.0f}ms"))
    lines.append(row(f"  Total cost:    ${report['total_cost_usd']:.4f}"))
    lines.append(row(f"  Total tokens:  {report['total_tokens']:,}"))
    lines.append(HR)

    # By classifier type
    if report["by_classifier"]:
        lines.append(section("BY CLASSIFIER"))
        for clf_type, count in report["by_classifier"].items():
            pct = count / report["total_decisions"]
            lines.append(row(f"  {clf_type:<16} {count:>5}  ({pct:>5.0%})"))
        lines.append(HR)

    # By task type
    if report["by_task_type"]:
        lines.append(section("BY TASK TYPE"))
        for task, count in report["by_task_type"].items():
            pct = count / report["total_decisions"]
            lines.append(row(f"  {task:<16} {count:>5}  ({pct:>5.0%})"))
        lines.append(HR)

    # By model
    if report["by_model"]:
        lines.append(section("BY MODEL"))
        lines.append(row(f"  {'Model':<24} {'Calls':>5}  {'Avg ms':>7}  {'Cost':>8}"))
        lines.append(row("  " + "-" * 50))
        for model, stats in report["by_model"].items():
            short = model.split("/")[-1] if "/" in model else model
            lines.append(row(
                f"  {short:<24} {stats['count']:>5}  "
                f"{stats['avg_latency']:>6.0f}ms  ${stats['total_cost']:>7.4f}"
            ))
        lines.append(HR)

    return "\n".join(lines)


async def llm_health() -> str:
    """Check the health status of all configured LLM providers."""
    from llm_router.config import probe_ollama
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

    # Show Ollama reachability explicitly — it's config-based AND needs a live probe
    if config.ollama_base_url:
        reachable = probe_ollama(config.ollama_base_url)
        ollama_status = "reachable ✅" if reachable else "unreachable ❌ — run: ollama serve"
        lines.append(f"\n🦙 Ollama ({config.ollama_base_url}): {ollama_status}")

    lines.append("\nTip: use llm_dashboard to open the visual web dashboard at localhost:7337")
    return "\n".join(lines)


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


async def llm_dashboard(port: int = 7337) -> str:
    """Open the LLM Router web dashboard in the background.

    Starts a local HTTP server at localhost:<port> showing routing stats,
    cost trends, model distribution, and recent decisions. Refreshes every 30s.

    The dashboard reads from the same SQLite DB the router writes — no extra
    configuration needed.

    Args:
        port: TCP port for the dashboard server (default 7337).

    Returns:
        URL and instructions for opening the dashboard.
    """
    import asyncio
    import subprocess
    import sys

    # Start as a detached background process so the MCP server stays responsive.
    # Using sys.executable guarantees the same venv Python is used.
    # start_new_session=True works on macOS and Linux; on Windows the process
    # still starts but is not fully detached (acceptable — Windows users can
    # close the terminal window to stop it).
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]

    subprocess.Popen(
        [sys.executable, "-m", "llm_router.dashboard.__main__", "--port", str(port)],
        **kwargs,
    )

    # Give it a moment to bind the port
    await asyncio.sleep(1)

    stop_cmd = (
        "pkill -f 'llm_router.dashboard'"
        if sys.platform != "win32"
        else "taskkill /F /FI \"WINDOWTITLE eq llm_router.dashboard\""
    )

    return (
        f"✅ Dashboard started at http://localhost:{port}\n\n"
        "Open that URL in your browser. It shows:\n"
        "- Today's calls, cost, and tokens\n"
        "- Monthly spend vs budget\n"
        "- Lifetime savings vs Opus baseline\n"
        "- Model & task-type distribution (7 days)\n"
        "- Daily cost trend (14 days)\n"
        "- Recent routing decisions\n\n"
        f"The dashboard auto-refreshes every 30 seconds.\n"
        f"Stop it with: {stop_cmd}"
    )


async def llm_savings() -> str:
    """Show time-bucketed savings dashboard: today / this week / this month / all-time.

    Displays actual spend vs Sonnet baseline and the efficiency multiplier (Nx)
    for each period. Use this to understand the real dollar value routing provides.

    Returns:
        Formatted savings table with efficiency multiplier.
    """
    from llm_router.cost import get_savings_by_period

    # Flush any hook-written JSONL records into SQLite before querying
    await import_savings_log()
    data = await get_savings_by_period()

    W = 58
    HR = "+" + "-" * W + "+"

    def row(text: str) -> str:
        return f"| {text:<{W - 1}}|"

    def section(title: str) -> str:
        return "|" + f" {title} ".center(W, "-") + "|"

    lines = [HR, "|" + " 💰 Savings Dashboard ".center(W) + "|", HR]

    period_labels = [
        ("today",    "Today"),
        ("week",     "This week"),
        ("month",    "This month"),
        ("all_time", "All time"),
    ]

    lines.append(section("SAVINGS vs SONNET BASELINE"))
    lines.append(row(f"  {'Period':<12}  {'Saved':>8}  {'Actual':>8}  {'Baseline':>9}  {'Eff':>5}"))
    lines.append(row("  " + "-" * 52))

    best_efficiency = 0.0
    for key, label in period_labels:
        d = data.get(key, {})
        saved = d.get("saved_usd", 0.0)
        actual = d.get("actual_usd", 0.0)
        baseline = d.get("baseline_usd", 0.0)
        eff = d.get("efficiency", 0.0)
        best_efficiency = max(best_efficiency, eff)
        eff_str = f"{eff:.1f}x" if eff >= 1.0 else "—"
        lines.append(row(
            f"  {label:<12}  ${saved:>7.2f}  ${actual:>7.4f}  ${baseline:>8.2f}  {eff_str:>5}"
        ))

    lines.append(HR)

    # Highlight the "wow" metric
    if best_efficiency >= 2.0:
        lines.append(section(f"YOUR AI IS {best_efficiency:.1f}x MORE COST-EFFICIENT"))
        lines.append(row("  than using Sonnet for every request."))
    elif data.get("all_time", {}).get("calls", 0) == 0:
        lines.append(row("  No routed calls yet. Run a few prompts to see savings."))

    lines.append(HR)
    lines.append(row("  Tip: run `llm-router test \"<prompt>\"` to simulate routing"))
    lines.append(HR)

    return "\n".join(lines)


async def llm_team_report(period: str = "week") -> str:
    """Show a team savings report for the current user and project.

    Displays call counts, cost savings, free-tier usage, and top models,
    broken down for the auto-detected user (git email) and project (git remote).

    Args:
        period: ``"today"``, ``"week"``, ``"month"``, or ``"all"``.
    """
    from llm_router.team import build_team_report, get_project_id, get_user_id, detect_channel

    config = get_config()
    user_id = get_user_id(override=config.llm_router_user_id)
    project_id = get_project_id()

    report = await build_team_report(user_id=user_id, project_id=project_id, period=period)

    W = 58
    HR = "+" + "-" * W + "+"

    def row(text: str) -> str:
        return f"| {text:<{W-1}}|"

    def section(title: str) -> str:
        return "|" + f" {title} ".center(W, "-") + "|"

    def bar(pct: float, width: int = 10) -> str:
        filled = round(pct * width)
        return "█" * filled + "░" * (width - filled)

    lines = [HR, "|" + " Team Savings Report ".center(W) + "|", HR]
    lines.append(row(f"  User:    {user_id}"))
    lines.append(row(f"  Project: {project_id}"))
    lines.append(row(f"  Period:  {period}"))
    lines.append(HR)

    calls = report["total_calls"]
    saved = report["saved_usd"]
    actual = report["actual_usd"]
    free_pct = report["free_pct"]

    if calls == 0:
        lines.append(row("  No routing data for this period."))
    else:
        lines.append(row(f"  Calls:     {calls:,}"))
        lines.append(row(f"  Saved:     ~${saved:.4f}  (paid ${actual:.4f})"))
        lines.append(row(f"  Free tier: {free_pct:.0%}  {bar(free_pct)}"))

        top = report.get("top_models", [])
        if top:
            lines.append(row(""))
            lines.append(row("  Top models:"))
            for m in top[:6]:
                short = m["model"].split("/")[-1][:20] if "/" in m["model"] else m["model"][:20]
                lines.append(row(f"    {short:<20}  {m['calls']:>4}x  ${m['cost']:.4f}"))

    lines.append(HR)

    endpoint = config.llm_router_team_endpoint
    if endpoint:
        channel = detect_channel(endpoint)
        lines.append(row(f"  Push endpoint: {channel} ({endpoint[:40]}...)"))
        lines.append(row("  Run llm_team_push to send this report."))
    else:
        lines.append(row("  Set LLM_ROUTER_TEAM_ENDPOINT to push to Slack/Discord/Telegram."))
    lines.append(HR)
    return "\n".join(lines)


async def llm_team_push(period: str = "week") -> str:
    """Push the team savings report to the configured notification channel.

    Sends a formatted message to the endpoint set by ``LLM_ROUTER_TEAM_ENDPOINT``.
    Channel is auto-detected from the URL:
      - hooks.slack.com        → Slack Block Kit message
      - discord.com/api/webhooks → Discord Embed
      - api.telegram.org/bot*  → Telegram MarkdownV2 message
      - anything else          → Generic JSON POST

    Args:
        period: ``"today"``, ``"week"``, ``"month"``, or ``"all"``.
    """
    from llm_router.team import build_team_report, detect_channel, get_project_id, get_user_id, push_report

    config = get_config()
    endpoint = config.llm_router_team_endpoint
    if not endpoint:
        return (
            "No team endpoint configured.\n"
            "Set LLM_ROUTER_TEAM_ENDPOINT in your .env or routing.yaml:\n\n"
            "  LLM_ROUTER_TEAM_ENDPOINT=https://hooks.slack.com/...    # Slack\n"
            "  LLM_ROUTER_TEAM_ENDPOINT=https://discord.com/api/webhooks/...  # Discord\n"
            "  LLM_ROUTER_TEAM_ENDPOINT=https://api.telegram.org/bot{token}/  # Telegram\n"
            "  LLM_ROUTER_TEAM_ENDPOINT=https://your-server.com/webhook  # Generic\n"
        )

    user_id = get_user_id(override=config.llm_router_user_id)
    project_id = get_project_id()
    chat_id = config.llm_router_team_chat_id

    report = await build_team_report(user_id=user_id, project_id=project_id, period=period)
    channel = detect_channel(endpoint)

    success, message = await push_report(report, endpoint, telegram_chat_id=chat_id)

    if success:
        return (
            f"✓ Report pushed to {channel}\n\n"
            f"  User:    {user_id}\n"
            f"  Project: {project_id}\n"
            f"  Period:  {period}\n"
            f"  Calls:   {report['total_calls']:,}\n"
            f"  Saved:   ~${report['saved_usd']:.4f}\n"
            f"  Free:    {report['free_pct']:.0%}\n"
        )
    return f"✗ Push failed: {message}"


async def llm_policy() -> str:
    """Show the active routing policy and recent policy audit events.

    Displays the merged policy from all three layers:
      - Org policy   (~/.llm-router/org-policy.yaml)
      - User policy  (~/.llm-router/routing.yaml)
      - Repo policy  (.llm-router.yml)

    Also shows the last 10 policy enforcement events from the audit log.
    """
    from llm_router.policy import load_org_policy, policy_summary
    from llm_router.repo_config import effective_config as get_repo_config
    from llm_router.cost import _get_db

    org = load_org_policy()
    repo = get_repo_config()

    W = 64
    lines = [
        "─" * W,
        "  Routing Policy (org → user → repo)",
        "",
        "  Org layer:",
    ]
    lines.append(policy_summary(org))

    lines += ["", "  Repo/user layer:"]
    if repo.block_providers:
        lines.append(f"    block_providers: {', '.join(repo.block_providers)}")
    if repo.block_models:
        lines.append(f"    block_models:    {', '.join(repo.block_models)}")
    if repo.allow_models:
        lines.append(f"    allow_models:    {', '.join(repo.allow_models)}")
    if repo.daily_caps:
        lines.append(f"    daily_caps:      {repo.daily_caps}")
    if repo._sources:
        lines.append(f"    sources:         {', '.join(repo._sources)}")
    if not (repo.block_providers or repo.block_models or repo.allow_models or repo.daily_caps):
        lines.append("    No restrictions configured.")

    # Recent policy audit events
    try:
        db = await _get_db()
        rows = await (await db.execute(
            """
            SELECT timestamp, task_type, final_model, policy_applied
            FROM routing_decisions
            WHERE policy_applied IS NOT NULL
            ORDER BY id DESC LIMIT 10
            """
        )).fetchall()
        await db.close()
        if rows:
            lines += ["", "  Recent policy events (last 10):"]
            for ts, task, model, pol in rows:
                lines.append(f"    {ts[:16]}  {task:<10}  {model or '?':<25}  {pol}")
        else:
            lines += ["", "  No policy enforcement events recorded yet."]
    except Exception:
        pass

    lines.append("─" * W)
    return "\n".join(lines)


async def llm_digest(period: str = "week", send: bool = False) -> str:
    """Generate a savings digest and optionally send it to a webhook.

    Formats a savings summary for the given period. Also detects spend spikes
    and shows a "what if router was off?" simulation.

    Args:
        period: ``"today"``, ``"week"``, ``"month"``, or ``"all time"``.
        send:   If True, POST the digest to LLM_ROUTER_WEBHOOK_URL.
    """
    from llm_router.digest import format_digest, send_to_webhook

    digest = await format_digest(period)

    if not send:
        return digest

    config = get_config()
    url = config.llm_router_webhook_url or config.llm_router_team_endpoint
    if not url:
        return (
            digest + "\n\n"
            "  ℹ️  To send this digest automatically, set:\n"
            "  LLM_ROUTER_WEBHOOK_URL=https://hooks.slack.com/...\n"
        )

    ok, msg = await send_to_webhook(url, digest)
    return digest + f"\n\n  {'✓' if ok else '✗'}  {msg}"


async def llm_benchmark() -> str:
    """Show routing accuracy benchmarks by task type.

    Accuracy is computed from llm_rate feedback (thumbs up/down).
    The more you rate responses with llm_rate, the more accurate this becomes.

    Also shows an optional community export status if LLM_ROUTER_COMMUNITY=true.
    """
    from llm_router.community import (
        format_benchmark_report,
        get_benchmark_stats,
        prepare_community_export,
    )

    stats = await get_benchmark_stats()
    report = format_benchmark_report(stats)

    config = get_config()
    if config.llm_router_community:
        export_msg = await prepare_community_export()
        report += f"\n\n  📤 Community export: {export_msg}"
    else:
        report += (
            "\n\n  ℹ️  Set LLM_ROUTER_COMMUNITY=true to prepare an anonymous "
            "export of your routing outcomes for community benchmarking."
        )

    return report


async def llm_session_spend() -> str:
    """Show real-time session cost breakdown.

    Reports spend accumulated since this session started, broken down by model
    and tool. Fires an anomaly warning if spend exceeds the configured threshold
    (default $0.50) in under 10 minutes.

    Returns a formatted summary with per-model costs and anomaly status.
    """
    from llm_router.session_spend import get_session_spend
    import time as _time

    spend = get_session_spend()
    summary = spend.get_summary()

    elapsed = _time.time() - summary.get("session_start", _time.time())
    elapsed_min = int(elapsed / 60)

    lines = [
        f"**Session spend** ({elapsed_min}m elapsed)",
        "",
        f"Total: **${summary['total_usd']:.4f}** across {summary['call_count']} calls",
    ]

    if summary.get("anomaly_flag"):
        lines.insert(0, "⚠️  ANOMALY: Spend threshold exceeded in < 10 minutes!")
        lines.insert(1, "")

    if summary.get("per_model"):
        lines.append("")
        lines.append("Per model:")
        for model, stats in sorted(
            summary["per_model"].items(),
            key=lambda x: -x[1]["cost_usd"],
        ):
            short = model.split("/", 1)[-1] if "/" in model else model
            lines.append(
                f"  {short:<28} {stats['calls']:>3}× calls  "
                f"${stats['cost_usd']:.4f}  "
                f"{stats['tokens']:,} tokens"
            )

    if summary.get("per_tool"):
        lines.append("")
        lines.append("Per tool:")
        for tool, count in sorted(summary["per_tool"].items(), key=lambda x: -x[1]):
            lines.append(f"  {tool:<24} {count:>3}× calls")

    config = get_config()
    if config.llm_router_escalate_above > 0:
        lines.append("")
        lines.append(
            f"Per-call cap: ${config.llm_router_escalate_above:.2f} "
            f"(LLM_ROUTER_ESCALATE_ABOVE)"
        )
    if config.llm_router_hard_stop_above > 0:
        lines.append(
            f"Session hard stop: ${config.llm_router_hard_stop_above:.2f} "
            f"(LLM_ROUTER_HARD_STOP_ABOVE)"
        )

    return "\n".join(lines)


# ── Pending approval state for cost-threshold escalation ─────────────────────

_pending_approval: dict | None = None


def _set_pending_approval(data: dict | None) -> None:
    global _pending_approval
    _pending_approval = data


def _get_pending_approval() -> dict | None:
    return _pending_approval


async def llm_approve_route(
    approve: bool = True,
    downgrade_to: str = "",
) -> str:
    """Approve or reject a pending high-cost routing decision.

    Use this when llm_route (or any routing tool) blocked a call because the
    estimated cost exceeded LLM_ROUTER_ESCALATE_ABOVE. The pending call is
    stored server-side until you approve or cancel it.

    Args:
        approve: True to proceed with the call, False to cancel it.
        downgrade_to: Optional cheaper model to use instead of the blocked one
                      (e.g. "gemini/gemini-2.5-flash" instead of "openai/o3").
    """
    pending = _get_pending_approval()

    if pending is None:
        return "No pending routing approval. All calls are proceeding normally."

    if not approve:
        _set_pending_approval(None)
        return (
            f"Routing cancelled.\n\n"
            f"Blocked call was: {pending.get('model', 'unknown')} "
            f"(estimated ${pending.get('estimated_cost', 0):.4f})\n"
            f"Use a cheaper tool like `llm_query` for simple tasks."
        )

    model = downgrade_to or pending.get("model", "")
    _set_pending_approval(None)

    return (
        f"Approved. Proceeding with: **{model}**\n\n"
        f"Estimated cost: ${pending.get('estimated_cost', 0):.4f}\n\n"
        f"Re-run your original prompt now — routing will proceed without interruption."
    )


def register(mcp, should_register=None) -> None:
    """Register management tools with the FastMCP instance.

    Args:
        should_register: Optional callable(tool_name) -> bool for slim mode filtering.
    """
    gate = should_register or (lambda _: True)
    if gate("llm_save_session"):
        mcp.tool()(llm_save_session)
    if gate("llm_set_profile"):
        mcp.tool()(llm_set_profile)
    if gate("llm_usage"):
        mcp.tool()(llm_usage)
    if gate("llm_savings"):
        mcp.tool()(llm_savings)
    if gate("llm_cache_stats"):
        mcp.tool()(llm_cache_stats)
    if gate("llm_cache_clear"):
        mcp.tool()(llm_cache_clear)
    if gate("llm_quality_report"):
        mcp.tool()(llm_quality_report)
    if gate("llm_health"):
        mcp.tool()(llm_health)
    if gate("llm_providers"):
        mcp.tool()(llm_providers)
    if gate("llm_dashboard"):
        mcp.tool()(llm_dashboard)
    if gate("llm_team_report"):
        mcp.tool()(llm_team_report)
    if gate("llm_team_push"):
        mcp.tool()(llm_team_push)
    if gate("llm_policy"):
        mcp.tool()(llm_policy)
    if gate("llm_digest"):
        mcp.tool()(llm_digest)
    if gate("llm_benchmark"):
        mcp.tool()(llm_benchmark)
    if gate("llm_session_spend"):
        mcp.tool()(llm_session_spend)
    if gate("llm_approve_route"):
        mcp.tool()(llm_approve_route)
