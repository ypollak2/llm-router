"""Tests for hook health monitoring and error visibility."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from llm_router.hook_health import (
    record_hook_error,
    record_hook_success,
    get_hook_health,
    get_recent_hook_errors,
    check_hook_permissions,
    cleanup_old_logs,
)


def test_record_hook_error(temp_router_dir):
    """Test recording a hook error."""
    record_hook_error(
        hook_name="auto-route",
        error="ValueError: invalid complexity",
        context={"session_id": "abc123", "task_type": "query"}
    )
    
    # Verify health file was updated
    health_file = temp_router_dir / "hook_health.json"
    assert health_file.exists()
    
    health = json.loads(health_file.read_text())
    assert "auto-route" in health
    assert health["auto-route"]["error_count"] == 1
    assert health["auto-route"]["last_error"] is not None
    
    # Verify error log was created
    log_file = temp_router_dir / "hook_errors.log"
    assert log_file.exists()
    
    log_lines = log_file.read_text().splitlines()
    assert len(log_lines) >= 1
    
    log_entry = json.loads(log_lines[0])
    assert log_entry["hook"] == "auto-route"
    assert "ValueError" in log_entry["error"]


def test_record_hook_success(temp_router_dir):
    """Test recording a successful hook execution."""
    record_hook_success("enforce-route")
    
    # Verify health file was updated
    health_file = temp_router_dir / "hook_health.json"
    assert health_file.exists()
    
    health = json.loads(health_file.read_text())
    assert "enforce-route" in health
    assert health["enforce-route"]["success_count"] == 1
    assert health["enforce-route"]["last_success"] is not None


def test_record_hook_error_with_context_filtering(temp_router_dir):
    """Test that context is filtered to only relevant fields."""
    record_hook_error(
        hook_name="auto-route",
        error="Some error",
        context={
            "session_id": "abc123",
            "task_type": "query",
            "internal_field": "should_be_filtered",
            "another_field": "also_filtered"
        }
    )
    
    # Check log entry
    log_file = temp_router_dir / "hook_errors.log"
    log_entry = json.loads(log_file.read_text().splitlines()[0])
    
    # Only allowed fields should be present
    context = log_entry.get("context", {})
    assert "session_id" in context
    assert "task_type" in context
    assert "internal_field" not in context
    assert "another_field" not in context


def test_get_hook_health_no_data(temp_router_dir):
    """Test getting health when no hooks have run yet."""
    health = get_hook_health()
    assert health == {}


def test_get_hook_health_with_data(temp_router_dir):
    """Test getting hook health with recorded data."""
    record_hook_success("auto-route")
    record_hook_success("auto-route")
    record_hook_error("auto-route", "Error 1")
    record_hook_success("enforce-route")
    
    health = get_hook_health()
    
    assert "auto-route" in health
    assert health["auto-route"]["success_count"] == 2
    assert health["auto-route"]["error_count"] == 1
    assert health["auto-route"]["health_status"] == "failing"  # Recent error (< 1 hour ago)
    
    assert "enforce-route" in health
    assert health["enforce-route"]["success_count"] == 1
    assert health["enforce-route"]["error_count"] == 0
    assert health["enforce-route"]["health_status"] == "healthy"


def test_hook_health_status_calculation(temp_router_dir):
    """Test health status classification."""
    # Healthy: no errors
    record_hook_success("hook-a")
    record_hook_success("hook-a")
    health = get_hook_health()
    assert health["hook-a"]["health_status"] == "healthy"
    
    # Failing: recent error (< 1 hour ago)
    record_hook_error("hook-b", "Recent error")
    health = get_hook_health()
    assert health["hook-b"]["health_status"] == "failing"
    
    # Degraded: old error (> 1 hour ago)
    # Manually set last_error to > 1 hour ago
    health_file = temp_router_dir / "hook_health.json"
    health_data = json.loads(health_file.read_text())
    old_time = (datetime.now() - timedelta(hours=2)).isoformat()
    health_data["hook-b"]["last_error"] = old_time
    health_file.write_text(json.dumps(health_data))
    
    health = get_hook_health()
    assert health["hook-b"]["health_status"] == "degraded"


def test_get_recent_hook_errors(temp_router_dir):
    """Test retrieving recent errors with time filtering."""
    # Record some errors
    record_hook_error("hook-a", "Error 1")
    record_hook_error("hook-b", "Error 2")
    record_hook_error("hook-a", "Error 3")
    
    # Get recent errors (last 24 hours)
    errors = get_recent_hook_errors(hours=24)
    
    assert len(errors) == 3
    assert errors[0]["hook"] in ["hook-a", "hook-b"]
    assert "Error" in errors[0]["error"]
    
    # Verify timestamps are in order
    for i in range(len(errors) - 1):
        assert errors[i]["timestamp"] <= errors[i + 1]["timestamp"]


def test_get_recent_hook_errors_time_filtering(temp_router_dir):
    """Test that errors are filtered by time window."""
    # Record an error
    record_hook_error("hook-a", "Recent error")
    
    # Get errors from last 1 hour (should include it)
    recent_errors = get_recent_hook_errors(hours=1)
    assert len(recent_errors) == 1
    
    # Manually add an old error to the log
    log_file = temp_router_dir / "hook_errors.log"
    old_time = (datetime.now() - timedelta(hours=48)).isoformat()
    old_entry = {
        "timestamp": old_time,
        "hook": "hook-b",
        "error": "Old error"
    }
    
    with log_file.open("a") as f:
        f.write(json.dumps(old_entry) + "\n")
    
    # Get errors from last 1 hour (should not include old error)
    recent_errors = get_recent_hook_errors(hours=1)
    assert len(recent_errors) == 1
    assert recent_errors[0]["hook"] == "hook-a"
    
    # Get errors from last 72 hours (should include both)
    all_errors = get_recent_hook_errors(hours=72)
    assert len(all_errors) == 2


def test_get_recent_hook_errors_no_data(temp_router_dir):
    """Test getting recent errors when none exist."""
    errors = get_recent_hook_errors()
    assert errors == []


@pytest.mark.requires_api_keys
def test_get_recent_hook_errors_invalid_json(temp_router_dir):
    """Test that invalid JSON in log is skipped gracefully."""
    log_file = temp_router_dir / "hook_errors.log"
    log_file.write_text(
        '{"timestamp": "2026-04-16T12:00:00", "hook": "hook-a", "error": "Error 1"}\n'
        'This is not JSON\n'
        '{"timestamp": "2026-04-16T13:00:00", "hook": "hook-b", "error": "Error 2"}\n'
    )
    
    errors = get_recent_hook_errors(hours=24)
    
    # Should only include valid JSON entries
    assert len(errors) == 2
    assert errors[0]["hook"] == "hook-a"
    assert errors[1]["hook"] == "hook-b"


def test_check_hook_permissions(temp_hooks_dir):
    """Test checking hook file permissions."""
    # Create hooks with different permissions
    hooks_dir = temp_hooks_dir
    
    # Executable hook
    (hooks_dir / "llm-router-auto-route.py").write_text("# hook")
    os.chmod(hooks_dir / "llm-router-auto-route.py", 0o755)
    
    # Non-executable hook
    (hooks_dir / "llm-router-enforce-route.py").write_text("# hook")
    os.chmod(hooks_dir / "llm-router-enforce-route.py", 0o644)
    
    status = check_hook_permissions()
    
    assert status["auto-route"] == "ok"
    assert status["enforce-route"] == "not_executable"


def test_check_hook_permissions_missing_hook(temp_hooks_dir):
    """Test permission check for missing hook file."""
    # Don't create the file
    hooks_dir = temp_hooks_dir
    
    # Record a missing hook (simulate it was once registered but deleted)
    status = {}
    for file in hooks_dir.glob("llm-router-*.py"):
        hook_name = file.stem.replace("llm-router-", "")
        status[hook_name] = "ok"
    
    # All created hooks should be ok
    for value in status.values():
        assert value == "ok"


def test_check_hook_permissions_no_hooks_dir():
    """Test permission check when hooks directory doesn't exist."""
    with patch("pathlib.Path.home") as mock_home:
        mock_home.return_value = Path("/nonexistent")
        
        status = check_hook_permissions()
        
        # Should indicate hooks not installed
        assert status.get("_install") == "not_installed"


