"""Gemini CLI local agent integration — route tasks to Google's Gemini CLI.

Gemini CLI (Google's desktop agent) runs tasks non-interactively using the user's
Google One AI Pro subscription. This is free from Claude's quota — a perfect fallback
when Claude limits are tight.

Uses asyncio.create_subprocess_exec (not shell) for safe argument passing.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

GEMINI_PATHS = [
    os.path.expanduser("~/.local/bin/gemini"),
    "/usr/local/bin/gemini",
    "/opt/homebrew/bin/gemini",
    "/Applications/Gemini.app/Contents/MacOS/gemini",
]
"""Ordered list of filesystem paths to search for the Gemini CLI binary.

The first entries are conventional user-local and system paths used by
manual/Homebrew installs. ``find_gemini_binary()`` checks each in order
and returns the first that exists and is executable.
"""

GEMINI_MODELS = [
    "gemini-2.5-flash",    # fastest
    "gemini-2.0-flash",    # fast/balanced
    "gemini-3-flash-preview",  # preview
]
"""Available Google models when routing through Gemini CLI, ordered by speed.

All models run through the user's Google One AI Pro subscription (separate from
Claude quota), making Gemini a free-from-Claude fallback.
"""

# ── BLOCKING I/O MITIGATION ──────────────────────────────────────────────
# CRITICAL: is_gemini_cli_available() is called from _build_and_filter_chain(),
# which is async. Any synchronous filesystem I/O (os.path.isfile, os.access,
# Path.is_dir) blocks the entire asyncio event loop, causing routing hangs
# when the filesystem is slow (network mounts, USB drives) or unavailable.
#
# Solution: Cache results at module import time, before any async code runs.
# Pre-compute Gemini binary availability on module load.
# ─────────────────────────────────────────────────────────────────────────

_GEMINI_BINARY_PATH: str | None = None


def _initialize_gemini_cache() -> None:
    """Initialize Gemini availability cache at module import time.

    Called once during module initialization to populate module-level caches
    with synchronous filesystem checks. This ensures is_gemini_cli_available()
    never blocks the event loop during async routing.
    """
    global _GEMINI_BINARY_PATH

    # Compute Gemini binary path synchronously (once, at import time)
    env_path = os.environ.get("GEMINI_CLI_PATH")
    if env_path:
        full = os.path.expanduser(env_path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            _GEMINI_BINARY_PATH = full
            return

    for path in GEMINI_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            _GEMINI_BINARY_PATH = full
            return


def find_gemini_binary() -> str | None:
    """Search for an executable Gemini CLI binary.

    Search order:
      1. ``GEMINI_CLI_PATH`` env var — user-specified override for custom installs
      2. ``GEMINI_PATHS`` list — known standard binary paths

    Returns:
        The absolute path to the first matching binary, or ``None`` if
        no executable Gemini binary is found at any known location.
    """
    # Honour explicit override first — covers non-standard install paths,
    # Linux installs, or symlinks managed by a version manager.
    env_path = os.environ.get("GEMINI_CLI_PATH")
    if env_path:
        full = os.path.expanduser(env_path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full

    for path in GEMINI_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def is_gemini_cli_available() -> bool:
    """Check whether a usable Gemini CLI binary exists on this system.

    Returns the cached result from module import time (pre-computed to avoid
    blocking I/O in async contexts). Calling this during async routing is safe.

    Returns:
        ``True`` if a Gemini binary was found at module import time.
    """
    return _GEMINI_BINARY_PATH is not None


@dataclass
class GeminiCLIResult:
    """Result from a single Gemini CLI agent execution.

    Attributes:
        content: The stdout output from the Gemini process, or an error
            message if the process failed or timed out.
        model: The Google model that was requested (e.g. ``"gemini-2.5-flash"``).
        exit_code: Process exit code.  ``0`` = success, ``124`` = timeout,
            ``1`` = general error or binary-not-found.
        duration_sec: Wall-clock execution time in seconds.
    """
    content: str
    model: str
    exit_code: int
    duration_sec: float

    @property
    def success(self) -> bool:
        """Return ``True`` if the Gemini process exited successfully (code 0)."""
        return self.exit_code == 0


async def run_gemini_cli(
    prompt: str,
    model: str = "gemini-2.5-flash",
    working_dir: str | None = None,
    timeout: int | None = None,
) -> GeminiCLIResult:
    """Run a task through the Gemini CLI agent as a subprocess.

    Invokes ``gemini`` with an explicit argument list via
    ``asyncio.create_subprocess_exec`` — no shell expansion is involved,
    so the prompt string is safe from injection regardless of content.

    SECURITY: Subprocess runs with a filtered environment that excludes
    all API keys and tokens to prevent exposure via /proc/[pid]/environ.

    Error recovery strategy:
        - If the binary is not found, returns a ``GeminiCLIResult`` with
          ``exit_code=1`` and an installation hint (no exception raised).
        - If the process exceeds ``timeout``, it is killed and a result
          with ``exit_code=124`` (matching the Unix ``timeout`` convention)
          is returned.
        - Any other exception (e.g. permission denied) is caught and
          returned as a result with ``exit_code=1``.

    Args:
        prompt: The task or question to send to Gemini.
        model: Which Google model to use (default: ``"gemini-2.5-flash"``).
        working_dir: Working directory for the Gemini process.  Defaults
            to the current working directory.
        timeout: Maximum seconds to wait before killing the process.
            Defaults to ``LLM_ROUTER_GEMINI_TIMEOUT`` env var (300s).

    Returns:
        A ``GeminiCLIResult`` with the process output, model name, exit code,
        and wall-clock duration.  Never raises; all errors are captured
        in the result.
    """
    from llm_router.safe_subprocess import get_safe_env

    binary = find_gemini_binary()
    if not binary:
        return GeminiCLIResult(
            content="Gemini CLI not found. Install from https://google.com/gemini",
            model=model, exit_code=1, duration_sec=0.0,
        )

    cwd = working_dir or os.getcwd()

    # Use configurable timeout if not explicitly provided
    if timeout is None:
        # Check env var first
        env_timeout = os.environ.get("LLM_ROUTER_GEMINI_TIMEOUT")
        timeout = int(env_timeout) if env_timeout else 300

    # Standard Gemini CLI invocation: `gemini -p "<prompt>" --model <model>`
    # Note: actual CLI flags verified during testing — may need adjustment
    args = [binary, "-p", prompt, "--model", model]

    start = time.monotonic()
    try:
        # Use safe environment that excludes API keys and tokens
        safe_env = get_safe_env()

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=safe_env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = time.monotonic() - start

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output and stderr:
            output = stderr.decode("utf-8", errors="replace").strip()

        return GeminiCLIResult(
            content=output, model=model,
            exit_code=proc.returncode or 0, duration_sec=duration,
        )
    except asyncio.TimeoutError:
        return GeminiCLIResult(
            content=f"Gemini CLI timed out after {timeout}s",
            model=model, exit_code=124, duration_sec=float(timeout),
        )
    except Exception as e:
        return GeminiCLIResult(
            content=f"Gemini CLI error: {e}",
            model=model, exit_code=1, duration_sec=time.monotonic() - start,
        )


# Initialize Gemini cache at module import time (before any async routing code runs).
# This is critical to prevent blocking I/O from hanging the event loop.
_initialize_gemini_cache()
