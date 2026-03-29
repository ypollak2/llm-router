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

# Default Codex binary path (macOS desktop app)
CODEX_PATHS = [
    "/Applications/Codex.app/Contents/Resources/codex",
    os.path.expanduser("~/.local/bin/codex"),
]

# Models available through Codex (ordered by capability)
CODEX_MODELS = [
    "gpt-5.4",       # strongest reasoning
    "o3",             # deep reasoning
    "o4-mini",        # fast reasoning
    "gpt-4o",         # balanced
    "gpt-4o-mini",   # fast/cheap
]


def find_codex_binary() -> str | None:
    """Find the Codex CLI binary on the system."""
    for path in CODEX_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def is_codex_available() -> bool:
    """Check if Codex CLI is installed."""
    return find_codex_binary() is not None


@dataclass
class CodexResult:
    """Result from a Codex agent execution."""
    content: str
    model: str
    exit_code: int
    duration_sec: float

    @property
    def success(self) -> bool:
        return self.exit_code == 0


async def run_codex(
    prompt: str,
    model: str = "gpt-5.4",
    working_dir: str | None = None,
    timeout: int = 300,
) -> CodexResult:
    """Run a task through the Codex CLI agent.

    Uses create_subprocess_exec with explicit argument list — no shell
    interpolation, safe from injection.

    Args:
        prompt: The task/question to send to Codex.
        model: Which OpenAI model to use (default: gpt-5.4).
        working_dir: Working directory for the task (default: cwd).
        timeout: Max seconds to wait (default: 300).
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
