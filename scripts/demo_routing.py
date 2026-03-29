"""Live demo of the smart routing system — classification, budget pressure, model selection, and savings."""

from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from llm_router.classifier import classify_complexity
from llm_router.cost import calc_savings, log_claude_usage, get_savings_summary
from llm_router.model_selector import select_model
from llm_router.types import MODEL_COST_PER_1K, QualityMode


PROMPTS = [
    ("What is 2+2?", "Trivial math"),
    ("Write a REST API with auth, pagination, and rate limiting", "Moderate coding"),
    ("Design a distributed event-sourcing CQRS architecture with exactly-once delivery guarantees", "Complex architecture"),
    ("Translate 'hello' to French", "Simple translation"),
    ("Analyze the trade-offs between microservices and monoliths for a fintech startup processing 10M transactions/day", "Complex analysis"),
    ("List 3 Python web frameworks", "Simple factual"),
    ("Implement a lock-free concurrent skip list in Rust with epoch-based garbage collection", "Complex algorithm"),
]

BUDGET_SCENARIOS = [0.0, 0.30, 0.55, 0.70, 0.85, 0.96]

QUALITY_MODES = [
    (QualityMode.BEST, "best"),
    (QualityMode.BALANCED, "balanced"),
    (QualityMode.CONSERVE, "conserve"),
]

# Simulated token counts per complexity (realistic for each task type)
SIMULATED_TOKENS = {
    "simple": 500,
    "moderate": 5_000,
    "complex": 15_000,
}

SEP = "\u2500" * 70


def print_header(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}\n")


async def test_classification() -> list:
    """TEST 1: Classify all prompts via real LLM call."""
    print_header("TEST 1: Real-Time Classification (via LLM)")

    results = []
    for prompt, label in PROMPTS:
        result = await classify_complexity(prompt)
        icon = {"simple": "\U0001f7e2", "moderate": "\U0001f7e1", "complex": "\U0001f534"}
        emoji = icon.get(result.complexity.value, "\u2753")

        print(f"  {emoji} [{result.complexity.value:>8}] {result.confidence:.0%}  {label}")
        print(f"     task_type={result.inferred_task_type or 'none':>10}  "
              f"model={result.classifier_model}  "
              f"cost=${result.classifier_cost_usd:.4f}  "
              f"latency={result.classifier_latency_ms:.0f}ms")
        print(f"     reasoning: {result.reasoning}")
        print()
        results.append(result)

    return results


def test_budget_pressure(results: list) -> None:
    """TEST 2: Show how budget pressure downshifts each classification."""
    print_header("TEST 2: Budget Pressure Downshift Matrix")

    print(f"  {'Prompt':<45} ", end="")
    for pct in BUDGET_SCENARIOS:
        print(f"  {pct:>5.0%}", end="")
    print()
    print(f"  {SEP}")

    for (prompt, label), result in zip(PROMPTS, results):
        short = label[:44]
        print(f"  {short:<45} ", end="")
        for pct in BUDGET_SCENARIOS:
            rec = select_model(result, budget_pct_used=pct)
            marker = "*" if rec.was_downshifted else " "
            print(f"  {rec.recommended_model:>5}{marker}", end="")
        print()

    print(f"\n  * = downshifted from ideal model")


def test_quality_modes(results: list) -> None:
    """TEST 3: Show quality mode effects on each classification."""
    print_header("TEST 3: Quality Mode Comparison")

    print(f"  {'Prompt':<45}  {'best':>8}  {'balanced':>8}  {'conserve':>8}")
    print(f"  {SEP}")

    for (prompt, label), result in zip(PROMPTS, results):
        short = label[:44]
        models = []
        for mode, _ in QUALITY_MODES:
            rec = select_model(result, budget_pct_used=0, quality_mode=mode)
            models.append(rec.recommended_model)
        print(f"  {short:<45}  {models[0]:>8}  {models[1]:>8}  {models[2]:>8}")


