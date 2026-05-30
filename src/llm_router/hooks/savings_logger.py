"""Persist DIRECT routing savings to ``~/.llm-router/savings_log.jsonl``.

Background:
    ``auto-route.py`` can answer a prompt by calling a cheap external model
    (Ollama / Gemini / OpenAI) via ``direct_executor.execute_chain``. When
    that succeeds, the model produced the answer for ~free, but historically
    no savings record was persisted. As a result, ``session-end.py``'s
    ``_sync_import_savings_log()`` had nothing to flush and the dashboard
    showed $0.00 saved for any session that relied entirely on DIRECT routing.

This module fixes that gap by appending one JSONL record per successful
DIRECT execution. ``session-end.py`` already flushes ``savings_log.jsonl``
into the ``savings_stats`` table on session end, so the dashboard picks
the data up without any further wiring.

Schema (must stay in sync with ``hooks/session-end.py::_sync_import_savings_log``):

    {
        "timestamp":        "<iso-8601 UTC>",
        "session_id":       "<claude code session id>",
        "task_type":        "query|code|analyze|generate|...",
        "complexity":       "simple|moderate|complex",
        "estimated_saved":  <float, USD>,
        "external_cost":    <float, USD, actual provider cost>,
        "model":            "<provider>/<model>",
        "host":             "claude_code"   # or codex/gemini-cli/opencode
    }
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_router.hooks.direct_executor import DirectResult


# ── Pricing table (USD per 1M tokens) ────────────────────────────────────────
# Conservative rates as of late 2025 / early 2026. Used only for relative
# savings estimation in the JSONL — not for billing. Unknown models map to
# (0.0, 0.0) so they don't crash and don't claim spurious savings.

_PRICING_PER_MTOK: dict[tuple[str, str], tuple[float, float]] = {
    # Claude (baseline references — what the user's subscription would otherwise spend)
    ("claude", "claude-haiku-4-5"):   (0.80,  4.00),
    ("claude", "claude-sonnet-4-6"):  (3.00, 15.00),
    ("claude", "claude-opus-4-7"):   (15.00, 75.00),
    # Ollama — local, free
    ("ollama", "*"):                  (0.00,  0.00),
    # Gemini
    ("gemini", "gemini-2.5-flash"):   (0.075, 0.30),
    ("gemini", "gemini-2.0-flash"):   (0.075, 0.30),
    ("gemini", "gemini-2.0-pro"):     (1.25,  5.00),
    # OpenAI
    ("openai", "gpt-4o-mini"):        (0.15,  0.60),
    ("openai", "gpt-4o"):             (2.50, 10.00),
    ("openai", "o3"):                (15.00, 60.00),
    # Codex — prepaid subscription, marginal cost ≈ 0
    ("codex",  "*"):                  (0.00,  0.00),
}

_BASELINE_MODEL_BY_COMPLEXITY: dict[str, str] = {
    "simple":   "claude-haiku-4-5",
    "moderate": "claude-sonnet-4-6",
    "complex":  "claude-opus-4-7",
}

_SAVINGS_LOG_FILENAME = "savings_log.jsonl"


def _lookup_rate(provider: str, model: str) -> tuple[float, float]:
    """Return (input_rate, output_rate) in USD per 1M tokens."""
    key = (provider, model)
    if key in _PRICING_PER_MTOK:
        return _PRICING_PER_MTOK[key]
    wildcard = (provider, "*")
    if wildcard in _PRICING_PER_MTOK:
        return _PRICING_PER_MTOK[wildcard]
    return (0.0, 0.0)


def _cost_for(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = _lookup_rate(provider, model)
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _baseline_cost(complexity: str, input_tokens: int, output_tokens: int) -> float:
    baseline_model = _BASELINE_MODEL_BY_COMPLEXITY.get(complexity, "claude-sonnet-4-6")
    return _cost_for("claude", baseline_model, input_tokens, output_tokens)


def _savings_log_path() -> Path:
    """Path is resolved at call time so test fixtures that patch Path.home() work."""
    return Path.home() / ".llm-router" / _SAVINGS_LOG_FILENAME


def log_direct_savings(
    result: "DirectResult",
    task_type: str,
    complexity: str,
    session_id: str,
    *,
    host: str = "claude_code",
) -> None:
    """Append a savings record for a successful DIRECT routing.

    Fire-and-forget — never raises. Any filesystem or serialization failure
    is silently swallowed so the calling hook (``auto-route.py``) stays
    snappy and robust on the user's critical path.
    """
    try:
        provider = result.model.provider
        model = result.model.model
        input_tokens = max(0, int(result.input_tokens or 0))
        output_tokens = max(0, int(result.output_tokens or 0))

        external_cost = _cost_for(provider, model, input_tokens, output_tokens)
        baseline = _baseline_cost(complexity, input_tokens, output_tokens)
        estimated_saved = max(0.0, baseline - external_cost)

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "task_type": task_type,
            "complexity": complexity,
            "estimated_saved": estimated_saved,
            "external_cost": external_cost,
            "model": f"{provider}/{model}",
            "host": host,
        }

        path = _savings_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        # Silent — savings logging must never break the routing hook.
        pass
