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

    # ── Research: always fall through to llm_research MCP tool (Perplexity) ──
    # Ollama has a knowledge cutoff and cannot answer current-events or
    # time-sensitive questions accurately. Returning [] causes direct_executor
    # to return None, so the hook falls through to inject the ⚡ MANDATORY
    # ROUTE hint pointing to llm_research, which calls Perplexity or a
    # web-grounded model. This applies at ALL complexity levels for research.
    if task_type == "research":
        return []

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
    # Ollama is excluded from complex chains for code/research tasks — Ollama
    # on complex reasoning causes UP-inversions (qwen3.5 winning tasks it
    # handles poorly). For these task types, let the chain fail through to
    # Claude (subscription) rather than degrade to local.
    high_risk = task_type in ("code", "research")
    if complexity in ("complex", "deep_reasoning"):
        if zone == "green":
            # Plenty of quota — Claude Opus leads for max quality
            return [_CLAUDE_OPUS] + mid_externals + ([] if high_risk else ollama)
        elif zone == "yellow":
            # Comfortable — mid-tier externals lead, Claude Opus as premium fallback
            return mid_externals + ([] if high_risk else ollama) + [_CLAUDE_OPUS]
        elif zone == "orange":
            # Getting tight — mid-tier externals lead, Claude Sonnet as last resort
            return mid_externals + ([] if high_risk else ollama) + [_CLAUDE_SONNET]
        elif zone == "red":
            # Preserve Claude — mid-tier first; skip Ollama for high-risk task types
            return mid_externals + ([] if high_risk else ollama) + cheap_externals
        else:
            # Critical — no Claude; mid-tier before Ollama, but skip Ollama for high-risk
            return mid_externals + ([] if high_risk else ollama) + cheap_externals

    # Fallback for unknown complexity
    return ollama + cheap_externals


def chain_has_claude(chain: list[ModelSpec]) -> bool:
    """Check if any model in the chain is a Claude model."""
    return any(m.provider == "claude" for m in chain)


def needs_claude_tools(prompt: str, task_type: str = "") -> bool:
    """Does this prompt require file and command tools?

    Thin wrapper over :func:`llm_router.capabilities.detect_capabilities` — the SINGLE
    shared predicate for exemption / routing / provisioning / permissions (CF-2). This
    module holds no regex of its own; all detection lives in ``capabilities``.

    Default (shadow mode) returns the LEGACY boolean so routing is byte-identical to
    the pre-CF-2 behavior. With ``LLM_ROUTER_CAPABILITY_ROUTING=1`` it returns the richer
    8-bit vector's ``needs_tools`` (symbol/write/command aware).
    """
    from llm_router.capabilities import capability_routing_enabled, detect_capabilities
    decision = detect_capabilities(prompt, task_type)
    if capability_routing_enabled():
        return decision.required.needs_tools
    return decision.legacy_match
