"""Service manager for llm-router sidecar.

Handles start/stop, health checks, and graceful degradation.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("llm-router.service-manager")

ROUTER_DIR = Path.home() / ".llm-router"
SERVICE_PID_FILE = ROUTER_DIR / "service.pid"
SERVICE_PORT = int(os.environ.get("LLM_ROUTER_SERVICE_PORT", "7337"))


def _get_service_pid() -> int | None:
    """Read service PID from file."""
    try:
        if SERVICE_PID_FILE.exists():
            return int(SERVICE_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        pass
    return None


def _write_service_pid(pid: int) -> None:
    """Write service PID to file."""
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_PID_FILE.write_text(str(pid))


def _is_process_alive(pid: int) -> bool:
    """Check if process is still running."""
    try:
        os.kill(pid, 0)  # Signal 0 doesn't kill, just checks
        return True
    except (OSError, ProcessLookupError):
        return False


def start_service() -> bool:
    """Start the sidecar service if not already running.
    
    Returns True if service started or already running, False on error.
    """
    # Check if already running
    existing_pid = _get_service_pid()
    if existing_pid and _is_process_alive(existing_pid):
        logger.info(f"Service already running (PID {existing_pid})")
        return True
    
    # Clean up stale PID file
    if SERVICE_PID_FILE.exists():
        SERVICE_PID_FILE.unlink()
    
    try:
        # Import here to avoid circular dependency
        
        # Find the service script
        service_script = Path(__file__).parent / "service.py"
        
        if not service_script.exists():
            logger.error(f"Service script not found: {service_script}")
            return False
        
        # Start service in background
        # Use subprocess.Popen with shell=False for security
        proc = subprocess.Popen(
            [sys.executable, str(service_script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from parent
            env={**os.environ, "LLM_ROUTER_SERVICE_PORT": str(SERVICE_PORT)},
        )
        
        _write_service_pid(proc.pid)
        logger.info(f"Started service (PID {proc.pid})")
        
        # Wait for service to be ready
        if _wait_for_service(timeout=5):
            logger.info("Service is ready")
            return True
        else:
            logger.warning("Service started but not responding")
            return False
    
    except Exception as e:
        logger.error(f"Failed to start service: {e}", exc_info=True)
        return False


def stop_service() -> bool:
    """Stop the sidecar service gracefully."""
    pid = _get_service_pid()
    if not pid:
        return True
    
    if not _is_process_alive(pid):
        SERVICE_PID_FILE.unlink(missing_ok=True)
        return True
    
    try:
        # Send SIGTERM (graceful shutdown)
        os.kill(pid, signal.SIGTERM)
        
        # Wait for graceful shutdown
        for _ in range(50):  # 5 seconds
            if not _is_process_alive(pid):
                SERVICE_PID_FILE.unlink(missing_ok=True)
                logger.info(f"Service stopped gracefully (PID {pid})")
                return True
            time.sleep(0.1)
        
        # Force kill if needed
        os.kill(pid, signal.SIGKILL)
        SERVICE_PID_FILE.unlink(missing_ok=True)
        logger.info(f"Service force-killed (PID {pid})")
        return True
    
    except Exception as e:
        logger.error(f"Error stopping service: {e}")
        return False


def _wait_for_service(timeout: int = 5) -> bool:
    """Wait for service to be ready (health check)."""
    import urllib.request
    import urllib.error
    
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{SERVICE_PORT}/health",
                timeout=1,
            )
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    
    return False


def is_service_ready() -> bool:
    """Check if service is running and healthy."""
    import urllib.request
    import urllib.error
    
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{SERVICE_PORT}/health",
            timeout=1,
        )
        return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False
