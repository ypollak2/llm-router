"""Persistent budget cap storage for llm-router.

Caps are stored in ``~/.llm-router/budgets.json`` and take priority over
environment-variable caps (``LLM_ROUTER_BUDGET_OPENAI``, etc.).

This file is written atomically (tmp → rename) so concurrent readers (the
MCP server, the dashboard, the CLI) never see a partial write.

Usage::

    from llm_router.budget_store import get_caps, set_cap, remove_cap

    set_cap("openai", 20.0)      # persist $20/month cap
    caps = get_caps()            # {"openai": 20.0}
    remove_cap("openai")         # clear the cap
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_ROUTER_DIR = Path.home() / ".llm-router"
_BUDGETS_FILE = _ROUTER_DIR / "budgets.json"


# ── Public API ────────────────────────────────────────────────────────────────


def get_caps() -> dict[str, float]:
    """Return all persisted budget caps as ``{provider: monthly_cap_usd}``.

    Returns an empty dict when no caps have been set or the file is missing.
    """
    try:
        return {
            k: float(v)
            for k, v in json.loads(_BUDGETS_FILE.read_text()).items()
            if isinstance(v, (int, float)) and float(v) > 0
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def set_cap(provider: str, amount: float) -> None:
    """Persist a monthly budget cap for *provider*.

    Args:
        provider: Provider name (e.g. ``"openai"``, ``"gemini"``).
        amount:   Monthly cap in USD.  Must be > 0.

    Raises:
        ValueError: If *amount* is not positive.
    """
    if amount <= 0:
        raise ValueError(f"Budget cap must be > 0, got {amount}")

    caps = _read_raw()
    caps[provider] = amount
    _write_atomic(caps)


def remove_cap(provider: str) -> bool:
    """Remove the persisted cap for *provider*.

    Args:
        provider: Provider name to clear.

    Returns:
        ``True`` if a cap was removed, ``False`` if none existed.
    """
    caps = _read_raw()
    if provider not in caps:
        return False
    del caps[provider]
    _write_atomic(caps)
    return True


def list_caps() -> dict[str, float]:
    """Alias for :func:`get_caps` — returns all persisted caps."""
    return get_caps()


def get_cap(provider: str) -> float:
    """Return the persisted cap for *provider*, or ``0.0`` if not set."""
    return get_caps().get(provider, 0.0)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _read_raw() -> dict[str, float]:
    """Read the raw budgets.json, returning {} on any error."""
    try:
        data = json.loads(_BUDGETS_FILE.read_text())
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_atomic(caps: dict[str, float]) -> None:
    """Write *caps* to budgets.json atomically via a temp file + os.replace()."""
    _ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _BUDGETS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(caps, indent=2, sort_keys=True))
    os.replace(tmp, _BUDGETS_FILE)
