#!/usr/bin/env python3
"""
LLM Router Demo 2 — "Building a SaaS Analytics Dashboard"
==========================================================
More complex use-case: a developer building a production SaaS product
that processes events, generates reports, and has an AI assistant.

This exercises ALL routing paths:
  - BUDGET:   Quick factual lookups, boilerplate generation
  - BALANCED: Non-trivial code, architecture analysis, content writing
  - PREMIUM:  Complex multi-constraint system design
  - RESEARCH: Live web data (market research, library docs)
  - IMAGE:    Generate product assets (if provider configured)

Also exercises:
  - Codex (free via OpenAI subscription)
  - Multiple fallbacks
  - High-pressure simulation (90%+ Claude quota)

Usage:
    uv run python demo/saas_builder_demo.py
    uv run python demo/saas_builder_demo.py --dry-run
    uv run python demo/saas_builder_demo.py --dry-run --pressure 0.92
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType

# ─────────────────────────────────────────────────────────────────────────────
# 12-task SaaS build session
# ─────────────────────────────────────────────────────────────────────────────

DEMO_TASKS: list[dict] = [

    # ── Phase 1: Research & Planning ─────────────────────────────────────────

    {
        "id": "P1", "phase": "Research",
        "tier": "BALANCED", "profile": RoutingProfile.BALANCED, "task_type": TaskType.RESEARCH,
        "name": "Market research: analytics SaaS landscape 2025",
        "prompt": (
            "What are the top open-source and SaaS analytics platforms in 2025? "
            "Focus on: PostHog, Mixpanel, Amplitude, self-hosted alternatives. "
            "Which has gained the most traction among startups recently?"
        ),
        "expected": "perplexity/sonar (web-grounded research)",
    },
    {
        "id": "P2", "phase": "Research",
        "tier": "BUDGET", "profile": RoutingProfile.BUDGET, "task_type": TaskType.QUERY,
        "name": "Quick: ClickHouse vs TimescaleDB for time-series",
        "prompt": (
            "One paragraph: ClickHouse vs TimescaleDB for storing analytics events "
            "(millions/day). Which should I use for a new SaaS product?"
        ),
        "expected": "haiku / gemini-flash (cheap factual Q&A)",
    },

    # ── Phase 2: System Design ────────────────────────────────────────────────

    {
        "id": "D1", "phase": "Design",
        "tier": "PREMIUM", "profile": RoutingProfile.PREMIUM, "task_type": TaskType.ANALYZE,
        "name": "Architecture: real-time event ingestion pipeline",
        "prompt": (
            "Design a production event ingestion pipeline for an analytics SaaS handling "
            "10M events/day from 500 tenants. Requirements:\n"
            "- Multi-tenant isolation\n"
            "- < 5s end-to-end latency for dashboards\n"
            "- Exactly-once delivery\n"
            "- Cost: < $500/month at 10M events/day\n\n"
            "Compare: Kafka vs Kinesis vs SQS + ClickHouse vs TimescaleDB. "
            "Give a concrete architecture diagram (text), cost estimate per component, "
            "and the 3 biggest technical risks."
        ),
        "expected": "premium model (complex multi-constraint analysis)",
    },
    {
        "id": "D2", "phase": "Design",
        "tier": "BALANCED", "profile": RoutingProfile.BALANCED, "task_type": TaskType.ANALYZE,
        "name": "Review: multi-tenant DB schema design",
        "prompt": (
            "Review this multi-tenant schema for an analytics SaaS:\n\n"
            "CREATE TABLE events (\n"
            "    id UUID DEFAULT gen_random_uuid(),\n"
            "    tenant_id UUID NOT NULL,\n"
            "    event_name VARCHAR(255),\n"
            "    properties JSONB,\n"
            "    occurred_at TIMESTAMPTZ DEFAULT NOW()\n"
            ");\n\n"
            "Issues to check: row-level security, indexing strategy for tenant isolation, "
            "JSONB query performance, partition strategy. Be specific about what's missing."
        ),
        "expected": "mid-tier model (code/schema analysis)",
    },

    # ── Phase 3: Implementation ───────────────────────────────────────────────

    {
        "id": "I1", "phase": "Implementation",
        "tier": "BALANCED", "profile": RoutingProfile.BALANCED, "task_type": TaskType.CODE,
        "name": "Event ingestion API (FastAPI + async)",
        "prompt": (
            "Implement a FastAPI endpoint for event ingestion:\n"
            "POST /ingest/{tenant_id}/events\n"
            "- Accepts a batch of up to 1000 events\n"
            "- Validates tenant_id exists and has quota remaining\n"
            "- Writes to an async queue (use asyncio.Queue as placeholder)\n"
            "- Returns 202 Accepted immediately\n"
            "- Rate limits: 100 req/s per tenant\n\n"
            "Use Pydantic v2, proper error handling, type annotations. "
            "Show complete runnable code."
        ),
        "expected": "codex/sonnet (code generation)",
    },
    {
        "id": "I2", "phase": "Implementation",
        "tier": "BALANCED", "profile": RoutingProfile.BALANCED, "task_type": TaskType.CODE,
        "name": "Dashboard aggregation query (ClickHouse SQL)",
        "prompt": (
            "Write a ClickHouse SQL query for a 'daily active users' dashboard metric:\n"
            "- Table: events(tenant_id UUID, user_id UUID, event_name String, occurred_at DateTime)\n"
            "- Show: DAU for last 30 days, rolling 7-day average, WoW % change\n"
            "- Optimize for ClickHouse (use proper engines, no JOINs on large tables)\n"
            "- Parameterized for tenant isolation\n\n"
            "Include both the query and a brief explanation of why each optimization matters."
        ),
        "expected": "codex/sonnet (code/SQL generation)",
    },
    {
        "id": "I3", "phase": "Implementation",
        "tier": "BUDGET", "profile": RoutingProfile.BUDGET, "task_type": TaskType.GENERATE,
        "name": "Generate: API documentation for ingestion endpoint",
        "prompt": (
            "Write OpenAPI/Swagger documentation for this endpoint:\n"
            "POST /ingest/{tenant_id}/events\n"
            "Request body: {events: [{name, properties, timestamp}], batch_id?: string}\n"
            "Responses: 202 (accepted), 400 (invalid), 429 (rate limited), 507 (quota exceeded)\n\n"
            "Format as YAML. Include example request/response bodies."
        ),
        "expected": "haiku/flash (cheap text generation)",
    },

    # ── Phase 4: AI Features ──────────────────────────────────────────────────

    {
        "id": "A1", "phase": "AI Features",
        "tier": "PREMIUM", "profile": RoutingProfile.PREMIUM, "task_type": TaskType.ANALYZE,
        "name": "Design: AI query-to-SQL natural language interface",
        "prompt": (
            "Design an AI-powered 'ask your data' feature for an analytics SaaS.\n\n"
            "Users type questions like:\n"
            "  'Show me top 10 events by DAU last week'\n"
            "  'Which users churned after the v2 release?'\n\n"
            "Design the full system:\n"
            "1. Prompt engineering to convert NL → ClickHouse SQL (safely, no injection)\n"
            "2. Schema-aware context injection strategy (how to pass schema without token bloat)\n"
            "3. Query validation + sandboxing before execution\n"
            "4. Caching strategy for repeated queries\n"
            "5. Error recovery when SQL is invalid\n\n"
            "Include example prompts, the system prompt template, and estimated token costs."
        ),
        "expected": "premium model (complex AI system design)",
    },
    {
        "id": "A2", "phase": "AI Features",
        "tier": "BALANCED", "profile": RoutingProfile.BALANCED, "task_type": TaskType.CODE,
        "name": "Implement: NL-to-SQL prompt builder",
        "prompt": (
            "Implement a Python class NLToSQLBuilder that:\n"
            "1. Takes a ClickHouse schema dict and a natural language query\n"
            "2. Builds a system prompt with schema context (max 2000 tokens)\n"
            "3. Calls an LLM to generate SQL\n"
            "4. Validates the SQL (no INSERT/UPDATE/DROP, only SELECT)\n"
            "5. Returns (sql: str, confidence: float, explanation: str)\n\n"
            "Use litellm for the LLM call. Include type hints, docstring, and a usage example."
        ),
        "expected": "codex/sonnet (code generation)",
    },

    # ── Phase 5: Growth ───────────────────────────────────────────────────────

    {
        "id": "G1", "phase": "Growth",
        "tier": "BALANCED", "profile": RoutingProfile.BALANCED, "task_type": TaskType.RESEARCH,
        "name": "Research: pricing models for analytics SaaS 2025",
        "prompt": (
            "What pricing models work best for analytics SaaS in 2025? "
            "Look at: event-based, seat-based, usage tiers. "
            "What have PostHog, Amplitude, and Mixpanel recently changed in their pricing? "
            "What's the going rate for 1M events/month for a startup?"
        ),
        "expected": "perplexity (web search for current pricing data)",
    },
    {
        "id": "G2", "phase": "Growth",
        "tier": "BUDGET", "profile": RoutingProfile.BUDGET, "task_type": TaskType.GENERATE,
        "name": "Write: product launch tweet thread",
        "prompt": (
            "Write a Twitter/X thread (5 tweets) announcing the launch of 'PulseDB' — "
            "an open-source analytics platform that's 10x cheaper than Mixpanel. "
            "Target audience: startup founders and developers. "
            "Include: hook tweet, pain point, differentiator, social proof placeholder, CTA."
        ),
        "expected": "haiku/flash (cheap content generation)",
    },
    {
        "id": "G3", "phase": "Growth",
        "tier": "PREMIUM", "profile": RoutingProfile.PREMIUM, "task_type": TaskType.ANALYZE,
        "name": "Strategic analysis: GTM strategy for open-source analytics SaaS",
        "prompt": (
            "Analyze the go-to-market strategy for launching an open-source analytics SaaS "
            "competing with PostHog (Series B, $50M raised) and Mixpanel ($200M+ revenue).\n\n"
            "My constraints:\n"
            "- Solo founder, no funding\n"
            "- Product is technically better but unknown brand\n"
            "- 6 months runway\n\n"
            "Analyze: (1) which customer segment to target first and why, "
            "(2) top 3 distribution channels ranked by CAC/LTV, "
            "(3) what PostHog got right in their PLG motion that I should copy, "
            "(4) the single most important metric to optimize in month 1."
        ),
        "expected": "premium model (complex strategic analysis)",
    },
]


@dataclass
class TaskResult:
    task_id: str
    phase: str
    tier: str
    task_name: str
    task_type: str
    expected: str
    profile: str = ""
    success: bool = False
    model_used: str = ""
    provider_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    response_preview: str = ""
    error: str = ""
    routing_correct: bool = False
    routing_note: str = ""


def _assess_routing(task: dict, result: TaskResult) -> None:
    model = result.model_used.lower()
    task_type = task["task_type"]
    tier = task["tier"]

    if not result.success:
        result.routing_note = "— task failed"
        return

    if task_type == TaskType.RESEARCH:
        if "perplexity" in model:
            result.routing_correct = True
            result.routing_note = "✅ Perplexity for RESEARCH"
        else:
            result.routing_correct = False
            result.routing_note = f"⚠️  RESEARCH should use Perplexity, got {result.model_used}"
        return

    if tier == "BUDGET":
        cheap = {"haiku", "flash", "flash-lite", "groq", "deepseek", "mini", "ollama"}
        if any(m in model for m in cheap):
            result.routing_correct = True
            result.routing_note = f"✅ Cheap model for BUDGET ({result.model_used})"
        elif "opus" in model or ("sonnet" in model and "haiku" not in model):
            result.routing_correct = False
            result.routing_note = f"⚠️  Over-routed for BUDGET: {result.model_used}"
        else:
            result.routing_correct = True
            result.routing_note = f"✅ {result.model_used}"
        return

    if tier == "BALANCED":
        good = {"sonnet", "gpt-4o", "gemini-2.5-pro", "codex", "mistral-large", "deepseek"}
        if any(m in model for m in good):
            result.routing_correct = True
            result.routing_note = f"✅ Mid-tier for BALANCED ({result.model_used})"
        elif "opus" in model:
            result.routing_correct = False
            result.routing_note = f"⚠️  Over-routed to Opus for BALANCED"
        elif any(m in model for m in {"haiku", "flash", "mini"}):
            result.routing_correct = False
            result.routing_note = f"⚠️  Under-routed for BALANCED: {result.model_used}"
        else:
            result.routing_correct = True
            result.routing_note = f"✅ {result.model_used}"
        return

    if tier == "PREMIUM":
        premium = {"opus", "o3", "gemini-2.5-pro", "gpt-4o", "sonnet", "codex", "deepseek-reasoner"}
        if any(m in model for m in premium):
            result.routing_correct = True
            result.routing_note = f"✅ Premium model ({result.model_used})"
        else:
            result.routing_correct = False
            result.routing_note = f"⚠️  Unexpected model for PREMIUM: {result.model_used}"


async def run_demo(dry_run: bool = False, pressure: float = 0.0) -> list[TaskResult]:
    if pressure > 0:
        try:
            from llm_router.claude_usage import set_claude_pressure
            set_claude_pressure(pressure)
            print(f"  ⚡ Simulated Claude quota pressure: {pressure:.0%}")
        except Exception as e:
            print(f"  ⚠️  Could not set pressure: {e}")

    results: list[TaskResult] = []
    total_cost = 0.0
    total_tokens = 0
    phase_costs: dict[str, float] = {}

    print("\n" + "═" * 70)
    print("  LLM Router Demo 2 — Building PulseDB (Analytics SaaS)")
    print("  Mode:", "DRY RUN" if dry_run else "LIVE", " | Tasks:", len(DEMO_TASKS))
    print("═" * 70)

    current_phase = ""
    for task in DEMO_TASKS:
        if task["phase"] != current_phase:
            current_phase = task["phase"]
            print(f"\n{'─' * 70}")
            print(f"  Phase: {current_phase}")
            print(f"{'─' * 70}")

        result = TaskResult(
            task_id=task["id"], phase=task["phase"], tier=task["tier"],
            task_name=task["name"], task_type=task["task_type"].value,
            expected=task["expected"], profile=task["profile"].value,
        )

        short_prompt = task["prompt"][:90].replace("\n", " ")
        print(f"\n  [{task['id']}] {task['tier']} — {task['name']}")
        print(f"       {task['task_type'].value} | {task['profile'].value} | {task['expected']}")
        print(f"       Prompt: {short_prompt}{'...' if len(task['prompt']) > 90 else ''}")

        t0 = time.time()
        try:
            if dry_run:
                from llm_router.profiles import get_model_chain
                chain = get_model_chain(task["profile"], task["task_type"])
                first_model = chain[0] if chain else "unknown"
                provider = first_model.split("/")[0] if "/" in first_model else "unknown"
                mock = LLMResponse(
                    content="[MOCK]", model=first_model, input_tokens=100,
                    output_tokens=60, cost_usd=0.0002, latency_ms=400.0, provider=provider,
                )
                resp = mock
                chain_str = " → ".join(chain[:4]) + ("…" if len(chain) > 4 else "")
                print(f"       Chain: {chain_str}")
            else:
                resp = await route_and_call(
                    task["task_type"], task["prompt"], profile=task["profile"],
                )

            elapsed_ms = (time.time() - t0) * 1000
            result.success = True
            result.model_used = resp.model
            result.provider_used = resp.provider
            result.input_tokens = resp.input_tokens
            result.output_tokens = resp.output_tokens
            result.cost_usd = resp.cost_usd
            result.latency_ms = resp.latency_ms or elapsed_ms
            result.response_preview = (resp.content or "")[:200]

            total_cost += resp.cost_usd
            total_tokens += resp.input_tokens + resp.output_tokens
            phase_costs[task["phase"]] = phase_costs.get(task["phase"], 0) + resp.cost_usd

            print(f"       ✓ {resp.model}  |  {resp.input_tokens}+{resp.output_tokens}tok  |  ${resp.cost_usd:.5f}  |  {result.latency_ms:.0f}ms")

        except Exception as e:
            result.success = False
            result.error = str(e)
            print(f"       ✗ FAILED: {e}")

        _assess_routing(task, result)
        print(f"       {result.routing_note}")
        results.append(result)

    print("\n" + "═" * 70)
    print(f"  {len(DEMO_TASKS)} tasks | {sum(1 for r in results if r.success)} succeeded | "
          f"{sum(1 for r in results if r.routing_correct)} routing correct")
    print(f"  Total: {total_tokens:,} tokens | ${total_cost:.4f}")
    print(f"  Cost by phase: " + " | ".join(f"{p}: ${c:.4f}" for p, c in phase_costs.items()))
    print("═" * 70)
    return results


def generate_report(results: list[TaskResult], pressure: float, dry_run: bool) -> str:
    now = time.strftime("%Y-%m-%d %H:%M")
    mode = "DRY RUN" if dry_run else "LIVE"
    pressure_str = f"{pressure:.0%}" if pressure > 0 else "0% (default)"

    succeeded = [r for r in results if r.success]
    routing_correct = [r for r in results if r.routing_correct]
    total_cost = sum(r.cost_usd for r in results)
    total_tokens = sum(r.input_tokens + r.output_tokens for r in results)
    avg_latency = sum(r.latency_ms for r in succeeded) / len(succeeded) if succeeded else 0

    # Tiers breakdown
    tier_stats: dict[str, dict] = {}
    for r in results:
        if r.tier not in tier_stats:
            tier_stats[r.tier] = {"total": 0, "ok": 0, "cost": 0.0, "latency": []}
        tier_stats[r.tier]["total"] += 1
        if r.routing_correct:
            tier_stats[r.tier]["ok"] += 1
        tier_stats[r.tier]["cost"] += r.cost_usd
        if r.success:
            tier_stats[r.tier]["latency"].append(r.latency_ms)

    lines = [
        f"# LLM Router Demo 2 — SaaS Builder Report",
        f"",
        f"**Date**: {now}  |  **Mode**: {mode}  |  **Pressure**: {pressure_str}",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Tasks | {len(results)} across 5 phases |",
        f"| Succeeded | {len(succeeded)}/{len(results)} |",
        f"| Routing correct | {len(routing_correct)}/{len(succeeded)} |",
        f"| Total cost | ${total_cost:.5f} |",
        f"| Total tokens | {total_tokens:,} |",
        f"| Avg latency | {avg_latency:.0f}ms |",
        f"",
        f"## Results by Tier",
        f"",
        f"| Tier | Tasks | Routing OK | Total Cost | Avg Latency |",
        f"|------|-------|-----------|------------|-------------|",
    ]
    for tier, s in sorted(tier_stats.items()):
        avg_lat = sum(s["latency"]) / len(s["latency"]) if s["latency"] else 0
        lines.append(f"| {tier} | {s['total']} | {s['ok']}/{s['total']} | ${s['cost']:.5f} | {avg_lat:.0f}ms |")

    lines += ["", "## Per-Task Results", ""]
    for r in results:
        status = "✅" if r.routing_correct else ("❌" if not r.success else "⚠️")
        lines.append(
            f"| {r.task_id} | {r.phase} | {r.tier} | {r.task_type} | "
            f"`{r.model_used or 'N/A'}` | {r.latency_ms:.0f}ms | ${r.cost_usd:.5f} | {status} {r.routing_note} |"
        )

    lines = lines[:lines.index("## Per-Task Results") + 2] + [
        "| ID | Phase | Tier | Type | Model | Latency | Cost | Routing |",
        "|---|---|---|---|---|---|---|---|",
    ] + lines[lines.index("## Per-Task Results") + 2:]

    lines += [
        "",
        "## What Went Right",
        "",
    ]
    right = [r for r in results if r.routing_correct and r.success]
    for r in right:
        lines.append(f"- **{r.task_id} ({r.tier}/{r.task_type})**: {r.routing_note}")

    lines += ["", "## What Went Wrong", ""]
    wrong = [r for r in results if not r.routing_correct or not r.success]
    if wrong:
        for r in wrong:
            if not r.success:
                lines.append(f"- **{r.task_id}**: FAILED — `{r.error}`")
            else:
                lines.append(f"- **{r.task_id} ({r.tier}/{r.task_type})**: {r.routing_note}")
    else:
        lines.append("- All tasks routed correctly! 🎉")

    lines += [
        "",
        "## Cost Efficiency Analysis",
        "",
        f"Total cost: **${total_cost:.5f}** for {len(results)} diverse tasks",
        "",
        "Estimated cost if all tasks ran on Claude Opus:",
        f"- ~$0.26/call × {len(results)} calls = **~${0.26 * len(results):.2f}**",
        f"- Savings: **~${max(0, 0.26 * len(results) - total_cost):.2f}** ({max(0, (1 - total_cost / max(0.01, 0.26 * len(results))) * 100):.0f}% reduction)",
    ]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Router SaaS builder demo")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pressure", type=float, default=0.0)
    parser.add_argument("--report", type=str, default="demo/output/saas_builder_report.md")
    args = parser.parse_args()

    results = asyncio.run(run_demo(dry_run=args.dry_run, pressure=args.pressure))

    report = generate_report(results, pressure=args.pressure, dry_run=args.dry_run)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    print(f"\n📄 Report: {report_path}")

    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(
        [{k: v for k, v in r.__dict__.items()} for r in results], indent=2
    ))
    print(f"📊 JSON:   {json_path}")


if __name__ == "__main__":
    main()
