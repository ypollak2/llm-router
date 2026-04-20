"""Safe subprocess execution with API key environment isolation.

Provides wrappers around subprocess calls that filter out sensitive
environment variables (API keys, OAuth tokens) to prevent exposure
via /proc/[pid]/environ or similar mechanisms.

This is critical because LiteLLM requires API keys in os.environ,
but subprocess calls should not inherit them.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

# All environment variable patterns that contain secrets
_SECRET_ENV_VARS = {
    # API Keys (format: *_API_KEY, *_API_TOKEN)
    r".*_API_KEY$",
    r".*_API_TOKEN$",
    r".*_TOKEN$",
    # OAuth / Bearer tokens
    r".*_OAUTH_TOKEN$",
    r"ANTHROPIC_API_KEY",
    r"OPENAI_API_KEY",
    r"GEMINI_API_KEY",
    r"PERPLEXITYAI_API_KEY",
    r"MISTRAL_API_KEY",
    r"GROQ_API_KEY",
    r"TOGETHER_API_KEY",
    r"XAI_API_KEY",
    r"COHERE_API_KEY",
    r"DEEPSEEK_API_KEY",
    r"FAL_KEY",
    r"STABILITY_API_KEY",
    r"ELEVENLABS_API_KEY",
    r"RUNWAY_API_KEY",
    r"REPLICATE_API_TOKEN",
    r"HF_TOKEN",
    r"HUGGINGFACE_API_KEY",
    # Other secrets
    r"OLLAMA_API_BASE",
    r"HELICONE_API_KEY",
    r"CLAUDE.*TOKEN",
    r".*PASSWORD.*",
    r".*SECRET.*",
}


def _is_sensitive_var(var_name: str) -> bool:
    """Return True if this environment variable contains sensitive data."""
    import re
    for pattern in _SECRET_ENV_VARS:
        if re.match(pattern, var_name, re.IGNORECASE):
            return True
    return False


def get_safe_env() -> dict[str, str]:
    """Get a copy of os.environ with all sensitive variables removed.

    This is used when spawning subprocesses to prevent API keys
    and tokens from being visible via /proc/[pid]/environ.

    Returns:
        A filtered copy of os.environ with secrets removed.
    """
    safe_env = {}
    for key, value in os.environ.items():
        if not _is_sensitive_var(key):
            safe_env[key] = value
    return safe_env


async def safe_subprocess_exec(
    *args: str,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    cwd: str | None = None,
    timeout: int | None = None,
) -> tuple[bytes, bytes, int]:
    """Execute a subprocess safely with environment variable filtering.

    This is a safe wrapper around asyncio.create_subprocess_exec that:
    1. Removes all API keys and tokens from the environment
    2. Prevents subprocess from accessing parent's secrets
    3. Uses explicit argument list (no shell expansion)

    Args:
        *args: Command and arguments (passed to subprocess exec)
        stdout: Subprocess stdout handling (default: PIPE)
        stderr: Subprocess stderr handling (default: PIPE)
        cwd: Working directory for subprocess
        timeout: Maximum seconds to wait for completion

    Returns:
        Tuple of (stdout_bytes, stderr_bytes, exit_code)

    Raises:
        asyncio.TimeoutError: If subprocess exceeds timeout
        Exception: Any other subprocess error
    """
    safe_env = get_safe_env()

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=safe_env,
        )

        if timeout:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        else:
            stdout_data, stderr_data = await proc.communicate()

        return stdout_data, stderr_data, proc.returncode or 0
    except asyncio.TimeoutError:
        proc.kill()
        raise


def safe_subprocess_run(
    *args: str,
    cwd: str | None = None,
    timeout: int | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Execute a subprocess safely using subprocess.run.

    This is a safe wrapper around subprocess.run that:
    1. Removes all API keys and tokens from the environment
    2. Prevents subprocess from accessing parent's secrets
    3. Uses explicit argument list (no shell expansion)

    Args:
        *args: Command and arguments
        cwd: Working directory
        timeout: Maximum seconds to wait
        **kwargs: Additional arguments passed to subprocess.run

    Returns:
        CompletedProcess with result
    """
    safe_env = get_safe_env()

    return subprocess.run(
        args,
        cwd=cwd,
        timeout=timeout,
        env=safe_env,
        **kwargs,
    )
