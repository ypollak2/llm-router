"""Hook health monitoring and error visibility.

Tracks hook execution health, logs errors, and provides diagnostic endpoints
for users to understand when and why hooks fail.

Health checks:
- File permissions (hooks must be executable)
- Hook execution success/failure
- Error rate tracking
- Last execution time
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

_ROUTER_DIR = Path.home() / ".llm-router"
_HOOK_HEALTH_FILE = _ROUTER_DIR / "hook_health.json"
_HOOK_LOG_FILE = _ROUTER_DIR / "hook_errors.log"


def record_hook_error(hook_name: str, error: str, context: dict | None = None) -> None:
    """Log a hook error with context for debugging.

    Args:
        hook_name: Name of the hook that failed (e.g., 'auto-route', 'enforce-route')
        error: Error message or exception string
        context: Optional dict with request/session context for debugging
    """
    try:
        _ROUTER_DIR.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().isoformat()
        entry = {
            "timestamp": ts,
            "hook": hook_name,
            "error": str(error)[:200],  # Truncate very long errors
        }
        if context:
            entry["context"] = {k: v for k, v in context.items() if k in
                              ("session_id", "tool_name", "task_type", "complexity")}

        # Append to log file
        with _HOOK_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        # Update health file
        _update_hook_health(hook_name, success=False, error=str(error)[:100])
    except OSError:
        # If we can't write health info, at least try stderr
        import sys
        print(f"[llm-router] Hook {hook_name} error (couldn't log): {error}", file=sys.stderr)


def record_hook_success(hook_name: str) -> None:
    """Record successful hook execution."""
    try:
        _update_hook_health(hook_name, success=True)
    except OSError:
        pass  # Health tracking is optional


def _update_hook_health(hook_name: str, success: bool, error: str = "") -> None:
    """Update hook health tracking file."""
    data = {}

    # Read existing health data
    if _HOOK_HEALTH_FILE.exists():
        try:
            data = json.loads(_HOOK_HEALTH_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}

    # Update or create hook entry
    if hook_name not in data:
        data[hook_name] = {
            "success_count": 0,
            "error_count": 0,
            "last_success": None,
            "last_error": None,
            "last_error_msg": "",
        }

    entry = data[hook_name]
    now = datetime.now().isoformat()

    if success:
        entry["success_count"] = entry.get("success_count", 0) + 1
        entry["last_success"] = now
    else:
        entry["error_count"] = entry.get("error_count", 0) + 1
        entry["last_error"] = now
        entry["last_error_msg"] = error

    # Write updated health file
    _HOOK_HEALTH_FILE.write_text(json.dumps(data, indent=2))


def get_hook_health() -> dict:
    """Get health status for all hooks.

    Returns:
        dict with hook names as keys, health info as values including:
        - success_count: number of successful executions
        - error_count: number of failed executions
        - last_success: ISO timestamp of last successful execution
        - last_error: ISO timestamp of last failed execution
        - last_error_msg: error message from last failure
        - health_status: 'healthy' (no recent errors), 'degraded' (some errors), or 'failing' (recent errors)
    """
    if not _HOOK_HEALTH_FILE.exists():
        return {}

    try:
        data = json.loads(_HOOK_HEALTH_FILE.read_text())

        # Add health status for each hook
        for hook_name, info in data.items():
            error_count = info.get("error_count", 0)
            last_error = info.get("last_error")

            # Determine health status
            if error_count == 0:
                status = "healthy"
            elif last_error:
                # Check if last error was recent (within 1 hour)
                try:
                    last_error_time = datetime.fromisoformat(last_error)
                    if datetime.now() - last_error_time < timedelta(hours=1):
                        status = "failing"
                    else:
                        status = "degraded"
                except (ValueError, TypeError):
                    status = "degraded"
            else:
                status = "degraded"

            info["health_status"] = status

        return data
    except (json.JSONDecodeError, OSError):
        return {}


def get_recent_hook_errors(hours: int = 24) -> list[dict]:
    """Get recent hook errors from the error log.

    Args:
        hours: Only return errors from the last N hours

    Returns:
        List of error entries with timestamp, hook name, and error message
    """
    if not _HOOK_LOG_FILE.exists():
        return []

    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        errors = []

        for line in _HOOK_LOG_FILE.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                entry_time = datetime.fromisoformat(entry.get("timestamp", ""))
                if entry_time > cutoff:
                    errors.append(entry)
            except (json.JSONDecodeError, ValueError):
                continue

        return errors
    except (OSError, ValueError):
        return []


def check_hook_permissions() -> dict[str, str]:
    """Check if all installed hook files have proper execute permissions.

    Checks ~/.claude/hooks/llm-router-*.py files, which are the actual hooks
    that Claude Code runs. Returns status for each hook.

    Returns:
        dict with hook names and permission status ('ok', 'missing', 'not_executable')
    """
    hooks_dir = Path.home() / ".claude" / "hooks"
    status = {}

    # Check for llm-router-*.py hooks
    if hooks_dir.exists():
        for hook_file in hooks_dir.glob("llm-router-*.py"):
            # Extract hook name from llm-router-NAME.py → NAME
            hook_name = hook_file.stem.replace("llm-router-", "")

            if not hook_file.exists():
                status[hook_name] = "missing"
            elif not os.access(hook_file, os.X_OK):
                status[hook_name] = "not_executable"
            else:
                status[hook_name] = "ok"
    else:
        status["_install"] = "not_installed"

    return status


def cleanup_old_logs(days: int = 30) -> int:
    """Remove old hook error log entries beyond the retention period.

    Args:
        days: Keep logs from the last N days

    Returns:
        Number of old entries removed
    """
    if not _HOOK_LOG_FILE.exists():
        return 0

    try:
        cutoff = datetime.now() - timedelta(days=days)
        lines = _HOOK_LOG_FILE.read_text().splitlines()

        kept_lines = []
        removed_count = 0

        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                entry_time = datetime.fromisoformat(entry.get("timestamp", ""))
                if entry_time > cutoff:
                    kept_lines.append(line)
                else:
                    removed_count += 1
            except (json.JSONDecodeError, ValueError):
                kept_lines.append(line)  # Keep lines we can't parse

        if removed_count > 0:
            _HOOK_LOG_FILE.write_text("\n".join(kept_lines) + "\n" if kept_lines else "")

        return removed_count
    except OSError:
        return 0


def get_deadlock_report() -> dict | None:
    """Get hook deadlock detection report.

    Runs the deadlock detector on all hooks and returns a structured report
    of circular dependencies, timeout issues, and resource contention.

    Returns:
        dict with keys:
        - has_cycles: bool — has circular dependencies
        - has_timeout_issues: bool — has subprocess calls without timeout
        - has_resource_contention: bool — has resource contention
        - cycles: list — circular dependency chains
        - timeout_issues: list — hooks with timeout problems
        - contention_patterns: list — (hook1, hook2) pairs with contention
        - critical_path_length: int — longest dependency chain

        Returns None if deadlock detector is not available.
    """
    try:
        from llm_router.hook_deadlock_detector import HookDeadlockDetector

        detector = HookDeadlockDetector()
        report = detector.analyze()

        return {
            "has_cycles": report.has_cycles,
            "has_timeout_issues": report.has_timeout_issues,
            "has_resource_contention": report.has_resource_contention,
            "cycles": report.cycles,
            "timeout_issues": report.timeout_issues,
            "contention_patterns": report.contention_patterns,
            "critical_path_length": report.critical_path_length,
        }
    except ImportError:
        return None
