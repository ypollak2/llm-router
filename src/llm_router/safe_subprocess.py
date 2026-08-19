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
    # CHZ-SEC-03: the blocklist leaked AWS_ACCESS_KEY_ID, GH_PAT and DATABASE_URL
    # to child CLIs (none of them end in _API_KEY/_TOKEN or contain SECRET).
    # Broaden to the credential-bearing classes.
    r"AWS_.*",                 # ACCESS_KEY_ID, SESSION_TOKEN, etc. (child CLIs don't need AWS)
    r".*ACCESS_KEY.*",
    r".*_KEY_ID$",
    r".*CREDENTIAL.*",
    r".*_PAT$",                # GH_PAT and friends
    r"GH_PAT",
    r"GITHUB_TOKEN",
    r".*DATABASE_URL$",
    r".*REDIS_URL$",
    r".*_DSN$",
    r".*CONNECTION_STRING.*",
    r".*PRIVATE_KEY.*",
}


def _is_sensitive_var(var_name: str) -> bool:
    """Return True if this environment variable contains sensitive data."""
    import re
    for pattern in _SECRET_ENV_VARS:
        if re.match(pattern, var_name, re.IGNORECASE):
            return True
    return False


def get_safe_env() -> dict[str, str]:
    """Get a copy of os.environ with all *known* sensitive variables removed.

    This is a DENYLIST and inherits a denylist's guarantee, which is none. It
    removes what ``_SECRET_ENV_VARS`` happens to describe and passes everything
    else through; an unrecognised credential is forwarded to the child. The
    comment on ``AWS_.*`` above records this happening in production — three
    credential classes were leaking because none of them ended in ``_API_KEY``.

    Kept for callers that must preserve most of the parent environment. For
    anything running model-authored commands, use :func:`get_delegated_env`.
    """
    safe_env = {}
    for key, value in os.environ.items():
        if not _is_sensitive_var(key):
            safe_env[key] = value
    return safe_env


#: RED6-01 (P0): the only variables a delegated subprocess may inherit.
#:
#: An allowlist, not another denylist. The distinction is the whole fix. A
#: denylist must enumerate every secret that exists, including the ones invented
#: after it was written, and it fails OPEN — an unrecognised name is forwarded.
#: `FAKE_KEY` matches nothing in `_SECRET_ENV_VARS`, which is precisely why the
#: acceptance criterion uses it as the canary. An allowlist fails CLOSED: a
#: variable nobody thought about is simply not there.
#:
#: Entries here need a reason to exist. "A tool might want it" is not one — a
#: child that genuinely needs more should be passed it explicitly by its caller.
_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "PATH",          # without it the child cannot find /bin/sh, git, python
    "HOME",          # git, ssh config lookups, tool caches
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "PWD",
    "SYSTEMROOT",    # Windows: python/ssl break without it
    "COMSPEC",
    "PATHEXT",
})

#: Prefixes carried through wholesale. Deliberately short.
_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    # Python needs these to run at all inside a venv-managed child.
    "PYTHON",
    "VIRTUAL_ENV",
)


def get_delegated_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a subprocess running model-authored commands.

    Contains ONLY :data:`_ENV_ALLOWLIST` (plus ``extra``, which the caller is
    stating it needs on purpose). Every provider key, OAuth token and cloud
    credential in the parent process is absent — not because it was recognised
    and stripped, but because nothing is carried across unless it was named.

    ``extra`` is applied last and is not filtered: passing a secret through it is
    an explicit act by the caller, not an accident of inheritance.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWLIST or key.startswith(_ENV_ALLOWLIST_PREFIXES)
    }
    # A child with no PATH cannot exec anything; supply the platform default
    # rather than hand it an environment it cannot run in.
    if "PATH" not in env:
        env["PATH"] = os.defpath
    if extra:
        env.update(extra)
    return env


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
            stdin=asyncio.subprocess.DEVNULL,
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
