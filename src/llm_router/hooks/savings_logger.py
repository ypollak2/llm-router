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

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from llm_router import pricing as _pricing

if TYPE_CHECKING:
    from llm_router.hooks.direct_executor import DirectResult
    from llm_router.receipt_store import Receipt


# Strong references to fire-and-forget persistence tasks scheduled on an already
# running loop (the gateway / SDK async path). Without this the loop would keep
# only a weak reference and the task could be GC'd before it finishes writing.
_INFLIGHT_PERSISTS: set = set()


# ── Pricing table (USD per 1M tokens) ────────────────────────────────────────
# Conservative rates as of late 2025 / early 2026. Used only for relative
# savings estimation in the JSONL — not for billing. Unknown models map to
# (0.0, 0.0) so they don't crash and don't claim spurious savings.

# WP-03: the rates come from llm_router.pricing now; only provider→model membership
# is stated here. This table's own history is the argument for that. Its comment
# recorded a hand-update on 2026-07-10 to undo a ~3x inflation, and it was the
# ONLY table in the codebase that had o3 right — "repriced from stale $15/$60"
# while three others kept $15/$60. A fix applied to one copy is not a fix; it is
# a divergence with a good changelog entry.
_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    # Claude — baseline references: what the subscription would otherwise spend.
    "claude": ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"),
    "gemini": ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-pro"),
    "openai": ("gpt-4o-mini", "gpt-4o", "o3"),
}

# Prepaid or local: marginal cost is genuinely zero for every model served this
# way, which is why these are wildcards rather than per-model rates.
_ZERO_MARGINAL_PROVIDERS: tuple[str, ...] = ("ollama", "codex")
_ZERO_RATE = (0.0, 0.0)

_PRICING_PER_MTOK: dict[tuple[str, str], tuple[float, float]] = {
    **{
        (_provider, _model): (_p.input, _p.output)
        for _provider, _models in _PROVIDER_MODELS.items()
        for _model in _models
        if (_p := _pricing.price_for(_model)) is not None
    },
    **{(_provider, "*"): _ZERO_RATE for _provider in _ZERO_MARGINAL_PROVIDERS},
}

# 2026-07-12 (user decision): savings are reported as OPUS-EQUIVALENT only — the
# counterfactual does not vary by complexity. The earlier complexity-tiered
# ("honest") baseline was dropped because it did not reflect how the user
# actually works. WP-05 keeps that decision and finishes it: the flat baseline is
# no longer restated here as a per-complexity table, it is read from the one
# policy in llm_router.pricing. The table shape was itself the hazard — a mapping
# keyed by complexity invites a future edit to make one row differ, which is
# precisely how this file and cost.py ended up 5x apart.
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
    """Counterfactual cost at the one savings baseline.

    ``complexity`` is accepted (callers still pass it, and it stays in the logged
    record) but deliberately does not select a model — see the note above.
    """
    del complexity  # retained in the signature; no longer a baseline input
    try:
        from llm_router import pricing as _pricing

        in_rate, out_rate = _pricing.savings_baseline_rates()
    except Exception:
        # A hook must never crash. Fall open to the current Opus list rate,
        # which is what the policy resolves to — not 0.0, which would log every
        # routed call as having saved nothing.
        in_rate, out_rate = 5.0, 25.0
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


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
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "host": host,
        }

        path = _savings_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")

        # Mirror this DIRECT routing into the SESSION-scoped ledger
        # (~/.llm-router/session_spend.json). llm_session_spend / llm_session_savings
        # read THAT ledger, not usage.db — so without this, a session that routes
        # exclusively through the DIRECT hook path (never the MCP llm_* tools)
        # reports $0 spent / $0 saved even though usage.db recorded real savings.
        # record()        → actual spend (≈$0 for local models) + call_count
        # record_reclaimed → opus-equivalent + net savings (drives the headline #)
        try:
            from llm_router.session_spend import get_session_spend

            _spend = get_session_spend()
            _spend.record(
                model=f"{provider}/{model}",
                tool="direct",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=external_cost,
            )
            _spend.record_reclaimed(
                tokens_reclaimed=input_tokens + output_tokens,
                opus_equivalent_usd=baseline,
                gates_passed=True,
            )
        except Exception:
            pass
    except Exception:
        # Silent — savings logging must never break the routing hook.
        pass