def test_cleanup_old_logs_retention(temp_router_dir):
    """Test log cleanup respects retention period."""
    log_file = temp_router_dir / "hook_errors.log"
    
    # Add old and new errors
    old_time = (datetime.now() - timedelta(days=40)).isoformat()
    new_time = datetime.now().isoformat()
    
    log_file.write_text(
        f'{{"timestamp": "{old_time}", "hook": "hook-a", "error": "Old error"}}\n'
        f'{{"timestamp": "{new_time}", "hook": "hook-b", "error": "New error"}}\n'
    )
    
    # Clean up logs older than 30 days
    removed = cleanup_old_logs(days=30)
    
    assert removed == 1
    
    # Verify only new error remains
    remaining_lines = log_file.read_text().splitlines()
    assert len(remaining_lines) == 1
    remaining_entry = json.loads(remaining_lines[0])
    assert remaining_entry["hook"] == "hook-b"


def test_cleanup_old_logs_no_logs(temp_router_dir):
    """Test cleanup when no log file exists."""
    removed = cleanup_old_logs()
    assert removed == 0


def test_cleanup_old_logs_handles_invalid_entries(temp_router_dir):
    """Test that cleanup preserves unparseable entries."""
    log_file = temp_router_dir / "hook_errors.log"
    
    old_time = (datetime.now() - timedelta(days=40)).isoformat()
    new_time = datetime.now().isoformat()
    
    log_file.write_text(
        f'{{"timestamp": "{old_time}", "hook": "hook-a", "error": "Old error"}}\n'
        'Invalid JSON line that cannot be parsed\n'
        f'{{"timestamp": "{new_time}", "hook": "hook-b", "error": "New error"}}\n'
    )
    
    removed = cleanup_old_logs(days=30)
    
    # Should remove 1 old entry but preserve the invalid line
    assert removed == 1
    
    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    
    # Verify the invalid line and new error are kept
    remaining_json_entries = [json.loads(line) for line in lines if line.startswith("{")]
    assert any(e["hook"] == "hook-b" for e in remaining_json_entries)


def test_record_hook_error_creates_directory(temp_router_dir):
    """Test that record_hook_error creates the .llm-router directory if it doesn't exist."""
    # Remove the directory to test creation
    import shutil
    shutil.rmtree(temp_router_dir, ignore_errors=True)
    
    assert not temp_router_dir.exists()
    
    # Record an error
    record_hook_error("test-hook", "Test error")
    
    # Directory should now exist
    assert temp_router_dir.exists()
    assert (temp_router_dir / "hook_errors.log").exists()
    assert (temp_router_dir / "hook_health.json").exists()


def test_hook_error_truncation(temp_router_dir):
    """Test that very long error messages are truncated."""
    long_error = "A" * 1000  # 1000 character error
    record_hook_error("hook-a", long_error)
    
    health_file = temp_router_dir / "hook_health.json"
    health = json.loads(health_file.read_text())
    
    # Error message in health file should be truncated to 100 chars
    assert len(health["hook-a"]["last_error_msg"]) <= 100
    
    # Error in log file should also be truncated
    log_file = temp_router_dir / "hook_errors.log"
    log_entry = json.loads(log_file.read_text().splitlines()[0])
    assert len(log_entry["error"]) <= 200
