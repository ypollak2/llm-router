"""Offline benchmark data fetcher — runs in GitHub Actions, never at runtime.

Fetches model rankings from four authoritative sources, computes weighted
per-task-type scores, assigns tier lists, and writes an updated
``data/benchmarks.json``. Run via ``scripts/update_benchmarks.py``.

Sources:
  - LMSYS Chatbot Arena (ELO scores — general quality)
  - Aider Leaderboard (code-specific pass rates)
  - HuggingFace Open LLM Leaderboard (reasoning, math, knowledge)
  - LiteLLM model_cost dict (pricing — already installed as a dependency)

This module requires the ``scripts`` optional dependency group:
  ``pip install claude-code-llm-router[scripts]``
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("llm_router.benchmark_fetcher")

# Path to the bundled benchmarks file (relative to this module).
_OUTPUT_PATH = Path(__file__).parent / "data" / "benchmarks.json"

# Task-type scoring weights: (lmsys, aider, hf, cost_inv).
# cost_inv = 1 - normalized_cost (cheaper → higher score).
_TASK_WEIGHTS: dict[str, tuple[float, float, float, float]] = {
    "code":     (0.30, 0.50, 0.00, 0.20),
    "analyze":  (0.40, 0.00, 0.40, 0.20),
    "query":    (0.40, 0.00, 0.40, 0.20),
    "generate": (0.50, 0.00, 0.30, 0.20),
    "research": (0.60, 0.00, 0.00, 0.40),
}

# Maps source model names → LiteLLM provider/model IDs.
# Extend this table when new frontier models are released.
_ALIASES: dict[str, str] = {
    # LMSYS names
    "Claude Opus 4.6": "anthropic/claude-opus-4-6",
    "Claude Sonnet 4.6": "anthropic/claude-sonnet-4-6",
    "Claude Haiku 4.5": "anthropic/claude-haiku-4-5-20251001",
    "GPT-4o": "openai/gpt-4o",
    "GPT-4o mini": "openai/gpt-4o-mini",
    "o3": "openai/o3",
    "Gemini 2.5 Pro": "gemini/gemini-2.5-pro",
    "Gemini 2.5 Flash": "gemini/gemini-2.5-flash",
    "DeepSeek-V3": "deepseek/deepseek-chat",
    "DeepSeek-R1": "deepseek/deepseek-reasoner",
    "Llama-3.3-70B": "groq/llama-3.3-70b-versatile",
    "Grok-3": "xai/grok-3",
    "Mistral-Large": "mistral/mistral-large-latest",
    "Command R+": "cohere/command-r-plus",
    # Aider names (may differ slightly)
    "claude-opus-4-6": "anthropic/claude-opus-4-6",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4-6",
    "gpt-4o": "openai/gpt-4o",
    "o3": "openai/o3",
    "gemini-2.5-pro": "gemini/gemini-2.5-pro",
    "gemini-2.5-flash": "gemini/gemini-2.5-flash",
    "deepseek-chat": "deepseek/deepseek-chat",
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
}

# Models that are NEVER included in benchmark tiers (local-only, no API benchmarks).
_EXCLUDED_PREFIXES = ("ollama/",)

# Tier boundary: top N per task type get premium, next N get balanced, rest budget.
_PREMIUM_COUNT = 3
_BALANCED_COUNT = 4


def _normalize(values: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a dict of scores to [0.0, 1.0]."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def _invert(values: dict[str, float]) -> dict[str, float]:
    """Invert normalized scores so lower cost → higher cost_inv score."""
    return {k: 1.0 - v for k, v in values.items()}


def _resolve(name: str) -> str | None:
    """Resolve a source model name to a LiteLLM provider/model ID."""
    return _ALIASES.get(name)


def fetch_lmsys() -> dict[str, float]:
    """Fetch LMSYS Chatbot Arena ELO scores.

    Returns:
        Dict mapping provider/model → raw ELO score (un-normalized).
        Empty dict on any network or parse failure.
    """
    try:
        import httpx
        resp = httpx.get(
            "https://huggingface.co/datasets/lmsys/chatbot_arena_leaderboard/resolve/main/elo_results_20240827.json",
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        scores = {}
        # The leaderboard JSON has an "elo_rating_final" dict keyed by model name.
        elo_map = data.get("elo_rating_final", data) if isinstance(data, dict) else {}
        for name, elo in elo_map.items():
            resolved = _resolve(name)
            if resolved and isinstance(elo, (int, float)):
                scores[resolved] = float(elo)
        log.info("LMSYS: fetched %d model scores", len(scores))
        return scores
    except Exception as e:
        log.warning("LMSYS fetch failed: %s — using empty data", e)
        return {}


def fetch_aider() -> dict[str, float]:
    """Fetch Aider code benchmark pass rates.

    Returns:
        Dict mapping provider/model → pass rate (0.0–1.0, already normalized).
        Empty dict on any network or parse failure.
    """
    try:
        import httpx
        resp = httpx.get(
            "https://aider.chat/assets/leaderboard.json",
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        rows = resp.json()
        scores = {}
        for row in rows if isinstance(rows, list) else []:
            name = row.get("model") or row.get("name", "")
            rate = row.get("pass_rate_2") or row.get("pass_rate", 0)
            resolved = _resolve(name) or _resolve(name.lower())
            if resolved and isinstance(rate, (int, float)):
                scores[resolved] = float(rate) / 100.0 if float(rate) > 1 else float(rate)
        log.info("Aider: fetched %d model scores", len(scores))
        return scores
    except Exception as e:
        log.warning("Aider fetch failed: %s — using empty data", e)
        return {}


def fetch_huggingface() -> dict[str, float]:
    """Fetch HuggingFace Open LLM Leaderboard scores.

    Averages IFEval, MMLU-Pro, and MATH Lvl5 columns.

    Returns:
        Dict mapping provider/model → averaged score (0.0–1.0).
        Empty dict on any network or parse failure.
    """
    try:
        import httpx
        # Use the HF Datasets API for the Open LLM Leaderboard v2
        resp = httpx.get(
            "https://huggingface.co/datasets/open-llm-leaderboard/contents/resolve/main/contents/results.json",
            timeout=30,
            follow_redirects=True,
        )
        resp.raise_for_status()
        rows = resp.json() if isinstance(resp.json(), list) else []
        scores: dict[str, list[float]] = {}
        for row in rows:
            name = row.get("fullname") or row.get("model", "")
            resolved = _resolve(name) or _resolve(name.split("/")[-1])
            if not resolved:
                continue
            vals = []
            for col in ("IFEval", "MMLU-Pro", "MATH Lvl 5", "ifeval", "mmlu_pro"):
                v = row.get(col)
                if isinstance(v, (int, float)):
                    vals.append(float(v) / 100.0 if float(v) > 1.0 else float(v))
            if vals:
                scores.setdefault(resolved, []).extend(vals)
        result = {m: sum(v) / len(v) for m, v in scores.items() if v}
        log.info("HuggingFace: fetched %d model scores", len(result))
        return result
    except Exception as e:
        log.warning("HuggingFace fetch failed: %s — using empty data", e)
        return {}


def fetch_litellm_pricing() -> dict[str, tuple[float, float]]:
    """Read LiteLLM's bundled pricing table (no network call needed).

    Returns:
        Dict mapping provider/model → (input_cost_per_1m, output_cost_per_1m).
    """
    try:
        import litellm
        pricing: dict[str, tuple[float, float]] = {}
        for name, info in litellm.model_cost.items():
            if not isinstance(info, dict):
                continue
            inp = info.get("input_cost_per_token", 0) or 0
            out = info.get("output_cost_per_token", 0) or 0
            # Convert per-token to per-1M
            pricing[name] = (inp * 1_000_000, out * 1_000_000)
        # Map LiteLLM names to our provider/model format where possible
        result = {}
        for key in (
            "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
            "gpt-4o", "gpt-4o-mini", "o3",
            "gemini/gemini-2.5-pro", "gemini/gemini-2.5-flash",
            "deepseek/deepseek-chat", "deepseek/deepseek-reasoner",
        ):
            if key in pricing:
                canonical = _resolve(key) or key
                result[canonical] = pricing[key]
        log.info("LiteLLM pricing: fetched %d model prices", len(result))
        return result
    except Exception as e:
        log.warning("LiteLLM pricing fetch failed: %s — using empty data", e)
        return {}


def compute_task_scores(
    lmsys: dict[str, float],
    aider: dict[str, float],
    hf: dict[str, float],
    pricing: dict[str, tuple[float, float]],
) -> dict[str, dict[str, float]]:
    """Compute weighted composite scores per task type for each model.

    Args:
        lmsys: Normalized LMSYS ELO scores (0.0–1.0).
        aider: Aider pass rates (0.0–1.0, already normalized).
        hf: HuggingFace averaged scores (0.0–1.0).
        pricing: (input_per_1m, output_per_1m) per model.

    Returns:
        Nested dict: ``{task_type: {model: composite_score}}``.
    """
    # Build blended cost (60% input, 40% output) and normalize inversely.
    blended_costs = {
        m: p[0] * 0.6 + p[1] * 0.4
        for m, p in pricing.items()
    }
    cost_inv = _invert(_normalize(blended_costs)) if blended_costs else {}

    # All models mentioned across any source.
    all_models = set(lmsys) | set(aider) | set(hf) | set(cost_inv)
    all_models = {m for m in all_models if not any(m.startswith(p) for p in _EXCLUDED_PREFIXES)}

    task_scores: dict[str, dict[str, float]] = {}
    for task, (w_lmsys, w_aider, w_hf, w_cost) in _TASK_WEIGHTS.items():
        scores: dict[str, float] = {}
        for model in all_models:
            components = []
            weights_used = []
            def _add(source: dict[str, float], w: float) -> None:
                if model in source:
                    components.append(source[model] * w)
                    weights_used.append(w)
            _add(lmsys, w_lmsys)
            _add(aider, w_aider)
            _add(hf, w_hf)
            _add(cost_inv, w_cost)
            if components:
                # Redistribute weights proportionally when sources are missing.
                total_w = sum(weights_used)
                scores[model] = sum(components) / total_w if total_w > 0 else 0.5
        task_scores[task] = dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

    return task_scores


def compute_tiers(task_scores: dict[str, dict[str, float]]) -> dict[str, dict[str, list[str]]]:
    """Map composite scores to tier lists (premium / balanced / budget) per task type.

    Args:
        task_scores: Output of ``compute_task_scores()``.

    Returns:
        Nested dict: ``{task_type: {tier: [model_id, ...]}}``.
    """
    tiers: dict[str, dict[str, list[str]]] = {}
    for task, scores in task_scores.items():
        ranked = sorted(scores, key=lambda m: scores[m], reverse=True)
        premium = ranked[:_PREMIUM_COUNT]
        balanced = ranked[_PREMIUM_COUNT:_PREMIUM_COUNT + _BALANCED_COUNT]
        budget = ranked[_PREMIUM_COUNT + _BALANCED_COUNT:]
        tiers[task] = {
            "premium": premium,
            "balanced": balanced,
            "budget": budget,
        }
    return tiers


def generate_benchmarks_json(output_path: Path = _OUTPUT_PATH) -> None:
    """Fetch all sources, score models, and write updated ``benchmarks.json``.

    Increments the ``version`` field from the existing file. Falls back
    gracefully when individual sources are unreachable.

    Args:
        output_path: Where to write the JSON (default: bundled data file).
    """
    # Load existing file to increment version and preserve source timestamps.
    existing: dict[str, Any] = {}
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    now = datetime.now(timezone.utc).isoformat()

    # Fetch raw data from all sources.
    log.info("Fetching benchmark data...")
    lmsys_raw = fetch_lmsys()
    aider_raw = fetch_aider()
    hf_raw = fetch_huggingface()
    pricing = fetch_litellm_pricing()

    # Normalize LMSYS ELO (min-max).
    lmsys = _normalize(lmsys_raw)

    # Build raw_scores for auditability.
    all_models = set(lmsys_raw) | set(aider_raw) | set(hf_raw)
    raw_scores: dict[str, Any] = {}
    for model in sorted(all_models):
        inp, out = pricing.get(model, (None, None))
        raw_scores[model] = {
            "lmsys_elo": lmsys_raw.get(model),
            "aider_pass_rate": aider_raw.get(model),
            "hf_score": hf_raw.get(model),
            "cost_per_1m_input": inp,
            "cost_per_1m_output": out,
        }

    # Compute scores and tiers.
    task_scores = compute_task_scores(lmsys, aider_raw, hf_raw, pricing)
    tiers = compute_tiers(task_scores)

    output: dict[str, Any] = {
        "version": int(existing.get("version", 0)) + 1,
        "generated_at": now,
        "sources": {
            "lmsys_fetched_at": now if lmsys_raw else existing.get("sources", {}).get("lmsys_fetched_at"),
            "aider_fetched_at": now if aider_raw else existing.get("sources", {}).get("aider_fetched_at"),
            "huggingface_fetched_at": now if hf_raw else existing.get("sources", {}).get("huggingface_fetched_at"),
            "litellm_fetched_at": now if pricing else existing.get("sources", {}).get("litellm_fetched_at"),
        },
        "raw_scores": raw_scores,
        "task_scores": task_scores,
        "tiers": tiers,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    log.info(
        "benchmarks.json updated to v%d (%d models, %d task types)",
        output["version"],
        len(raw_scores),
        len(task_scores),
    )
