"""WP-13 follow-on — an unaccountable spend component must not read as zero.

Owner decision 2026-08-12: fail CLOSED.

`_rejected_attempt_spend` sums billable-but-rejected provider attempts from the
execution ledger (RED1-08). `get_daily_spend()` returns `winning + rejected`, and
the router compares that total against the configured cap at four sites.

It previously returned 0.0 on any read error, which UNDER-REPORTS spend, so the
cap check passes. A guard that cannot read the ledger did not reject — it
silently approved. That is the same failure direction as the budget TOCTOU race
(#19) and the savings query that rendered "$0.00 saved": failing in the direction
that looks harmless, which is exactly why it survives.

Failing closed returns `inf`, so every cap comparison denies. Routing continues —
free and local providers are unaffected — but money is not spent against a total
we cannot account for. The two DISPLAY consumers must render "Unknown" rather
than the literal `inf`: a fabricated infinity on a dashboard is its own lie.
"""

from __future__ import annotations

import math

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Part 0 rule 4: assert the resolved DB path is inside the tmpdir.

    LLM_ROUTER_HOME does not isolate cost._get_db(); that gap destroyed real data
    once during this audit, so the assertion is the point.
    """
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    assert str(db).startswith(str(tmp_path))
    yield


@pytest.mark.asyncio
async def test_unreadable_rejected_spend_fails_closed():
    """The core assertion: an unreadable component must not be counted as 0."""
    from llm_router import cost

    class _BrokenDB:
        async def execute(self, *a, **k):
            raise RuntimeError("ledger unreadable")

    spend = await cost._rejected_attempt_spend(_BrokenDB(), "day")

    assert spend != 0.0, "unreadable ledger read as zero spend — the cap passes"
    assert math.isinf(spend), f"expected inf to force the cap closed, got {spend}"


@pytest.mark.asyncio
async def test_a_readable_empty_ledger_is_still_zero():
    """Fail-closed must not turn 'genuinely nothing spent' into a denial —
    otherwise a fresh install can never route to a paid model."""
    from llm_router import cost

    db = await cost._get_db()
    try:
        spend = await cost._rejected_attempt_spend(db, "day")
    finally:
        await db.close()

    assert spend == 0.0, f"empty ledger should be a real zero, got {spend}"


@pytest.mark.asyncio
async def test_the_failure_is_counted_not_just_denied():
    """A denial with no cause is an outage nobody can diagnose."""
    from llm_router import cost, failopen

    failopen.clear()
    failopen.reset_cache()

    class _BrokenDB:
        async def execute(self, *a, **k):
            raise RuntimeError("ledger unreadable")

    await cost._rejected_attempt_spend(_BrokenDB(), "day")
    failopen.reset_cache()

    assert failopen.snapshot().by_code.get("CHZ-FO-COST-CAP-LEDGER-READ", 0) >= 1


def test_display_renders_unknown_not_infinity():
    """`inf` is correct for a cap COMPARISON and wrong for a dashboard. A user
    told they spent $inf learns less than one told 'Unknown'."""
    from llm_router.cost import format_spend_for_display

    assert format_spend_for_display(float("inf")) == "Unknown"
    assert format_spend_for_display(float("nan")) == "Unknown"
    assert "1.23" in format_spend_for_display(1.2345)


# ── Envelope release: the leak must be recoverable, not permanent ────────────

@pytest.mark.asyncio
async def test_release_retries_once_before_giving_up():
    """A transient backend blip should not strand budget permanently."""
    from llm_router.quota_envelope_routing import release_envelope

    calls = {"n": 0}

    class _FlakyBackend:
        async def release(self, key, amount):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return None

    await release_envelope("k", 0.04, backend=_FlakyBackend())
    assert calls["n"] == 2, "release did not retry after a transient failure"


@pytest.mark.asyncio
async def test_a_stranded_reservation_records_its_amount():
    """'release failed' is unactionable; '$0.04 stranded on key k' can be
    reconciled. Without the amount, a slow leak only ever presents as 'routing
    got stingy for no reason'."""
    from llm_router import failopen
    from llm_router.quota_envelope_routing import release_envelope

    failopen.clear()
    failopen.reset_cache()

    class _DeadBackend:
        async def release(self, key, amount):
            raise RuntimeError("backend down")

    await release_envelope("k", 0.04, backend=_DeadBackend())  # must not raise
    failopen.reset_cache()

    snap = failopen.snapshot()
    assert snap.by_code.get("CHZ-FO-ENVELOPE-STRANDED", 0) == 1
    raw = failopen.store_path().read_text()
    assert "0.040000" in raw, f"stranded amount not recorded: {raw}"