def test_min_model_floor(results: list) -> None:
    """TEST 4: Show min_model floor enforcement."""
    print_header("TEST 4: Min Model Floor Enforcement")

    floors = ["haiku", "sonnet", "opus"]
    print(f"  {'Prompt':<45}  {'floor=haiku':>12}  {'floor=sonnet':>13}  {'floor=opus':>11}")
    print(f"  {SEP}")

    for (prompt, label), result in zip(PROMPTS, results):
        short = label[:44]
        models = []
        for floor in floors:
            rec = select_model(result, budget_pct_used=0.85, min_model=floor)
            models.append(rec.recommended_model)
        print(f"  {short:<45}  {models[0]:>12}  {models[1]:>13}  {models[2]:>11}")

    print(f"\n  (all tested at 85% budget pressure to show floor vs downshift interaction)")


def test_recommendation_headers(results: list) -> None:
    """TEST 5: Show full recommendation headers with budget bars."""
    print_header("TEST 5: Full Recommendation Headers")

    scenarios = [
        (0.20, QualityMode.BALANCED, "haiku", "Fresh budget, balanced"),
        (0.65, QualityMode.BALANCED, "haiku", "Mid-day pressure, balanced"),
        (0.90, QualityMode.BALANCED, "sonnet", "High pressure, floor=sonnet"),
        (0.98, QualityMode.BALANCED, "haiku", "Near-exhausted budget"),
        (0.10, QualityMode.BEST, "haiku", "Fresh budget, best mode"),
        (0.75, QualityMode.CONSERVE, "haiku", "Mid pressure, conserve mode"),
    ]

    complex_result = results[2]

    for budget, mode, floor, desc in scenarios:
        rec = select_model(complex_result, budget_pct_used=budget, quality_mode=mode, min_model=floor)
        print(f"  Scenario: {desc}")
        print(f"  {rec.header()}")
        print()


async def test_per_call_savings(results: list) -> None:
    """TEST 6: Per-call savings — what each routing decision saves vs opus."""
    print_header("TEST 6: Per-Call Savings (vs Always Using Opus)")

    model_icons = {"haiku": "\U0001f7e1", "sonnet": "\U0001f535", "opus": "\U0001f7e3"}

    print(f"  {'Prompt':<35}  {'Model':>7}  {'Tokens':>7}  {'$ Saved':>8}  {'Time Saved':>10}")
    print(f"  {SEP}")

    total_cost_saved = 0.0
    total_time_saved = 0.0
    total_tokens = 0

    for (prompt, label), result in zip(PROMPTS, results):
        rec = select_model(result, budget_pct_used=0.40)
        tokens = SIMULATED_TOKENS[result.complexity.value]
        cost_saved, time_saved = calc_savings(rec.recommended_model, tokens)

        total_cost_saved += cost_saved
        total_time_saved += time_saved
        total_tokens += tokens

        icon = model_icons.get(rec.recommended_model, "")
        time_str = f"{time_saved:.1f}s" if time_saved > 0 else "-"
        cost_str = f"${cost_saved:.4f}" if cost_saved > 0 else "-"

        print(f"  {label:<35}  {icon} {rec.recommended_model:>5}  {tokens:>6,}  {cost_str:>8}  {time_str:>10}")

    # Summary row
    print(f"  {SEP}")
    opus_cost = (total_tokens / 1000) * MODEL_COST_PER_1K["opus"]
    actual_cost = opus_cost - total_cost_saved
    pct = (total_cost_saved / opus_cost * 100) if opus_cost > 0 else 0

    print(f"  {'TOTAL':<35}  {'':>7}  {total_tokens:>6,}  ${total_cost_saved:>7.4f}  {total_time_saved:>9.1f}s")
    print()
    print(f"  Opus equivalent:  ${opus_cost:.4f}")
    print(f"  Actual cost:      ${actual_cost:.4f}")
    print(f"  Savings:          {pct:.0f}%")


