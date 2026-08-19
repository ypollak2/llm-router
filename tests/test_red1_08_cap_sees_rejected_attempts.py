"""Regression: RED1-08 — cap-check must see billable-but-rejected attempts.

cost.log_usage records only the WINNING attempt to the `usage` table, so a paid
model that was tried, billed, then rejected (contract gate or quality escalation)
was invisible to get_daily_spend*() — the exact functions the TQ-007 cap-check
reads. A cap could therefore be crossed by the value of rejected paid attempts
before it tripped.

The execution ledger records every attempt with `rejected` + `measured_cost_usd`;
the winning attempt is separately marked `accepted`. get_daily_spend*() now adds
the sum of `rejected=1` rows to the `usage` total — the extra cost missing from
`usage`, with no double-count of the winner.
"""

from __future__ import annotations

import time

import pytest

from llm_router import cost
from llm_router.execution_ledger import LedgerEvent, record_event


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))
    # Force cost._get_db to use this path.
    from llm_router import config as _cfg
    monkeypatch.setattr(_cfg, "get_config", _cfg.get_config)  # no-op anchor
    return db


async def _seed_usage_winner(db_path, cost_usd, task_type="code"):
    """Record a winning call via the real cost.log_usage path (correct schema)."""
    from llm_router.types import LLMResponse, RoutingProfile, TaskType
    resp = LLMResponse(
        content="ok", model="openai/gpt-4o", input_tokens=10, output_tokens=5,
        cost_usd=cost_usd, latency_ms=12.0, provider="openai",
    )
    await cost.log_usage(resp, TaskType(task_type), RoutingProfile.BALANCED)


def test_rejected_paid_attempt_counts_toward_daily_spend(isolated_db, monkeypatch):
    import asyncio

    async def go():
        # Winning accepted call: $0.10 in usage.
        await _seed_usage_winner(isolated_db, 0.10, "code")
        # A rejected paid attempt: $0.05, recorded to the ledger only.
        record_event(LedgerEvent(
            ts=time.time(), event_type="route_attempt", task_type="code",
            provider="openai", model="openai/gpt-4o",
            measured_cost_usd=0.05, rejected=True, accepted=False,
        ))
        # The winning attempt is ALSO in the ledger (accepted) — must NOT be
        # double-counted by the rejected-sum.
        record_event(LedgerEvent(
            ts=time.time(), event_type="route_attempt", task_type="code",
            provider="ollama", model="ollama/x",
            measured_cost_usd=0.10, rejected=False, accepted=True,
        ))
        total = await cost.get_daily_spend()
        by_task = await cost.get_daily_spend_by_task_type("code")
        return total, by_task

    total, by_task = asyncio.run(go())
    # usage winner (0.10) + rejected attempt (0.05) = 0.15; accepted ledger row
    # must NOT be added again.
    assert abs(total - 0.15) < 1e-9, f"expected 0.15 (0.10 winner + 0.05 rejected), got {total}"
    assert abs(by_task - 0.15) < 1e-9, f"by-task expected 0.15, got {by_task}"


def test_no_rejected_attempts_is_unchanged(isolated_db):
    import asyncio

    async def go():
        await _seed_usage_winner(isolated_db, 0.20, "query")
        return await cost.get_daily_spend()

    total = asyncio.run(go())
    assert abs(total - 0.20) < 1e-9, f"with no rejected attempts, spend must equal usage: {total}"


def test_monthly_spend_also_counts_rejected_attempts(isolated_db, monkeypatch):
    """RED1-2-01: get_monthly_spend must include rejected-attempt spend, like daily."""
    import asyncio
    import time as _time

    async def go():
        await _seed_usage_winner(isolated_db, 0.10, "code")
        record_event(LedgerEvent(
            ts=_time.time(), event_type="route_attempt", task_type="code",
            provider="openai", model="openai/gpt-4o",
            measured_cost_usd=50.0, rejected=True, accepted=False,
        ))
        return await cost.get_daily_spend(), await cost.get_monthly_spend()

    daily, monthly = asyncio.run(go())
    # Both ceilings must see the $50 rejected attempt (+ $0.10 winner).
    assert monthly >= 50.0, f"RED1-2-01: monthly cap blind to rejected spend (monthly={monthly})"
    assert abs(daily - monthly) < 1e-9, (
        f"daily and monthly must agree on rejected spend: daily={daily} monthly={monthly}"
    )
