"""Helicone integration for llm-router.

When ``HELICONE_API_KEY`` is set, llm-router can:

  **Push** — attach routing metadata to every LiteLLM call as Helicone
  custom properties, making routing decisions visible in the Helicone
  dashboard alongside cost and latency data.

  **Pull** — query Helicone's usage API to get per-model spend data,
  which supplements the local SQLite budget tracking.  Useful when some
  traffic is routed outside llm-router (e.g. direct SDK calls from your
  app that also use Helicone).

Configuration::

    export HELICONE_API_KEY=sk-helicone-...

Optional::

    export LLM_ROUTER_HELICONE_PULL=true   # pull spend data (default: false)

Usage (push — called automatically by the router)::

    from llm_router.integrations.helicone import get_helicone_headers
    extra_headers = get_helicone_headers(task_type="code", model="ollama/qwen3:32b")
    # Pass extra_headers to litellm.acompletion(..., headers=extra_headers)

Usage (pull — called by budget.py when enabled)::

    from llm_router.integrations.helicone import get_helicone_spend
    spend_by_provider = await get_helicone_spend()
    # {"openai": 4.21, "gemini": 0.87, ...}
"""

from __future__ import annotations

import os
from typing import Any


# ── Push ──────────────────────────────────────────────────────────────────────


def get_helicone_headers(
    task_type: str = "",
    model: str = "",
    complexity: str = "",
    profile: str = "",
) -> dict[str, str]:
    """Return extra HTTP headers to attach to LiteLLM calls for Helicone tracking.

    When ``HELICONE_API_KEY`` is not set, returns an empty dict (no-op).

    Args:
        task_type:  Routing task type (e.g. ``"code"``, ``"query"``).
        model:      Model ID selected by the router.
        complexity: Complexity classification (e.g. ``"simple"``, ``"complex"``).
        profile:    Routing profile (e.g. ``"budget"``, ``"balanced"``).

    Returns:
        Dict of headers to merge into the LiteLLM call.
    """
    api_key = os.environ.get("HELICONE_API_KEY", "")
    if not api_key:
        return {}

    headers: dict[str, str] = {
        "Helicone-Auth": f"Bearer {api_key}",
    }
    if task_type:
        headers["Helicone-Property-Router-Task-Type"] = task_type
    if model:
        headers["Helicone-Property-Router-Model"] = model
    if complexity:
        headers["Helicone-Property-Router-Complexity"] = complexity
    if profile:
        headers["Helicone-Property-Router-Profile"] = profile
    return headers


def is_helicone_enabled() -> bool:
    """Return True when a Helicone API key is configured."""
    return bool(os.environ.get("HELICONE_API_KEY", ""))


# ── Pull ──────────────────────────────────────────────────────────────────────

_HELICONE_API_BASE = "https://api.helicone.ai"
_PULL_ENABLED_ENV = "LLM_ROUTER_HELICONE_PULL"


async def get_helicone_spend(days: int = 30) -> dict[str, float]:
    """Pull per-provider spend from Helicone for the last *days* days.

    Returns a dict mapping provider name → spend in USD.
    Returns an empty dict if Helicone pull is not enabled or the API call fails.

    Args:
        days: Number of past days to aggregate spend over (default: 30).

    Returns:
        ``{"openai": 4.21, "gemini": 0.87, ...}``
    """
    api_key = os.environ.get("HELICONE_API_KEY", "")
    if not api_key:
        return {}
    if os.environ.get(_PULL_ENABLED_ENV, "").lower() not in ("1", "true", "yes"):
        return {}

    try:
        import asyncio
        import urllib.request
        import json
        from datetime import datetime, timezone, timedelta

        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"{_HELICONE_API_BASE}/v1/usage/costs?start_date={start_date}&group_by=provider"

        def _fetch() -> dict[str, Any]:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())

        data = await asyncio.get_event_loop().run_in_executor(None, _fetch)

        # Helicone returns: {"data": [{"provider": "openai", "cost": 4.21}, ...]}
        result: dict[str, float] = {}
        for entry in data.get("data", []):
            provider = str(entry.get("provider", "")).lower()
            cost = float(entry.get("cost", 0.0))
            if provider and cost > 0:
                result[provider] = result.get(provider, 0.0) + cost
        return result

    except Exception:
        return {}
