"""Quota-balanced routing — monitor and reorder chains to balance subscription usage.

QUOTA_BALANCED is a new routing profile that dynamically reorders the fallback chain
based on real-time quota consumption across three subscription providers:
  - Claude (via get_claude_pressure())
  - Gemini CLI (via get_gemini_pressure())
  - Codex (via local daily counter)

Algorithm:
  1. Fetch current pressure (0.0–1.0) from each provider
  2. If pressures are within ±10% band → use FREE-FIRST tiebreak order: codex → gemini → claude
  3. If imbalance exceeds ±10% → route to least-used provider first
  4. Reorder the static routing chain by provider prefix to reflect the decision
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from llm_router.logging import get_logger

log = get_logger("llm_router.quota_balance")

_QUOTA_CACHE_FILE = Path.home() / ".llm-router" / "codex_quota.json"
_QUOTA_CACHE_TTL = 300  # 5 minutes


def get_codex_pressure(daily_limit: int = 1000) -> float:
    """Get budget pressure for Codex (count / daily_limit, resets at UTC midnight).

    Reads from ~/.llm-router/codex_quota.json. Resets counter if date changed.

    Args:
        daily_limit: Max requests per day (default 1000 for free tier).

    Returns:
        Float 0.0–1.0 representing daily quota usage.
        0.0 = unused, 1.0 = limit reached.
    """
    if not _QUOTA_CACHE_FILE.exists():
        return 0.0

    try:
        data = json.loads(_QUOTA_CACHE_FILE.read_text())
        today = datetime.now(timezone.utc).date().isoformat()
        cached_date = data.get("date")

        if cached_date == today:
            count = data.get("count", 0)
            return min(1.0, max(0.0, count / daily_limit if daily_limit > 0 else 0.0))
        else:
            # New day — reset
            return 0.0
    except Exception:
        return 0.0


def record_codex_request(daily_limit: int = 1000) -> None:
    """Increment Codex daily counter and persist to ~/.llm-router/codex_quota.json.

    Auto-resets when date changes. Uses UTC midnight as day boundary.

    Args:
        daily_limit: Max requests per day (default 1000).
    """
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        _QUOTA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Read current state (or initialize if missing)
        data = {}
        if _QUOTA_CACHE_FILE.exists():
            try:
                data = json.loads(_QUOTA_CACHE_FILE.read_text())
                cached_date = data.get("date")
                # Reset if day changed
                if cached_date != today:
                    data = {}
            except Exception:
                data = {}

        # Increment counter for today
        if data.get("date") != today:
            count = 1
        else:
            count = data.get("count", 0) + 1

        # Persist atomically
        new_data = {
            "date": today,
            "count": count,
            "estimated_daily_limit": daily_limit,
            "cached_at": time.time(),
        }
        _QUOTA_CACHE_FILE.write_text(json.dumps(new_data, indent=2))
    except Exception:
        pass  # Silently fail — quota tracking is best-effort


async def get_provider_pressures() -> dict[str, float]:
    """Fetch budget pressure from all three subscription providers.

    Returns a dict with claude, gemini_cli, and codex pressures (0.0–1.0).

    Returns:
        Dict with keys "claude", "gemini_cli", "codex" and float values 0.0–1.0.
    """
    from llm_router.claude_usage import get_claude_pressure
    from llm_router.gemini_cli_quota import get_gemini_pressure
    from llm_router.config import get_config

    cfg = get_config()

    pressures = {
        "claude": get_claude_pressure(),
        "codex": get_codex_pressure(cfg.codex_daily_limit),
    }

    # Gemini pressure is async
    try:
        pressures["gemini_cli"] = await get_gemini_pressure()
    except Exception:
        pressures["gemini_cli"] = 0.0

    return pressures


def get_balanced_provider_order(
    pressures: dict[str, float], tolerance: float = 0.10
) -> list[str]:
    """Determine provider routing order based on quota balance.

    If all providers are within ±tolerance of each other, use free-first tiebreak order.
    Otherwise, sort ascending by pressure (least-used first).

    Args:
        pressures: Dict with "claude", "gemini_cli", "codex" pressure values.
        tolerance: If max-min <= tolerance, use tiebreak order (default 0.10 = ±10%).

    Returns:
        List of provider names in routing order: ["codex", "gemini_cli", "claude"] or
        ascending by pressure.
    """
    if not pressures:
        return ["codex", "gemini_cli", "claude"]

    values = [
        pressures.get("claude", 0.0),
        pressures.get("gemini_cli", 0.0),
        pressures.get("codex", 0.0),
    ]
    spread = max(values) - min(values)

    if spread <= tolerance:
        # Within ±10% — use free-first tiebreak order
        return ["codex", "gemini_cli", "claude"]
    else:
        # Imbalanced — route to least-used provider first
        pairs = [
            ("codex", pressures.get("codex", 0.0)),
            ("gemini_cli", pressures.get("gemini_cli", 0.0)),
            ("claude", pressures.get("claude", 0.0)),
        ]
        pairs.sort(key=lambda p: p[1])
        return [name for name, _ in pairs]


def reorder_chain_by_providers(
    chain: list[str], provider_order: list[str]
) -> list[str]:
    """Reorder a routing chain to prioritize providers in the given order.

    Segments the chain by provider prefix (codex/, gemini_cli/, anthropic/, etc.),
    then concatenates them in the order specified by provider_order.
    Non-subscription models (paid APIs) remain at the end.

    Args:
        chain: Unordered routing chain like ["ollama/...", "gemini/...", "codex/...", ...].
        provider_order: List of provider names in preferred order, e.g. ["codex", "gemini_cli", "claude"].

    Returns:
        Reordered chain with subscription providers prioritized per provider_order.
    """
    # Segment chain by provider
    segments: dict[str, list[str]] = {
        "claude": [],
        "gemini_cli": [],
        "codex": [],
        "ollama": [],
        "other": [],
    }

    for model in chain:
        if model.startswith("anthropic/"):
            segments["claude"].append(model)
        elif model.startswith("gemini_cli/"):
            segments["gemini_cli"].append(model)
        elif model.startswith("codex/"):
            segments["codex"].append(model)
        elif model.startswith("ollama/"):
            segments["ollama"].append(model)
        else:
            segments["other"].append(model)

    # Reorder: ollama (always first, local is free) → subscription in order → paid APIs
    result = []
    result.extend(segments["ollama"])

    for provider in provider_order:
        if provider in segments:
            result.extend(segments[provider])

    # Append any remaining paid APIs
    result.extend(segments["other"])

    return result
