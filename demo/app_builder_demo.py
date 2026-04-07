#!/usr/bin/env python3
"""
LLM Router Demo — "Building a Todo App with AI"
================================================
Simulates a developer building a web app by sending realistic tasks through
the router at all 3 complexity tiers. Each task exercises a different
routing path and records the result for the final report.

Usage:
    cd /Users/yali.pollak/Projects/llm-router
    uv run python demo/app_builder_demo.py

    # Dry run (no real API calls, use mock responses):
    uv run python demo/app_builder_demo.py --dry-run

    # Simulate high Claude quota pressure (forces external models):
    uv run python demo/app_builder_demo.py --pressure 0.90
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Make sure the src package is on the path when running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType

# ─────────────────────────────────────────────────────────────────────────────
# Task definitions — the "developer's session"
# ─────────────────────────────────────────────────────────────────────────────

DEMO_TASKS: list[dict] = [
    # ── Tier 1: BUDGET ────────────────────────────────────────────────────
    {
        "id": "T1",
        "tier": "BUDGET",
        "profile": RoutingProfile.BUDGET,
        "task_type": TaskType.QUERY,
        "name": "Quick concept check",
        "prompt": (
            "In one sentence: what is the difference between a REST API and a "
            "GraphQL API? I need to decide which to use for a simple todo app."
        ),
        "expected_model_hint": "haiku / gemini-flash",
    },
    {
        "id": "T2",
        "tier": "BUDGET",
        "profile": RoutingProfile.BUDGET,
        "task_type": TaskType.GENERATE,
        "name": "Simple README draft",
        "prompt": (
            "Write a one-paragraph README intro for a FastAPI todo app. "
            "Mention: Python, FastAPI, SQLite, REST endpoints. Keep it casual."
        ),
        "expected_model_hint": "haiku / gemini-flash",
    },

    # ── Tier 2: BALANCED ──────────────────────────────────────────────────
    {
        "id": "T3",
        "tier": "BALANCED",
        "profile": RoutingProfile.BALANCED,
        "task_type": TaskType.CODE,
        "name": "CRUD API implementation",
        "prompt": (
            "Write a FastAPI app with SQLite that implements:\n"
            "  GET /todos — list all\n"
            "  POST /todos — create {title, done=false}\n"
            "  PATCH /todos/{id} — update done status\n"
            "  DELETE /todos/{id} — delete\n\n"
            "Use Pydantic models, async SQLite with aiosqlite. "
            "Return clean, runnable Python code only."
        ),
        "expected_model_hint": "claude-sonnet / gpt-4o",
    },
    {
        "id": "T4",
        "tier": "BALANCED",
        "profile": RoutingProfile.BALANCED,
        "task_type": TaskType.ANALYZE,
        "name": "Code review of the implementation",
        "prompt": (
            "Review this FastAPI todo endpoint for potential issues:\n\n"
            "@app.delete('/todos/{todo_id}')\n"
            "async def delete_todo(todo_id: int):\n"
            "    async with aiosqlite.connect('todos.db') as db:\n"
            "        await db.execute('DELETE FROM todos WHERE id=?', (todo_id,))\n"
            "        await db.commit()\n"
            "    return {'ok': True}\n\n"
            "List bugs, missing validations, and security issues. Be concise."
        ),
        "expected_model_hint": "claude-sonnet / gpt-4o",
    },
    {
        "id": "T5",
        "tier": "BALANCED",
        "profile": RoutingProfile.BALANCED,
        "task_type": TaskType.RESEARCH,
        "name": "Research: latest FastAPI best practices",
        "prompt": (
            "What are the recommended best practices for FastAPI apps in 2025? "
            "Focus on: async SQLite vs PostgreSQL, auth patterns, testing. "
            "Give 3-5 specific, current recommendations."
        ),
        "expected_model_hint": "perplexity/sonar (web-grounded, always first for RESEARCH)",
    },

    # ── Tier 3: PREMIUM ───────────────────────────────────────────────────
    {
        "id": "T6",
        "tier": "PREMIUM",
        "profile": RoutingProfile.PREMIUM,
        "task_type": TaskType.ANALYZE,
        "name": "Architecture decision: SQLite vs PostgreSQL",
        "prompt": (
            "I'm building a todo app that might scale to 10k users. "
            "Deep analysis of: SQLite vs PostgreSQL for this use case. "
            "Consider: deployment complexity, hosting cost, latency at scale, "
            "connection pooling, migration ergonomics, and when to switch. "
            "Give a concrete recommendation with trade-off table."
        ),
        "expected_model_hint": "claude-opus / claude-sonnet (premium analysis)",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    task_id: str
    tier: str
    task_name: str
    task_type: str
    expected_model_hint: str
    profile: str = ""
    # Outcome
    success: bool = False
    model_used: str = ""
    provider_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    response_preview: str = ""
    error: str = ""
    # Routing assessment
    routing_correct: bool = False
    routing_note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Mock mode (no real API calls)
# ─────────────────────────────────────────────────────────────────────────────

MOCK_RESPONSES: dict[str, LLMResponse] = {}

def _make_mock_response(model: str, provider: str, latency: float = 500.0) -> LLMResponse:
    return LLMResponse(
        content="[MOCK RESPONSE] This is a simulated response for demo purposes.",
        model=model,
        input_tokens=120,
        output_tokens=80,
        cost_usd=0.0002,
        latency_ms=latency,
        provider=provider,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routing correctness checks
# ─────────────────────────────────────────────────────────────────────────────

def _assess_routing(task: dict, result: TaskResult) -> None:
    """Fill routing_correct and routing_note based on what model was actually used."""
    model = result.model_used.lower()
    provider = result.provider_used.lower()
    tier = task["tier"]
    task_type = task["task_type"]

    if not result.success:
        result.routing_note = "SKIP — task failed"
        return

    # RESEARCH: Perplexity must be first
    if task_type == TaskType.RESEARCH:
        if "perplexity" in model or "perplexity" in provider:
            result.routing_correct = True
            result.routing_note = "✅ Perplexity used for RESEARCH (correct)"
        else:
            result.routing_correct = False
            result.routing_note = f"⚠️  Expected Perplexity for RESEARCH, got {result.model_used}"
        return

    # BUDGET: should use Haiku or cheap model (not Opus/Sonnet)
    if tier == "BUDGET":
        cheap_models = {"haiku", "flash", "flash-lite", "groq", "deepseek", "mini", "ollama"}
        expensive_models = {"opus", "sonnet", "gpt-4o", "o3", "gemini-2.5-pro"}
        is_cheap = any(m in model for m in cheap_models)
        is_expensive = any(m in model for m in expensive_models)

        if is_cheap and not is_expensive:
            result.routing_correct = True
            result.routing_note = f"✅ Cheap model used for BUDGET tier ({result.model_used})"
        elif is_expensive:
            result.routing_correct = False
            result.routing_note = f"⚠️  Expensive model used for BUDGET: {result.model_used} (over-routed)"
        else:
            result.routing_correct = True  # unknown model, give benefit of doubt
            result.routing_note = f"ℹ️  Unknown tier for: {result.model_used}"
        return

    # BALANCED: should use Sonnet-class or GPT-4o (not Opus, not pure Haiku)
    if tier == "BALANCED":
        good_models = {"sonnet", "gpt-4o", "gemini-2.5-pro", "codex", "mistral-large"}
        if any(m in model for m in good_models):
            result.routing_correct = True
            result.routing_note = f"✅ Mid-tier model for BALANCED ({result.model_used})"
        elif "opus" in model:
            result.routing_correct = False
            result.routing_note = f"⚠️  Over-routed to Opus for BALANCED: {result.model_used}"
        elif any(m in model for m in {"haiku", "flash", "mini"}):
            result.routing_correct = False
            result.routing_note = f"⚠️  Under-routed to cheap model for BALANCED: {result.model_used}"
        else:
            result.routing_correct = True
            result.routing_note = f"ℹ️  Model: {result.model_used}"
        return

    # PREMIUM: should use Opus or best available
    if tier == "PREMIUM":
        premium_models = {"opus", "o3", "gemini-2.5-pro", "gpt-4o", "sonnet"}
        if any(m in model for m in premium_models):
            result.routing_correct = True
            result.routing_note = f"✅ Premium model for PREMIUM tier ({result.model_used})"
        else:
            result.routing_correct = False
            result.routing_note = f"⚠️  Expected premium model, got {result.model_used}"
        return

    result.routing_note = f"ℹ️  Unclassified tier: {tier}"


# ─────────────────────────────────────────────────────────────────────────────
# Demo runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_demo(dry_run: bool = False, pressure: float = 0.0) -> list[TaskResult]:
    """Execute all demo tasks and return results."""

    # Set simulated pressure (affects model chain reordering)
    if pressure > 0:
        try:
            from llm_router.claude_usage import set_claude_pressure
            set_claude_pressure(pressure)
            print(f"  ⚡ Simulated Claude pressure: {pressure:.0%}")
        except Exception as e:
            print(f"  ⚠️  Could not set pressure: {e}")

    results: list[TaskResult] = []
    total_cost = 0.0
    total_tokens = 0

    print("\n" + "═" * 65)
    print("  LLM Router Demo — Building a Todo App")
    print("  Mode:", "DRY RUN (mock responses)" if dry_run else "LIVE (real API calls)")
    print("═" * 65)

    for i, task in enumerate(DEMO_TASKS, 1):
        result = TaskResult(
            task_id=task["id"],
            tier=task["tier"],
            task_name=task["name"],
            task_type=task["task_type"].value,
            expected_model_hint=task["expected_model_hint"],
            profile=task["profile"].value,
        )

        # Print task header
        print(f"\n[{task['id']}] {task['tier']} — {task['name']}")
        print(f"     Task type : {task['task_type'].value}")
        print(f"     Profile   : {task['profile'].value}")
        print(f"     Expected  : {task['expected_model_hint']}")
        print(f"     Prompt    : {task['prompt'][:80]}{'...' if len(task['prompt']) > 80 else ''}")
        print("     " + "─" * 55)

        t0 = time.time()
        try:
            if dry_run:
                # Resolve model chain without calling the API
                from llm_router.profiles import get_model_chain
                chain = get_model_chain(task["profile"], task["task_type"])
                first_model = chain[0] if chain else "unknown"
                provider = first_model.split("/")[0] if "/" in first_model else "unknown"
                resp = _make_mock_response(first_model, provider)
            else:
                resp = await route_and_call(
                    task["task_type"],
                    task["prompt"],
                    profile=task["profile"],
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

            print(f"     ✓ Model     : {resp.model}")
            print(f"       Provider  : {resp.provider}")
            print(f"       Tokens    : {resp.input_tokens} in / {resp.output_tokens} out")
            print(f"       Cost      : ${resp.cost_usd:.6f}")
            print(f"       Latency   : {result.latency_ms:.0f}ms")
            if dry_run:
                print(f"       Chain     : {' → '.join(get_model_chain(task['profile'], task['task_type'])[:3])}...")

        except Exception as e:
            elapsed_ms = (time.time() - t0) * 1000
            result.success = False
            result.error = str(e)
            print(f"     ✗ FAILED   : {e}")

        _assess_routing(task, result)
        print(f"     Routing   : {result.routing_note}")
        results.append(result)

    print("\n" + "═" * 65)
    print(f"  Session total: {total_tokens:,} tokens  |  ${total_cost:.4f} cost")
    print("═" * 65)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_report(results: list[TaskResult], pressure: float, dry_run: bool) -> str:
    """Generate a markdown report from demo results."""
    now = time.strftime("%Y-%m-%d %H:%M")
    mode = "DRY RUN" if dry_run else "LIVE"
    pressure_str = f"{pressure:.0%}" if pressure > 0 else "0% (default)"

    total_tasks = len(results)
    succeeded = sum(1 for r in results if r.success)
    routing_correct = sum(1 for r in results if r.routing_correct)
    total_cost = sum(r.cost_usd for r in results)
    total_tokens = sum(r.input_tokens + r.output_tokens for r in results)
    avg_latency = (
        sum(r.latency_ms for r in results if r.success) / succeeded
        if succeeded else 0
    )

    lines = [
        f"# LLM Router Demo Report",
        f"",
        f"**Date**: {now}  |  **Mode**: {mode}  |  **Pressure**: {pressure_str}",
        f"",
        f"## Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Tasks run | {total_tasks} |",
        f"| Succeeded | {succeeded}/{total_tasks} |",
        f"| Routing correct | {routing_correct}/{succeeded} |",
        f"| Total cost | ${total_cost:.6f} |",
        f"| Total tokens | {total_tokens:,} |",
        f"| Avg latency | {avg_latency:.0f}ms |",
        f"",
        f"## Task Results",
        f"",
    ]

    for r in results:
        status_icon = "✅" if r.success else "❌"
        routing_icon = "✅" if r.routing_correct else ("⚠️" if r.success else "—")
        lines += [
            f"### {r.task_id}: {r.task_name}",
            f"",
            f"- **Tier**: {r.tier}  |  **Type**: {r.task_type}  |  **Profile**: {r.profile}",
            f"- **Expected model**: {r.expected_model_hint}",
            f"- **Status**: {status_icon} {'Succeeded' if r.success else 'Failed'}",
        ]
        if r.success:
            lines += [
                f"- **Model used**: `{r.model_used}`",
                f"- **Tokens**: {r.input_tokens} in / {r.output_tokens} out",
                f"- **Cost**: ${r.cost_usd:.6f}  |  **Latency**: {r.latency_ms:.0f}ms",
            ]
        else:
            lines.append(f"- **Error**: `{r.error}`")
        lines += [
            f"- **Routing**: {routing_icon} {r.routing_note}",
            f"",
        ]
        if r.response_preview and not dry_run:
            preview = r.response_preview.replace("\n", " ")[:150]
            lines += [
                f"<details><summary>Response preview</summary>",
                f"",
                f"> {preview}...",
                f"",
                f"</details>",
                f"",
            ]

    # What went right / wrong
    lines += ["## What Went Right", ""]
    right = [r for r in results if r.routing_correct and r.success]
    if right:
        for r in right:
            lines.append(f"- **{r.task_id} ({r.tier})**: {r.routing_note}")
    else:
        lines.append("- (no tasks routed correctly)")
    lines.append("")

    lines += ["## What Went Wrong / Needs Fixing", ""]
    wrong = [r for r in results if not r.routing_correct or not r.success]
    if wrong:
        for r in wrong:
            if not r.success:
                lines.append(f"- **{r.task_id} ({r.tier})**: FAILED — `{r.error}`")
            else:
                lines.append(f"- **{r.task_id} ({r.tier})**: {r.routing_note}")
    else:
        lines.append("- All tasks routed correctly! 🎉")
    lines.append("")

    lines += [
        "## Routing Chain Inspection (dry-run only)",
        "",
        "Chains shown are what the router *would* use at current pressure:",
        "",
    ]
    if dry_run:
        from llm_router.profiles import get_model_chain
        for task in DEMO_TASKS:
            chain = get_model_chain(task["profile"], task["task_type"])
            chain_str = " → ".join(chain[:5])
            if len(chain) > 5:
                chain_str += f" (+{len(chain)-5} more)"
            lines.append(f"- **{task['id']} ({task['tier']}/{task['task_type'].value})**: `{chain_str}`")
        lines.append("")

    lines += [
        "## Next Steps",
        "",
        "Based on these results, refinements needed:",
        "",
        "1. [ ] Any BUDGET tasks using expensive models → check pressure logic",
        "2. [ ] Any BALANCED tasks under/over-routing → adjust chain ordering",
        "3. [ ] Any RESEARCH tasks not using Perplexity → check RESEARCH static chain",
        "4. [ ] Any failures → check API keys, model availability, error types",
        "5. [ ] Compare dry-run chains vs live model used → verify no unexpected fallback",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Router app-builder demo")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve model chains without making real API calls"
    )
    parser.add_argument(
        "--pressure", type=float, default=0.0,
        help="Simulated Claude subscription pressure (0.0–1.0). Try 0.90 to see high-pressure routing."
    )
    parser.add_argument(
        "--report", type=str, default="demo/output/app_builder_report.md",
        help="Path to write the markdown report (default: demo/output/app_builder_report.md)"
    )
    args = parser.parse_args()

    results = asyncio.run(run_demo(dry_run=args.dry_run, pressure=args.pressure))

    report = generate_report(results, pressure=args.pressure, dry_run=args.dry_run)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    print(f"\n📄 Report written to: {report_path}")

    # Also dump raw JSON for programmatic inspection
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(
        [
            {
                "task_id": r.task_id, "tier": r.tier, "task_name": r.task_name,
                "task_type": r.task_type, "profile": r.profile,
                "success": r.success, "model_used": r.model_used,
                "provider_used": r.provider_used, "cost_usd": r.cost_usd,
                "latency_ms": r.latency_ms, "routing_correct": r.routing_correct,
                "routing_note": r.routing_note, "error": r.error,
            }
            for r in results
        ],
        indent=2,
    ))
    print(f"📊 Raw JSON: {json_path}")


if __name__ == "__main__":
    main()
