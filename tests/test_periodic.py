"""Tests for mid-session periodic monitoring.

Tests cover:
- Hourly snapshot capture
- Trend analysis and gap emergence detection
- Accuracy degradation tracking
- Snapshot persistence and loading
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from llm_router.monitoring.periodic import (
    analyze_session_trends,
    cleanup_old_snapshots,
    format_trend_summary,
    get_current_snapshot,
    load_session_snapshots,
    save_session_snapshot,
    take_session_snapshot,
)


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def sample_decisions() -> list[dict]:
    """Sample routing decisions for testing."""
    return [
        {
            "id": 1,
            "timestamp": "2026-04-17T14:30:00+00:00",
            "task_type": "code",
            "classifier_confidence": 0.95,
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "cost_usd": 0.0001,
            "saved_usd": 0.001,
            "judge_score": 0.95,
        },
        {
            "id": 2,
            "timestamp": "2026-04-17T14:31:00+00:00",
            "task_type": "security_review",
            "classifier_confidence": 0.25,
            "recommended_model": "haiku",
            "final_model": "haiku",
            "success": 1,
            "cost_usd": 0.0002,
            "saved_usd": 0.0005,
            "judge_score": None,
        },
    ]


@pytest.fixture
def sample_corrections() -> list[dict]:
    """Sample manual corrections."""
    return [
        {"decision_id": 2, "reason": "User escalated to opus"},
    ]


# ── Snapshot Creation Tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_take_session_snapshot_basic(sample_decisions, sample_corrections):
    """Test basic snapshot capture."""
    start = datetime(2026, 4, 17, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 17, 14, 32, tzinfo=timezone.utc)

    with mock.patch("llm_router.monitoring.periodic.fetch_session_decisions") as mock_fetch:
        with mock.patch("llm_router.monitoring.periodic.fetch_session_corrections") as mock_corr:
            with mock.patch("llm_router.monitoring.periodic.analyze_facts") as mock_facts:
                with mock.patch("llm_router.monitoring.periodic.analyze_gaps") as mock_gaps:
                    with mock.patch("llm_router.monitoring.periodic.classify_root_causes") as mock_causes:
                        with mock.patch("llm_router.monitoring.periodic.generate_actions") as mock_actions:
                            mock_fetch.return_value = sample_decisions
                            mock_corr.return_value = sample_corrections
                            mock_facts.return_value = {
                                "total_calls": 2,
                                "total_cost": 0.0003,
                                "total_saved": 0.0015,
                                "classification_accuracy": 0.95,
                                "duration_min": 2,
                            }
                            mock_gaps.return_value = [
                                {
                                    "id": 2,
                                    "task_type": "security_review",
                                    "flags": ["MANUAL_OVERRIDE"],
                                }
                            ]
                            mock_causes.return_value = []
                            mock_actions.return_value = []

                            snapshot = await take_session_snapshot(start, end, 1)

                            assert snapshot["hour"] == 1
                            assert snapshot["facts"]["total_calls"] == 2
                            assert snapshot["facts"]["accuracy"] == 0.95
                            assert snapshot["gap_count"] == 1
                            assert snapshot["action_count"] == 0


# ── Snapshot Persistence Tests ─────────────────────────────────────────────

def test_save_session_snapshot(tmp_path, monkeypatch):
    """Test snapshot file persistence."""
    monkeypatch.setattr(
        "llm_router.monitoring.periodic.SNAPSHOT_DIR",
        tmp_path / "snapshots",
    )

    snapshot = {
        "hour": 2,
        "timestamp": "2026-04-17T15:00:00Z",
        "facts": {
            "total_calls": 23,
            "total_cost": 1.23,
            "total_saved": 45.67,
            "accuracy": 0.91,
            "duration_min": 60,
        },
        "gap_count": 1,
        "action_count": 1,
        "top_gap": "security_review",
    }

    path = save_session_snapshot(snapshot)

    assert path.exists()
    assert path.parent == tmp_path / "snapshots"
    assert ".json" in path.name

    # Verify content
    loaded = json.loads(path.read_text())
    assert loaded["hour"] == 2
    assert loaded["facts"]["total_calls"] == 23


def test_load_session_snapshots_empty(tmp_path, monkeypatch):
    """Test loading snapshots when none exist."""
    monkeypatch.setattr(
        "llm_router.monitoring.periodic.SNAPSHOT_DIR",
        tmp_path / "snapshots",
    )

    snapshots = load_session_snapshots()
    assert snapshots == []


def test_load_session_snapshots_multiple(tmp_path, monkeypatch):
    """Test loading multiple snapshots for a session."""
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "llm_router.monitoring.periodic.SNAPSHOT_DIR",
        snapshot_dir,
    )

    # Create multiple snapshots
    for hour in [1, 2, 3]:
        snapshot = {
            "hour": hour,
            "timestamp": f"2026-04-17T{14 + hour}:00:00Z",
            "facts": {"accuracy": 0.95 - hour * 0.01},
            "gap_count": 0,
            "action_count": 0,
        }
        filename = f"2026-04-17-{hour:02d}h.json"
        (snapshot_dir / filename).write_text(json.dumps(snapshot))

    snapshots = load_session_snapshots("2026-04-17")

    assert len(snapshots) == 3
    assert snapshots[0]["hour"] == 1
    assert snapshots[1]["hour"] == 2
    assert snapshots[2]["hour"] == 3


# ── Trend Analysis Tests ────────────────────────────────────────────────────

def test_analyze_session_trends_improving():
    """Test trend detection for improving accuracy."""
    snapshots = [
        {
            "hour": 1,
            "facts": {"accuracy": 0.85},
            "gap_count": 2,
        },
        {
            "hour": 2,
            "facts": {"accuracy": 0.90},
            "gap_count": 1,
        },
        {
            "hour": 3,
            "facts": {"accuracy": 0.95},
            "gap_count": 0,
        },
    ]

    trend = analyze_session_trends(snapshots)

    assert trend["trend_type"] == "improving"
    assert trend["accuracy_change"] == pytest.approx(0.10, abs=0.01)
    assert trend["first_accuracy"] == 0.85
    assert trend["last_accuracy"] == 0.95
    assert len(trend["gap_emergence"]) >= 0
    assert not trend["concerning"]


def test_analyze_session_trends_degrading():
    """Test trend detection for degrading accuracy."""
    snapshots = [
        {
            "hour": 1,
            "facts": {"accuracy": 0.95},
            "gap_count": 0,
        },
        {
            "hour": 2,
            "facts": {"accuracy": 0.90},
            "gap_count": 1,
        },
        {
            "hour": 3,
            "facts": {"accuracy": 0.80},
            "gap_count": 2,
        },
    ]

    trend = analyze_session_trends(snapshots)

    assert trend["trend_type"] == "degrading"
    assert trend["accuracy_change"] == pytest.approx(-0.15, abs=0.01)
    assert trend["concerning"]


def test_analyze_session_trends_gap_emergence():
    """Test gap emergence detection."""
    snapshots = [
        {
            "hour": 1,
            "facts": {"accuracy": 0.95},
            "gap_count": 0,
        },
        {
            "hour": 2,
            "facts": {"accuracy": 0.95},
            "gap_count": 2,  # Gap emergence
        },
    ]

    trend = analyze_session_trends(snapshots)

    assert len(trend["gap_emergence"]) >= 1
    assert trend["gap_emergence"][0]["hour"] == 2
    assert trend["gap_emergence"][0]["gap_count"] == 2


def test_analyze_session_trends_empty():
    """Test trend analysis with empty snapshots."""
    trend = analyze_session_trends([])

    assert trend["trend_type"] == "none"
    assert trend["accuracy_change"] == 0.0
    assert trend["gap_emergence"] == []
    assert not trend["concerning"]


# ── Output Formatting Tests ────────────────────────────────────────────────

def test_format_trend_summary_improving():
    """Test trend summary formatting for improving accuracy."""
    trend = {
        "trend_type": "improving",
        "accuracy_change": 0.10,
        "first_accuracy": 0.85,
        "last_accuracy": 0.95,
        "gap_emergence": [],
        "concerning": False,
        "snapshot_count": 3,
    }

    output = format_trend_summary(trend)

    assert "85%" in output
    assert "95%" in output
    assert "+10pp" in output
    assert "improving" in output.lower() or "↑" in output


def test_format_trend_summary_concerning():
    """Test trend summary formatting for concerning patterns."""
    trend = {
        "trend_type": "degrading",
        "accuracy_change": -0.10,
        "first_accuracy": 0.95,
        "last_accuracy": 0.85,
        "gap_emergence": [
            {"hour": 2, "gap_count": 1},
            {"hour": 3, "gap_count": 2, "delta": 1},
        ],
        "concerning": True,
        "snapshot_count": 3,
    }

    output = format_trend_summary(trend)

    assert "degrading" in output.lower() or "↓" in output
    assert "⚠" in output


def test_format_trend_summary_empty():
    """Test trend summary formatting with no snapshots."""
    trend = {
        "snapshot_count": 0,
        "trend_type": "none",
        "accuracy_change": 0.0,
        "gap_emergence": [],
        "concerning": False,
    }

    output = format_trend_summary(trend)

    assert "No snapshots" in output


# ── Snapshot Retrieval Tests ──────────────────────────────────────────────

def test_get_current_snapshot_with_existing(tmp_path, monkeypatch):
    """Test retrieving current snapshot when snapshots exist."""
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "llm_router.monitoring.periodic.SNAPSHOT_DIR",
        snapshot_dir,
    )

    # Create a snapshot
    snapshot = {
        "hour": 2,
        "timestamp": "2026-04-17T15:00:00Z",
        "facts": {"accuracy": 0.92},
        "gap_count": 1,
        "action_count": 0,
    }
    filename = f"{datetime.now().strftime('%Y-%m-%d')}-02h.json"
    (snapshot_dir / filename).write_text(json.dumps(snapshot))

    current = get_current_snapshot()

    assert current["hour"] == 2
    assert current["facts"]["accuracy"] == 0.92


def test_get_current_snapshot_empty(tmp_path, monkeypatch):
    """Test retrieving current snapshot when none exist."""
    monkeypatch.setattr(
        "llm_router.monitoring.periodic.SNAPSHOT_DIR",
        tmp_path / "snapshots",
    )

    current = get_current_snapshot()

    assert current["hour"] == 1
    assert current["gap_count"] == 0
    assert "timestamp" in current


# ── Cleanup Tests ──────────────────────────────────────────────────────────

def test_cleanup_old_snapshots(tmp_path, monkeypatch):
    """Test cleanup of old snapshot files."""
    import os

    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "llm_router.monitoring.periodic.SNAPSHOT_DIR",
        snapshot_dir,
    )

    # Create an old snapshot file
    old_file = snapshot_dir / "2026-04-10-01h.json"
    old_file.write_text("{}")
    # Set modification time to 8 days ago
    old_mtime = datetime.now().timestamp() - (8 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    # Create a recent snapshot
    recent_file = snapshot_dir / "2026-04-17-01h.json"
    recent_file.write_text("{}")

    # Clean up snapshots older than 7 days
    deleted = cleanup_old_snapshots(keep_days=7)

    assert deleted == 1
    assert not old_file.exists()
    assert recent_file.exists()