async def test_session_simulation() -> None:
    """TEST 7: Simulate a full work session with cumulative savings tracking."""
    print_header("TEST 7: Full Session Simulation (10 Tasks)")

    # Use in-memory DB for this test
    os.environ["LLM_ROUTER_DB_PATH"] = "/tmp/llm_router_demo.db"
    # Remove old DB if exists
    try:
        os.unlink("/tmp/llm_router_demo.db")
    except FileNotFoundError:
        pass

    session_tasks = [
        ("What's the weather API endpoint format?", 0.05),
        ("Fix the null check in auth middleware", 0.10),
        ("Write a Kafka consumer with exactly-once semantics", 0.20),
        ("What's the Python dict merge syntax?", 0.25),
        ("Refactor the payment module to use Strategy pattern", 0.35),
        ("List the HTTP status codes for redirects", 0.40),
        ("Design a real-time notifications system with WebSockets", 0.55),
        ("What does `git rebase -i` do?", 0.60),
        ("Implement rate limiting with token bucket algorithm", 0.70),
        ("Architect a multi-tenant SaaS data isolation strategy", 0.85),
    ]

    model_icons = {"haiku": "\U0001f7e1", "sonnet": "\U0001f535", "opus": "\U0001f7e3"}
    cumulative_saved = 0.0
    cumulative_time = 0.0

    for i, (prompt, budget_pct) in enumerate(session_tasks, 1):
        result = await classify_complexity(prompt)
        rec = select_model(result, budget_pct_used=budget_pct)

        tokens = SIMULATED_TOKENS[result.complexity.value]
        call_result = await log_claude_usage(rec.recommended_model, tokens, result.complexity.value)

        cumulative_saved += call_result["cost_saved_usd"]
        cumulative_time += call_result["time_saved_sec"]

        icon = model_icons.get(rec.recommended_model, "")
        cost_str = f"+${call_result['cost_saved_usd']:.4f}" if call_result["cost_saved_usd"] > 0 else "     -"
        shift = " \u2b07\ufe0f" if rec.was_downshifted else ""

        short_prompt = prompt[:50]
        print(
            f"  {i:>2}. {icon} {rec.recommended_model:>6}{shift:<3} "
            f"| {result.complexity.value:>8} "
            f"| {tokens:>6,} tok "
            f"| {cost_str} "
            f"| cumul: ${cumulative_saved:.4f}"
        )

    # Final summary from DB
    summary = await get_savings_summary("today")

    print(f"\n  {SEP}")
    print(f"  SESSION COMPLETE")
    print(f"  {SEP}")
    print(f"  Total calls:        {summary['total_calls']}")
    print(f"  Total tokens:       {summary['total_tokens']:,}")

    opus_cost = (summary["total_tokens"] / 1000) * MODEL_COST_PER_1K["opus"]
    actual_cost = opus_cost - summary["cost_saved_usd"]
    pct = (summary["cost_saved_usd"] / opus_cost * 100) if opus_cost > 0 else 0

    print(f"  Opus would cost:    ${opus_cost:.4f}")
    print(f"  Actual cost:        ${actual_cost:.4f}")
    print(f"  \U0001f4b0 Money saved:       ${summary['cost_saved_usd']:.4f} ({pct:.0f}%)")

    mins = summary["time_saved_sec"] / 60
    print(f"  \u23f1\ufe0f  Time saved:        {summary['time_saved_sec']:.1f}s ({mins:.1f} minutes)")

    if summary["by_model"]:
        print(f"\n  By model:")
        for model, data in summary["by_model"].items():
            icon = model_icons.get(model, "")
            print(
                f"    {icon} {model}: {data['calls']} calls, "
                f"{data['tokens']:,} tokens, "
                f"saved ${data['cost_saved']:.4f}"
            )


async def main() -> None:
    print("\n" + "=" * 70)
    print("  LLM ROUTER — LIVE SMART ROUTING DEMO")
    print("  Real LLM classification + budget-aware routing + savings tracking")
    print("=" * 70)

    # TEST 1: Real classification via LLM
    results = await test_classification()

    # TEST 2-5: Model selection (pure logic, no API calls)
    test_budget_pressure(results)
    test_quality_modes(results)
    test_min_model_floor(results)
    test_recommendation_headers(results)

    # TEST 6: Per-call savings
    await test_per_call_savings(results)

    # TEST 7: Full session simulation with DB tracking
    await test_session_simulation()

    # Summary
    print_header("CLASSIFIER SUMMARY")
    total_cost = sum(r.classifier_cost_usd for r in results)
    avg_latency = sum(r.classifier_latency_ms for r in results) / len(results)
    print(f"  Classifications:  {len(results)} prompts (test 1) + 10 prompts (test 7)")
    print(f"  Classifier cost:  ${total_cost:.4f} (test 1 only)")
    print(f"  Avg latency:      {avg_latency:.0f}ms")
    print(f"  Classifier model: {results[0].classifier_model}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