def log_receipt_savings(
    receipt: "Receipt",  # llm_router.receipt_store.Receipt (duck-typed; no import cycle)
    session_id: str,
    *,
    host: str = "router",
) -> None:
    """Append a savings record for a router/gateway-routed call.

    Bridges the receipt path (router.py → receipts.db) into
    ``savings_log.jsonl`` so the dashboard/session-end summary see the same
    calls. Before this bridge existed, gateway-routed turns landed ONLY in
    receipts.db and the JSONL ledger stayed empty (488 receipts vs 0 lines,
    2026-07-12). Savings are opus-equivalent by construction — see
    receipt_store.compute_receipt.

    Fire-and-forget — never raises.
    """
    try:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "task_type": receipt.task_type,
            "complexity": receipt.complexity,
            "estimated_saved": max(0.0, float(receipt.savings_usd)),
            "external_cost": float(receipt.cost_usd),
            "model": receipt.model,
            "input_tokens": int(receipt.input_tokens),
            "output_tokens": int(receipt.output_tokens),
            "host": host,
        }
        path = _savings_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        # Silent — savings logging must never break the routing path.
        pass


def log_direct_to_db(
    result: "DirectResult",
    *,
    prompt: str,
    task_type: str,
    complexity: str,
    classifier_type: str = "hook",
    profile: str = "balanced",
    session_id: str = "",
) -> None:
    """Persist a successful DIRECT routing into the ``usage`` and
    ``routing_decisions`` tables of ``~/.llm-router/usage.db``.

    The DIRECT (hook) path historically only appended to
    ``savings_log.jsonl`` (via :func:`log_direct_savings`), so the two tables
    that the routing view / summary read from — ``usage`` and
    ``routing_decisions`` — stayed frozen whenever the hook answered prompts
    inline instead of routing through the MCP tools. This mirrors what the
    MCP path's ``cost.log_usage`` / ``cost.log_routing_decision`` do, so
    DIRECT-routed turns become visible everywhere the MCP path is.

    Fire-and-forget — never raises. The routing hook stays snappy and robust
    even if the DB is locked or the import graph changes.
    """
    try:
        from llm_router.cost import (
            log_routing_decision as _cost_log_routing_decision,
            log_usage as _cost_log_usage,
        )
        from llm_router.types import LLMResponse, RoutingProfile, TaskType

        provider = result.model.provider
        model = result.model.model
        input_tokens = max(0, int(result.input_tokens or 0))
        output_tokens = max(0, int(result.output_tokens or 0))
        latency_ms = float(result.latency_ms or 0)
        cost_usd = _cost_for(provider, model, input_tokens, output_tokens)

        # Also append to model_tracking.jsonl — the per-decision log the
        # session-end dashboard reads. Without this, gateway/SDK routings land in
        # usage.db (the routing report) but stay invisible in `llm_router summary`.
        try:
            from llm_router.model_tracking import log_routing_decision as _mt_log
            _mt_log(task_type=task_type, complexity=complexity,
                    classification_method=classifier_type,
                    selected_model=model, provider=provider,
                    cost_usd_estimate=cost_usd, notes=classifier_type)
        except Exception:
            pass

        # Map the hook's string fields onto the typed enums the cost API wants,
        # falling back to safe defaults if an unexpected value shows up.
        try:
            _task = TaskType(task_type)
        except ValueError:
            _task = TaskType.QUERY
        try:
            _profile = RoutingProfile(profile)
        except ValueError:
            _profile = RoutingProfile.BALANCED

        response = LLMResponse(
            content=getattr(result, "text", "") or "",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            provider=provider,
        )

        async def _persist() -> None:
            await _cost_log_usage(
                response,
                _task,
                _profile,
                success=True,
                complexity=complexity,
            )
            await _cost_log_routing_decision(
                prompt=prompt,
                task_type=task_type,
                profile=_profile.value,
                classifier_type=classifier_type,
                classifier_model=None,
                classifier_confidence=0.0,
                classifier_latency_ms=0.0,
                complexity=complexity,
                recommended_model=model,
                base_model=model,
                was_downshifted=False,
                budget_pct_used=0.0,
                quality_mode="balanced",
                final_model=model,
                final_provider=provider,
                success=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                reason_code="direct",
            )

        # Persist. The standalone UserPromptSubmit hook is synchronous with no
        # ambient loop, so asyncio.run is correct there. Inside the gateway / SDK
        # a loop IS already running: schedule the coroutine on it (fire-and-forget
        # with a strong reference) instead of dropping it. Previously the running-
        # loop branch did nothing, so gateway/LoopHole routings were never metered
        # (and older builds left an un-awaited coroutine → "never awaited" warning).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            asyncio.run(_persist())
        else:
            task = loop.create_task(_persist())
            _INFLIGHT_PERSISTS.add(task)
            task.add_done_callback(_INFLIGHT_PERSISTS.discard)
    except Exception:
        # Silent — DB persistence must never break the routing hook.
        pass
