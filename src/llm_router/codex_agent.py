"""Codex local agent integration — route tasks to OpenAI Codex desktop app.

Codex CLI (`codex exec`) runs tasks non-interactively using the user's
OpenAI subscription. This is free from Claude's quota — a perfect fallback
when Claude limits are tight.

Uses asyncio.create_subprocess_exec (not shell) for safe argument passing.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

CODEX_PATHS = [
    "/Applications/Codex.app/Contents/Resources/codex",
    os.path.expanduser("~/.local/bin/codex"),
]
"""Ordered list of filesystem paths to search for the Codex CLI binary.

The first entry is the macOS desktop app bundle location; the second is
the conventional user-local binary path used by manual/Homebrew installs.
``find_codex_binary()`` checks each in order and returns the first that
exists and is executable.
"""

CODEX_MODELS = [
    "gpt-5.4",       # strongest reasoning
    "o3",             # deep reasoning
    "o4-mini",        # fast reasoning
    "gpt-4o",         # balanced
    "gpt-4o-mini",   # fast/cheap
]
"""Available OpenAI models when routing through Codex, ordered by capability.

All models run through the user's OpenAI subscription (separate from
Claude quota), making Codex a free-from-Claude fallback.
"""


def find_codex_binary() -> str | None:
    """Search ``CODEX_PATHS`` for an executable Codex CLI binary.

    Returns:
        The absolute path to the first matching binary, or ``None`` if
        no executable Codex binary is found at any known location.
    """
    for path in CODEX_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def is_codex_available() -> bool:
    """Check whether a usable Codex CLI binary exists on this system.

    Returns:
        ``True`` if ``find_codex_binary()`` finds an executable binary.
    """
    return find_codex_binary() is not None


@dataclass
class CodexResult:
    """Result from a single Codex CLI agent execution.

    Attributes:
        content: The stdout output from the Codex process, or an error
            message if the process failed or timed out.
        model: The OpenAI model that was requested (e.g. ``"gpt-5.4"``).
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
        """Return ``True`` if the Codex process exited successfully (code 0)."""
        return self.exit_code == 0


async def run_codex(
    prompt: str,
    model: str = "gpt-5.4",
    working_dir: str | None = None,
    timeout: int = 300,
) -> CodexResult:
    """Run a task through the Codex CLI agent as a subprocess.

    Invokes ``codex exec`` with an explicit argument list via
    ``asyncio.create_subprocess_exec`` — no shell expansion is involved,
    so the prompt string is safe from injection regardless of content.

    Error recovery strategy:
        - If the binary is not found, returns a ``CodexResult`` with
          ``exit_code=1`` and an installation hint (no exception raised).
        - If the process exceeds ``timeout``, it is killed and a result
          with ``exit_code=124`` (matching the Unix ``timeout`` convention)
          is returned.
        - Any other exception (e.g. permission denied) is caught and
          returned as a result with ``exit_code=1``.

    Args:
        prompt: The task or question to send to Codex.
        model: Which OpenAI model to use (default: ``"gpt-5.4"``).
        working_dir: Working directory for the Codex process.  Defaults
            to the current working directory.
        timeout: Maximum seconds to wait before killing the process
            (default: 300).

    Returns:
        A ``CodexResult`` with the process output, model name, exit code,
        and wall-clock duration.  Never raises; all errors are captured
        in the result.
    """
    binary = find_codex_binary()
    if not binary:
        return CodexResult(
            content="Codex CLI not found. Install from https://openai.com/codex",
            model=model, exit_code=1, duration_sec=0.0,
        )

    cwd = working_dir or os.getcwd()

    # All arguments passed as separate list items — no shell expansion
    args = [binary, "exec", "-m", model, "--color", "never", "-C", cwd, prompt]

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = time.monotonic() - start

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output and stderr:
            output = stderr.decode("utf-8", errors="replace").strip()

        return CodexResult(
            content=output, model=model,
            exit_code=proc.returncode or 0, duration_sec=duration,
        )
    except asyncio.TimeoutError:
        return CodexResult(
            content=f"Codex timed out after {timeout}s",
            model=model, exit_code=124, duration_sec=float(timeout),
        )
    except Exception as e:
        return CodexResult(
            content=f"Codex error: {e}",
            model=model, exit_code=1, duration_sec=time.monotonic() - start,
        )
