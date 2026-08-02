"""Plan 06 sub_10 benchmark — OpenRouter-pinned variant.

The standard run_sub10.py path goes through `route_and_call`, but Codex
injection (always-on in subscription mode) preempts OpenRouter in the
chain — every prompt ends up routed to codex/gpt-5.4 at $0 cost. That's
a valid llm-router output (subscription users get free routing), but it
doesn't exercise the routerarena_tuned workhorse pool the policy was
designed for.

This script picks the active model per prompt via the same
predict_head_model logic policy_diff uses, then calls it directly via
litellm so the OpenRouter workhorses actually see traffic. It still
exercises Plan 07 Cat D.4 OpenRouterQuirks (anthropic/ rename +
max_tokens cap via the transform_request hook) by passing through
providers.call_llm.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

os.environ.setdefault("LLM_ROUTER_GATES", "off")
os.environ.setdefault("LLM_ROUTER_POLICY", "routerarena_tuned")

VERSION = os.environ.get("LLM_BENCH_VERSION", "v1.28.0-ra-tuned-openrouter")
LIMIT = int(os.environ.get("LLM_BENCH_LIMIT", "30"))
SPLIT = os.environ.get("LLM_BENCH_SPLIT", "sub_10_shuffled")

DATA = Path.home() / ".llm-router" / "data" / "routerarena" / f"{SPLIT}.jsonl"
PRED_OUT = DATA.parent / f"{SPLIT}_predictions_{VERSION}.jsonl"


async def main() -> None:
    from llm_router.benchmark import Prediction
    from llm_router.benchmark.runners.routerarena import RouterArenaRunner
    from llm_router.policy import get_policy_manager
    from llm_router.policy_diff import predict_head_model
    from llm_router.providers import call_llm

    get_policy_manager().set_active_policy("routerarena_tuned")
    policy = get_policy_manager().get_active_policy()

    runner = RouterArenaRunner()
    dataset = runner.load_dataset(SPLIT)
    if LIMIT > 0:
        dataset = dataset[:LIMIT]

    print(f"Policy: {policy.name}   Dataset: {len(dataset)} prompts   Version: {VERSION}")
    print(f"Writing predictions to: {PRED_OUT}")

    predictions: list[Prediction] = []
    started = time.monotonic()
    with PRED_OUT.open("w") as f:
        for i, prompt in enumerate(dataset, start=1):
            model = predict_head_model(policy, prompt.subject or "general")
            if model == "<unconfigured>":
                model = policy.workhorses[1]  # skip Ollama at index 0
            # Force OpenRouter slot — replace any non-OpenRouter pick with the
            # second workhorse (also OpenRouter in this policy)
            if not model.startswith("openrouter/"):
                # Use first OpenRouter model from workhorses
                or_models = [m for m in policy.workhorses if m.startswith("openrouter/")]
                if or_models:
                    model = or_models[0]
                else:
                    print(f"  {i}/{len(dataset)} SKIPPING — no openrouter model available")
                    continue

            t0 = time.monotonic()
            try:
                resp = await call_llm(
                    model=model,
                    messages=[{"role": "user", "content": prompt.text}],
                    max_tokens=256,
                    temperature=0.0,
                )
                pred = Prediction(
                    prompt_id=prompt.id,
                    model=resp.model,
                    response=resp.content,
                    cost_usd=resp.cost_usd,
                    latency_ms=resp.latency_ms,
                )
            except Exception as err:  # noqa: BLE001
                pred = Prediction(
                    prompt_id=prompt.id,
                    model=f"{model}<error>",
                    response="",
                    cost_usd=0.0,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    metadata={"error": str(err)[:200]},
                )

            predictions.append(pred)
            row = {
                "id": pred.prompt_id,
                "subject": prompt.subject,
                "reference": prompt.reference,
                "model": pred.model,
                "response": pred.response[:200],
                "cost_usd": pred.cost_usd,
                "latency_ms": pred.latency_ms,
                "dataset": prompt.metadata.get("dataset"),
                "difficulty": prompt.metadata.get("difficulty"),
            }
            f.write(json.dumps(row) + "\n")
            f.flush()
            correct = "✓" if pred.response.strip().upper().startswith(
                (prompt.reference or "").upper()
            ) else "✗"
            print(f"  {i:>3}/{len(dataset)}  {prompt.subject:<10} {pred.model.split('/')[-1]:<35} {pred.latency_ms/1000:5.1f}s  ${pred.cost_usd:.5f}  {correct}",
                  flush=True)

    elapsed = time.monotonic() - started

    # Score
    result = runner.evaluate(predictions, dataset)
    print(f"\n=== Result ===")
    print(f"Accuracy (strict exact-match): {result.score:.4f}  n={result.n_samples}")

    # Lenient eval: response starts with reference letter
    n_total = n_correct = 0
    by_subj: dict[str, list[int]] = {}
    for pred, prompt in zip(predictions, dataset):
        ref = (prompt.reference or "").strip().upper()
        if not ref: continue
        n_total += 1
        ok = pred.response.strip().upper().startswith(ref)
        if ok: n_correct += 1
        slot = by_subj.setdefault(prompt.subject or "general", [0, 0])
        slot[1] += 1
        if ok: slot[0] += 1

    accuracy = n_correct / max(n_total, 1)
    total_cost = sum(p.cost_usd for p in predictions)
    cost_per_1k = total_cost * (1000 / max(n_total, 1))

    print(f"Accuracy (lenient letter-prefix): {n_correct}/{n_total} = {accuracy:.4f}")
    print(f"Total cost: ${total_cost:.4f}    Cost/1K prompts: ${cost_per_1k:.4f}")
    print(f"Avg latency: {elapsed/max(n_total,1):.2f}s")
    print()
    print("Per subject:")
    for s, (c, t) in sorted(by_subj.items(), key=lambda x: -x[1][1]):
        print(f"  {s:<10} {c}/{t} = {c/t:.4f}")

    # Arena Score approximation: accuracy - 0.06 * (cost/1K)
    # (Per Plan 06 leaderboard back-calculation: Sqwish 76.4%-1.13%=75.27 for $0.18/1K)
    arena = accuracy - 0.06 * cost_per_1k
    print(f"\nArena Score (approx, accuracy - 0.06×cost/1K): {arena:.4f}")
    print(f"Compare:")
    print(f"  Sqwish     0.7527 (#1)")
    print(f"  llm-router {arena:.4f}  ({'BEATS #1!' if arena > 0.7527 else 'below #1'})")

    # Persist
    from llm_router.benchmark.regression import store_result
    await store_result(
        version=VERSION,
        policy="routerarena_tuned_openrouter",
        benchmark="routerarena",
        split=SPLIT,
        score=accuracy,
        n_samples=n_total,
        per_subject={s: c/t for s, (c, t) in by_subj.items()},
    )
    print(f"\nPersisted as {VERSION}.")


if __name__ == "__main__":
    asyncio.run(main())
