"""Pressure-aware model chain builder for direct hook execution.

Builds ordered model chains based on:
  - Task complexity (simple / moderate / complex)
  - Quota pressure zone (green / yellow / orange / red / critical)
  - Available providers (Ollama running? API keys present?)

The 80/20 rule: ~80% of prompts are simple/moderate and always go to
cheap/free models. ~20% are complex and MAY use Claude when pressure allows.

Claude models appear in the chain but are skipped by direct_executor
(they can't be called from the hook). Standard mode falls through when
they are the only option left; zero-Claude mode blocks the submission.
"""

from __future__ import annotations

import os
import json
from pathlib import Path

from llm_router.hooks.direct_executor import ModelSpec


# ── Available Models ──────────────────────────────────────────────────────────

def _ollama_models() -> list[ModelSpec]:
    """Get configured Ollama models."""
    models_str = os.environ.get("OLLAMA_BUDGET_MODELS",
                                os.environ.get("LLM_ROUTER_OLLAMA_MODEL", "qwen3.5:latest"))
    models = [m.strip() for m in models_str.split(",") if m.strip()]
    return [ModelSpec("ollama", m) for m in models]


def _has_gemini() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", ""))


def _has_openai() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", ""))


# ── Pressure Zone Calculator ─────────────────────────────────────────────────

def pressure_zone(session_pct: float, resets_at: str = "") -> str:
    """Determine the quota pressure zone.

    Args:
        session_pct: Current 5h session quota usage as percentage (0-100).
        resets_at: ISO timestamp when the 5h window resets (for time-adjusted pressure).

    Returns:
        Zone string: green / yellow / orange / red / critical
    """
    effective = _effective_pressure(session_pct, resets_at)

    if effective < 30:
        return "green"
    elif effective < 50:
        return "yellow"
    elif effective < 70:
        return "orange"
    elif effective < 85:
        return "red"
    else:
        return "critical"


def _effective_pressure(raw_pct: float, resets_at: str) -> float:
    """Adjust pressure based on time remaining in the 5h window.

    If we've used 40% quota but 80% of the window has passed, we're spending
    slowly → effective pressure is lower than raw suggests.

    If we've used 40% but only 20% has passed, we're burning fast → higher.
    """
    if not resets_at:
        return raw_pct

    try:
        from datetime import datetime, timezone
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        window_total_sec = 5 * 3600  # 5 hours
        remaining_sec = max(0, (reset_dt - now).total_seconds())
        elapsed_pct = 1.0 - (remaining_sec / window_total_sec)

        if elapsed_pct <= 0.05:
            return raw_pct  # Too early to judge rate

        # spending_rate: >1 means burning faster than time passes
        spending_rate = (raw_pct / 100.0) / elapsed_pct
        adjusted = raw_pct * max(spending_rate, 0.5)  # Floor at 0.5x to avoid over-optimism
        return min(adjusted, 100.0)
    except Exception:
        return raw_pct


# ── Read Current Pressure ─────────────────────────────────────────────────────

def get_current_pressure() -> tuple[str, float]:
    """Read pressure from usage.json and return (zone, raw_pct).

    Returns ("green", 0.0) if usage data unavailable.
    """
    usage_path = Path.home() / ".llm-router" / "usage.json"
    try:
        data = json.loads(usage_path.read_text())
        session_pct = float(data.get("session_pct", 0.0))
        resets_at = data.get("session_resets_at", "")
        zone = pressure_zone(session_pct, resets_at)
        return zone, session_pct
    except Exception:
        return "green", 0.0


# ── Chain Builder ─────────────────────────────────────────────────────────────

# Shared model specs
_GEMINI_FLASH = ModelSpec("gemini", "gemini-2.5-flash")
_GEMINI_PRO = ModelSpec("gemini", "gemini-2.0-pro")
_GPT4O_MINI = ModelSpec("openai", "gpt-4o-mini")
_GPT4O = ModelSpec("openai", "gpt-4o")
_CLAUDE_SONNET = ModelSpec("claude", "claude-sonnet-4-6", quota_cost=1.0)
_CLAUDE_OPUS = ModelSpec("claude", "claude-opus-4-6", quota_cost=3.0)


