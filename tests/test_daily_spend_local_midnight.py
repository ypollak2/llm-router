"""`get_daily_spend` must bucket by the LOCAL calendar day, against a real DB.

tests/test_daily_cap.py mocks get_daily_spend and tests/test_cost.py never crosses a
day boundary, so changing `date(timestamp,'localtime') = date('now','localtime')` to
UTC passed both (independent review, 2026-10-08).

SQLite cannot be frozen, so the timezone is chosen per run: +12h when the UTC hour is
>= 12, else -12h. In both cases the local date differs from the UTC date for the
whole run, which guarantees rows straddling local midnight fall on opposite sides of
a UTC-day bucket whatever time CI runs.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from llm_router import cost


def _set_tz(monkeypatch, tz: str) -> None:
    monkeypatch.setenv("TZ", tz)
    time.tzset()


@pytest.fixture
def _restore_tz():
    import os

    old = os.environ.get("TZ")
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.asyncio
async def test_daily_spend_buckets_by_local_day_not_utc(temp_db, monkeypatch, _restore_tz):
    now = datetime.now(timezone.utc)
    if now.hour >= 12:
        tz, off = "Etc/GMT-12", timedelta(hours=12)   # UTC+12
    else:
        tz, off = "Etc/GMT+12", timedelta(hours=-12)  # UTC-12
    _set_tz(monkeypatch, tz)
    local_today = (now + off).date()

    # (day offset from local today, local wall-clock hh:mm, cost). Powers of two so the
    # expected total identifies exactly which rows were counted.
    rows = [(-1, (0, 30), 1.0), (-1, (23, 30), 2.0),
            (0, (0, 30), 4.0), (0, (23, 30), 8.0),
            (1, (0, 30), 16.0), (1, (23, 30), 32.0)]
    db = await cost._get_db()
    try:
        for day, (h, m), usd in rows:
            local = datetime.combine(local_today + timedelta(days=day),
                                     datetime.min.time()) + timedelta(hours=h, minutes=m)
            ts = (local - off).strftime("%Y-%m-%d %H:%M:%S")  # stored as UTC
            await db.execute(
                "INSERT INTO usage (timestamp, model, provider, task_type, profile, "
                "input_tokens, output_tokens, cost_usd, latency_ms, success) "
                "VALUES (?, 'm', 'p', 'query', 'balanced', 1, 1, ?, 1, 1)", (ts, usd))
        await db.commit()
    finally:
        await db.close()

    # Only the two rows on the local date count: 4 + 8. A UTC bucket would give a
    # different, row-set-dependent sum (never 12 here).
    assert await cost.get_daily_spend(include_simulated=True) == pytest.approx(12.0)
