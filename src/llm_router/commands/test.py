"""Test command — dry-run route simulation."""

from __future__ import annotations

import asyncio
import os
import sys


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_test(args: list[str]) -> int:
    """Entry point for test command."""
    prompt = " ".join(args)
    _run_test(prompt)
    return 0


# ── Implementation ─────────────────────────────────────────────────────────────

def _run_test(prompt: str) -> None:
    """Dry-run route simulation: classify prompt + show model choice + cost estimate."""
    if not prompt:
        print(_red("Usage: llm-router test \"<your prompt>\""))
        sys.exit(1)

    async def _simulate() -> None:
        from llm_router.classifier import classify_complexity
        from llm_router.types import MODEL_COST_PER_1K

        # Baseline: claude-sonnet-4-5 (what a non-routing user would pay)
        BASELINE = "claude-sonnet-4-5"
        BASELINE_IN = 3.0   # $/M tokens
        BASELINE_OUT = 15.0

        # Estimate token counts from prompt length (rough: 1 token ≈ 4 chars)
        est_in = max(50, len(prompt) // 4)
        est_out = 300  # typical completion

        result = await classify_complexity(prompt)
        complexity = result.complexity.value
        confidence = result.confidence
        task = result.inferred_task_type.value if result.inferred_task_type else "unknown"
        method = result.classifier_model or "heuristic"

        # Map complexity → cheapest appropriate model (mirrors router.py logic)
        complexity_model_map = {
            "simple": "claude-haiku-4-5-20251001",
            "moderate": "claude-sonnet-4-6",
            "complex": "claude-opus-4-6",
            "deep_reasoning": "claude-opus-4-6",
        }
        chosen = complexity_model_map.get(complexity, BASELINE)

        # Cost estimate for chosen model
        costs = MODEL_COST_PER_1K.get(chosen, {})
        in_rate = costs.get("input", 0.0) * 1000   # convert /1k to /M
        out_rate = costs.get("output", 0.0) * 1000
        chosen_cost = (est_in * in_rate + est_out * out_rate) / 1_000_000
        baseline_cost = (est_in * BASELINE_IN + est_out * BASELINE_OUT) / 1_000_000
        saved = max(0.0, baseline_cost - chosen_cost)
        savings_pct = round(saved / baseline_cost * 100) if baseline_cost > 0 else 0

        W = 56
        HR = "+" + "-" * W + "+"
        def row(text: str) -> str:
            return f"| {text:<{W - 1}}|"
        def section(title: str) -> str:
            return "|" + f" {title} ".center(W, "-") + "|"

        print()
        print(HR)
        print("|" + " Route Simulation (dry run) ".center(W) + "|")
        print(HR)
        print(row(f"  Prompt:      {prompt[:42]}{'…' if len(prompt) > 42 else ''}"))
        print(row(""))
        print(section("CLASSIFICATION"))
        print(row(f"  Task type:   {task}"))
        print(row(f"  Complexity:  {complexity}"))
        print(row(f"  Confidence:  {confidence:.0%}  (via {method})"))
        print(HR)
        print(section("ROUTING DECISION"))
        print(row(f"  Chosen:      {_green(chosen)}"))
        print(row(f"  Baseline:    {BASELINE}"))
        print(HR)
        print(section("COST ESTIMATE  (~{est_in}t in / {est_out}t out)".format(
            est_in=est_in, est_out=est_out)))
        print(row(f"  Chosen cost: ${chosen_cost:.5f}"))
        print(row(f"  Baseline:    ${baseline_cost:.5f}"))
        if saved > 0:
            print(row(f"  Saved:       {_green(f'${saved:.5f}  ({savings_pct}% cheaper)')}"))
        else:
            print(row("  Saved:       —"))
        print(HR)
        print()

    asyncio.run(_simulate())
