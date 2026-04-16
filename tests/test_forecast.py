"""Tests for budget burn rate forecasting."""

import pytest
from datetime import datetime, timedelta

from llm_router.forecast import BurnForecast, get_burn_forecast


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Use a temporary database for testing."""
    db_path = tmp_path / "test_usage.db"
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    # Reset config singleton so it reads the new env vars
    import llm_router.config as config_module
    config_module._config = None
    return db_path


async def _insert_usage_data(provider: str, days: int, cost_per_day: float = 0.01):
    """Helper to insert usage data with specific timestamps."""
    from llm_router import cost

    db = await cost._get_db()
    try:
        now = datetime.now()
        for i in range(days):
            ts = (now - timedelta(days=i)).isoformat()
            await db.execute(
                "INSERT INTO usage (timestamp, model, provider, task_type, profile, input_tokens, output_tokens, cost_usd, latency_ms, success) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, "test-model", provider, "query", "balanced", 100, 50, cost_per_day, 100.0, 1),
            )
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_forecast_no_data(temp_db):
    """Test forecast with no historical data."""
    forecast = await get_burn_forecast()
    assert forecast.projected_monthly_usd == 0.0
    assert forecast.days_to_limit == -1
    assert forecast.pct_remaining == -1.0
    assert forecast.confidence == 0.0
    assert forecast.days_of_data == 0


@pytest.mark.asyncio
async def test_forecast_insufficient_data(temp_db):
    """Test forecast with < 3 days of data (insufficient)."""
    await _insert_usage_data("gemini", days=2, cost_per_day=0.01)

    forecast = await get_burn_forecast()
    assert forecast.days_of_data == 2
    assert forecast.projected_monthly_usd == 0.0  # No projection with < 3 days
    assert forecast.confidence == 0.0


@pytest.mark.asyncio
async def test_forecast_with_data(temp_db):
    """Test forecast with sufficient data."""
    await _insert_usage_data("openai", days=5, cost_per_day=0.02)

    forecast = await get_burn_forecast()
    assert forecast.days_of_data >= 3
    assert forecast.projected_monthly_usd > 0.0
    assert forecast.daily_burn_usd > 0.0
    assert 0.4 <= forecast.confidence <= 1.0  # Confidence increases with more data


@pytest.mark.asyncio
async def test_forecast_with_budget_limit(temp_db):
    """Test forecast with budget limit."""
    await _insert_usage_data("openai", days=5, cost_per_day=0.05)

    # Forecast with budget limit
    forecast = await get_burn_forecast(budget_limit=10.0)
    assert forecast.days_to_limit >= 0  # Should have days remaining
    assert 0 <= forecast.pct_remaining <= 100  # Percentage should be valid


@pytest.mark.asyncio
async def test_forecast_provider_filter(temp_db):
    """Test forecast filtered by provider."""
    # Add data for two providers with different costs
    await _insert_usage_data("openai", days=5, cost_per_day=0.05)
    await _insert_usage_data("gemini", days=5, cost_per_day=0.001)

    # Get forecast for specific provider
    openai_forecast = await get_burn_forecast(provider="openai")
    gemini_forecast = await get_burn_forecast(provider="gemini")

    # Costs should reflect the different prices
    assert openai_forecast.daily_burn_usd > gemini_forecast.daily_burn_usd


@pytest.mark.asyncio
async def test_forecast_budget_exhaustion(temp_db):
    """Test forecast when budget will be exhausted."""
    await _insert_usage_data("openai", days=5, cost_per_day=1.0)  # High cost

    # Set low budget limit
    forecast = await get_burn_forecast(budget_limit=10.0)
    assert forecast.days_to_limit >= 0  # Should show when budget exhausts
    assert forecast.pct_remaining < 100  # Budget is partially used


def test_burn_forecast_dataclass():
    """Test BurnForecast dataclass creation and immutability."""
    forecast = BurnForecast(
        projected_monthly_usd=100.0,
        days_to_limit=5,
        pct_remaining=50.0,
        confidence=0.8,
        daily_burn_usd=3.0,
        days_of_data=7,
    )

    assert forecast.projected_monthly_usd == 100.0
    assert forecast.days_to_limit == 5
    assert forecast.confidence == 0.8

    # Verify immutability (frozen dataclass)
    with pytest.raises(AttributeError):
        forecast.confidence = 0.9


@pytest.mark.asyncio
async def test_forecast_empty_days_handling(temp_db):
    """Test forecast handles gaps in daily data gracefully."""
    # Add data on only 3 days (creates gaps)
    await _insert_usage_data("openai", days=3, cost_per_day=0.05)

    # Should still work with 3 data points
    forecast = await get_burn_forecast()
    assert forecast.days_of_data == 3
    assert forecast.projected_monthly_usd >= 0.0
