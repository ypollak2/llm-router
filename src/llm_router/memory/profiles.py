"""Build and manage learned routing profiles from correction history.

A learned profile is built from the corrections table: when users override
routing with llm_reroute, we aggregate by task_type and confidence. After
3 corrections for the same task_type → lock a hard routing override.

This enables the router to learn from user behavior and personalize routing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class LearnedRoute:
    """A learned routing override with confidence tracking."""

    model: str
    """The model to override to (e.g. 'claude-opus-4-6', 'openai/gpt-4o')"""

    confidence: int
    """Confidence level (1-3): how many corrections support this route"""

    source: str
    """Where the learning came from: 'corrections' or 'feedback'"""

    last_correction: str
    """ISO timestamp of the most recent correction for this task"""


LEARNED_ROUTES_FILE = Path.home() / ".llm-router" / "learned_routes.json"
DB_PATH = Path.home() / ".llm-router" / "usage.db"

# Threshold: require 3 corrections before locking a route
CONFIDENCE_THRESHOLD = 3


def fetch_corrections_history(days: int = 30) -> list[dict]:
    """Fetch corrections from the last N days.

    Args:
        days: Number of days to look back (default 30)

    Returns:
        List of correction dicts from corrections table
    """
    if not DB_PATH.exists():
        return []

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        rows = conn.execute(
            """
            SELECT * FROM corrections
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
            """,
            (cutoff,),
        ).fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def build_learned_profile() -> dict[str, LearnedRoute]:
    """Build a learned profile by aggregating corrections by task_type.

    Strategy:
    1. Fetch corrections history (last 30 days)
    2. Group by task_type and corrected_model
    3. Count corrections per (task_type, model) pair
    4. Create LearnedRoute if count >= CONFIDENCE_THRESHOLD

    Returns:
        Dict mapping task_type → LearnedRoute (only if confidence >= 3)
    """
    corrections = fetch_corrections_history(days=30)

    if not corrections:
        return {}

    # Group by task_type and corrected_model
    # Format: {(task_type, model): {"count": N, "last": datetime_str}}
    routes: dict[tuple[str, str], dict] = {}

    for corr in corrections:
        task_type = corr.get("original_tool", "unknown")
        corrected_model = corr.get("corrected_model", "unknown")

        key = (task_type, corrected_model)
        if key not in routes:
            routes[key] = {"count": 0, "last": corr.get("timestamp", "")}

        routes[key]["count"] += 1
        # Keep most recent timestamp
        if corr.get("timestamp", "") > routes[key]["last"]:
            routes[key]["last"] = corr.get("timestamp", "")

    # Filter to only routes with sufficient confidence
    learned = {}
    for (task_type, model), data in routes.items():
        if data["count"] >= CONFIDENCE_THRESHOLD:
            learned[task_type] = LearnedRoute(
                model=model,
                confidence=data["count"],
                source="corrections",
                last_correction=data["last"],
            )

    return learned


def save_learned_profile(profile: dict[str, LearnedRoute]) -> Path:
    """Save learned profile to ~/.llm-router/learned_routes.json.

    Format:
    ```json
    {
        "security_review": {
            "model": "claude-opus-4-6",
            "confidence": 3,
            "source": "corrections",
            "last_correction": "2026-04-17T15:30:00+00:00"
        }
    }
    ```

    Args:
        profile: Learned profile dict

    Returns:
        Path to written file
    """
    LEARNED_ROUTES_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Convert dataclasses to dicts for JSON serialization
    data = {
        task: {
            "model": route.model,
            "confidence": route.confidence,
            "source": route.source,
            "last_correction": route.last_correction,
        }
        for task, route in profile.items()
    }

    LEARNED_ROUTES_FILE.write_text(json.dumps(data, indent=2))
    return LEARNED_ROUTES_FILE


def load_learned_profile() -> dict[str, LearnedRoute]:
    """Load learned profile from ~/.llm-router/learned_routes.json.

    Returns:
        Dict mapping task_type → LearnedRoute (empty dict if file not found)
    """
    if not LEARNED_ROUTES_FILE.exists():
        return {}

    try:
        data = json.loads(LEARNED_ROUTES_FILE.read_text())
        return {
            task: LearnedRoute(
                model=route["model"],
                confidence=route["confidence"],
                source=route["source"],
                last_correction=route["last_correction"],
            )
            for task, route in data.items()
        }
    except (json.JSONDecodeError, KeyError):
        return {}