def build_chain(complexity: str, zone: str, task_type: str) -> list[ModelSpec]:
    """Build an ordered model chain based on complexity and pressure zone.

    The chain is ordered cheapest-first. Claude models are included when
    pressure allows - direct_executor skips them (can't call from hook).
    A caller outside zero-Claude mode can then fall through to Claude.

    Args:
        complexity: simple / moderate / complex / deep_reasoning
        zone: green / yellow / orange / red / critical
        task_type: query / research / generate / analyze / code

    Returns:
        Ordered list of ModelSpec to try.
    """
    ollama = _ollama_models()
    has_gemini = _has_gemini()
    has_openai = _has_openai()

    # Externals available based on API keys
    cheap_externals: list[ModelSpec] = []
    mid_externals: list[ModelSpec] = []

    if has_gemini:
        cheap_externals.append(_GEMINI_FLASH)
        mid_externals.append(_GEMINI_PRO)
    if has_openai:
        cheap_externals.append(_GPT4O_MINI)
        mid_externals.append(_GPT4O)

    # ── Simple: NEVER uses Claude — always direct ────────────────────────────
    if complexity == "simple":
        return ollama + cheap_externals

    # ── Moderate: Claude included only at low pressure ───────────────────────
    if complexity == "moderate":
        base = ollama + cheap_externals + mid_externals
        if zone == "green":
            # Plenty of quota — Claude Sonnet as fallback is fine
            return base + [_CLAUDE_SONNET]
        elif zone == "yellow":
            # Getting warm — Claude Sonnet as last resort only
            return base + [_CLAUDE_SONNET]
        else:
            # Orange/Red/Critical — no Claude for moderate
            return base

    # ── Complex: Claude leads at low pressure, excluded at high ──────────────
    if complexity in ("complex", "deep_reasoning"):
        if zone == "green":
            # Plenty of quota — Claude Opus leads for max quality
            return [_CLAUDE_OPUS] + ollama + mid_externals
        elif zone == "yellow":
            # Comfortable — try externals first, Claude Opus as strong option
            return ollama + mid_externals + [_CLAUDE_OPUS]
        elif zone == "orange":
            # Getting tight — externals first, Claude Sonnet (cheaper) as fallback
            return ollama + mid_externals + [_CLAUDE_SONNET]
        elif zone == "red":
            # Preserve Claude — externals only
            return ollama + mid_externals + cheap_externals
        else:
            # Critical — no Claude at all
            return ollama + mid_externals + cheap_externals

    # Fallback for unknown complexity
    return ollama + cheap_externals


def chain_has_claude(chain: list[ModelSpec]) -> bool:
    """Check if any model in the chain is a Claude model."""
    return any(m.provider == "claude" for m in chain)


def needs_claude_tools(prompt: str, task_type: str) -> bool:
    """Does this prompt require file and command tools?

    If yes, direct execution must use the external tool-capable agent path.
    Native Claude is only available as a non-strict fallback.

    Delegates to ``llm_router.capabilities.detect_capabilities(...).legacy_match``
    (WS4), which reproduces this function's original inline regex logic exactly --
    this refactor is behavior-preserving by construction (see the deviation note
    in ``capabilities.py`` about why ``legacy_match`` intentionally omits the
    richer ``_LEGACY_LOCAL_FS`` pattern that only the new capability vector uses).
    FAIL-OPEN: any import/lookup failure falls back to the original inline logic
    so a capabilities-module issue can never silently disable tool routing.
    """
    try:
        from llm_router.capabilities import detect_capabilities

        return detect_capabilities(prompt, task_type).legacy_match
    except Exception:  # noqa: BLE001 -- must never break routing
        import re

        # Project structure or local context references (applicable to any task type)
        if re.search(
            r'\b(src/|tests/|hooks/|in the codebase|this file|this repo|this project|current project|current version|what version|package\.json|pyproject\.toml|llm-router|blocked by hook|error message)\b',
            prompt,
            re.IGNORECASE,
        ):
            return True

        # Reading an explicit local file requires tool access even when the
        # classifier labels the request as a simple query.
        if re.search(
            r'\b(?:read|open|inspect|show|cat|summari[sz]e)\s+'
            r'(?:the\s+)?(?:file\s+)?[\w./-]+\.[A-Za-z0-9]{1,8}\b',
            prompt,
            re.IGNORECASE,
        ):
            return True

        if task_type not in ("code", "analyze"):
            return False  # General Q&A, research, generate never need file tools

        # Explicit file references
        if re.search(r'[\w/]+\.\w{1,4}\b', prompt) and re.search(
            r'\.(py|ts|js|go|rs|java|cpp|yaml|json|md|toml|cfg|sh|sql)\b', prompt
        ):
            return True
        # Edit/fix/debug intent with location
        if re.search(r'\b(fix|debug|investigate|refactor|update|modify)\b', prompt, re.IGNORECASE) and \
           re.search(r'\b(in|at|from|the)\s+(src|tests|hooks|module|class|function)\b', prompt, re.IGNORECASE):
            return True
        return False
