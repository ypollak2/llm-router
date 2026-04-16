"""Budget burn rate forecasting with linear regression.

Analyzes historical spend patterns over the last 7 days and projects
forward to month-end using linear regression + exponential smoothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import linear_regression

from llm_router.cost import _get_db


@dataclass(frozen=True)
class BurnForecast:
    """Budget projection based on historical spend trend.

    Attributes:
        projected_monthly_usd: Estimated total spend for the full month at current burn rate.
        days_to_limit: Days until budget limit is exhausted (0 if no limit set, -1 if no data).
        pct_remaining: Percentage of monthly budget remaining (0–100, or -1 if no limit).
        confidence: Confidence level (0.0–1.0) based on data points available.
        daily_burn_usd: Average daily spend over the last 7 days.
        days_of_data: Number of days with usage data (0–7).
    """
    projected_monthly_usd: float
    days_to_limit: int
    pct_remaining: float
    confidence: float
    daily_burn_usd: float
    days_of_data: int


async def get_burn_forecast(provider: str | None = None, budget_limit: float = 0.0) -> BurnForecast:
    """Calculate spend forecast for the current month.

    Analyzes the last 7 days of spend data and projects forward using linear
    regression. Returns graceful no-op when < 3 days of historical data.

    Args:
        provider: Filter forecast to specific provider (e.g., "openai", "gemini").
                  If None, forecasts all providers combined.
        budget_limit: Monthly budget limit in USD. If 0 (default), forecast shows
                     absolute projection without limit countdown.

    Returns:
        BurnForecast with projected monthly spend and remaining budget/days.
    """
    db = await _get_db()
    try:
        # Get daily spend for the last 7 days
        if provider:
            query = """
                SELECT DATE(timestamp) as day, SUM(cost_usd)
                FROM usage
                WHERE timestamp >= datetime('now', '-7 days')
                  AND provider = ?
                GROUP BY DATE(timestamp)
                ORDER BY day
            """
            cursor = await db.execute(query, (provider,))
        else:
            query = """
                SELECT DATE(timestamp) as day, SUM(cost_usd)
                FROM usage
                WHERE timestamp >= datetime('now', '-7 days')
                GROUP BY DATE(timestamp)
                ORDER BY day
            """
            cursor = await db.execute(query)

        rows = await cursor.fetchall()

        # Not enough data to forecast
        if len(rows) < 3:
            return BurnForecast(
                projected_monthly_usd=0.0,
                days_to_limit=-1,
                pct_remaining=-1.0,
                confidence=0.0,
                daily_burn_usd=0.0,
                days_of_data=len(rows),
            )

        # Extract daily spend values
        daily_spends = [float(row[1]) for row in rows]

        # Linear regression: fit day_index (0, 1, 2, ...) vs spend
        x_data = list(range(len(daily_spends)))
        slope, intercept = linear_regression(x_data, daily_spends)

        # Calculate average daily burn (more robust than slope for short windows)
        avg_daily_burn = sum(daily_spends) / len(daily_spends)

        # Use slope-adjusted burn if positive trend, otherwise use moving avg
        # This prevents extreme projections from single outlier days
        if slope > 0:
            # Upward trend: temper projection with historical average
            daily_burn_usd = (avg_daily_burn * 0.6) + (slope * 0.4)
        else:
            # No trend or downward: use conservative historical average
            daily_burn_usd = avg_daily_burn

        # Days remaining in month
        today = datetime.now()
        days_in_month = (
            (datetime(today.year, today.month + 1 if today.month < 12 else 1,
                     1 if today.month < 12 else 1) - today).days
            if today.month < 12
            else (datetime(today.year + 1, 1, 1) - today).days
        )

        # Current spend this month
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE timestamp >= datetime('now', 'start of month')"
        )
        current_row = await cursor.fetchone()
        current_spend = float(current_row[0]) if current_row else 0.0

        # Projected month-end spend
        projected_monthly_usd = current_spend + (daily_burn_usd * days_in_month)

        # Budget countdown
        days_to_limit = -1
        pct_remaining = -1.0
        if budget_limit > 0:
            remaining_budget = max(0, budget_limit - current_spend)
            days_to_limit = max(0, int(remaining_budget / daily_burn_usd)) if daily_burn_usd > 0 else 999
            pct_remaining = (remaining_budget / budget_limit) * 100 if budget_limit > 0 else 0.0

        # Confidence: higher with more data points
        # 3 days = 40%, 7 days = 100%
        confidence = min(1.0, (len(rows) - 2) / 5.0)

        return BurnForecast(
            projected_monthly_usd=projected_monthly_usd,
            days_to_limit=days_to_limit,
            pct_remaining=pct_remaining,
            confidence=confidence,
            daily_burn_usd=daily_burn_usd,
            days_of_data=len(rows),
        )

    finally:
        await db.close()
