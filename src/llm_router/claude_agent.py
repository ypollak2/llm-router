"""Claude Code local agent — route offload tasks to the Claude Code CLI.

The `claude` CLI runs one-shot prompts non-interactively using the user's Claude
Code subscription (subscription auth, no API key). This lets LLM Router's offload tools
spend Claude on the hardest tasks without a separate Anthropic API key/billing.

Uses asyncio.create_subprocess_exec (not shell) for safe argument passing. The
router gates this provider on real subscription pressure so it never starves the
user's primary Claude Code work.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

CLAUDE_PATHS = [
    os.path.expanduser("~/.local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.npm-global/bin/claude"),
]
"""Ordered filesystem locations to search for the Claude Code CLI binary."""

CLAUDE_MODELS = ["opus", "sonnet", "haiku"]
"""Claude CLI model aliases, best-to-fast. The router maps its per-tier
``anthropic/*`` selection to one of these via :func:`_model_alias`."""

# ── Blocking-I/O mitigation (mirrors codex_agent) ────────────────────────────
# is_claude_available() is called from async routing; sync filesystem I/O would
# block the event loop. Cache at import time; positive cache is trusted, a
# negative cache re-probes at most once per _PROBE_INTERVAL_SEC so it self-heals
# if the CLI is installed after the daemon started.
_CLAUDE_BINARY_PATH: str | None = None
_PROBE_INTERVAL_SEC: float = 60.0
_LAST_PROBE_TS: float = 0.0


def _initialize_claude_cache() -> None:
    """Populate the binary-path cache synchronously at module import."""
    global _CLAUDE_BINARY_PATH
    env_path = os.environ.get("CLAUDE_CODE_PATH")
    if env_path:
        full = os.path.expanduser(env_path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            _CLAUDE_BINARY_PATH = full
            return
    for path in CLAUDE_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            _CLAUDE_BINARY_PATH = full
            break


def find_claude_binary() -> str | None:
    """Return the path to an executable `claude` CLI, honouring ``CLAUDE_CODE_PATH``."""
    env_path = os.environ.get("CLAUDE_CODE_PATH")
    if env_path:
        full = os.path.expanduser(env_path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    for path in CLAUDE_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def is_claude_available() -> bool:
    """True if a `claude` CLI binary is findable (positive-cache + 60s re-probe)."""
    global _CLAUDE_BINARY_PATH, _LAST_PROBE_TS
    if _CLAUDE_BINARY_PATH is not None:
        return True
    now = time.monotonic()
    if now - _LAST_PROBE_TS < _PROBE_INTERVAL_SEC:
        return False
    _LAST_PROBE_TS = now
    _CLAUDE_BINARY_PATH = find_claude_binary()
    return _CLAUDE_BINARY_PATH is not None


def _reset_claude_cache_for_tests() -> None:
    """Drop caches so the next is_claude_available() re-probes (tests only)."""
    global _CLAUDE_BINARY_PATH, _LAST_PROBE_TS
    _CLAUDE_BINARY_PATH = None
    _LAST_PROBE_TS = 0.0


def offload_available(config: object) -> bool:
    """Whether Claude may be used for OFFLOAD right now.

    True only when subscription mode is on, the `claude` CLI is installed (so
    anthropic/* can actually EXECUTE rather than dead-end to local), and the combined
    5h+weekly Claude pressure is below ``llm_router_claude_offload_max_pressure`` — so
    offload never starves the user's primary Claude Code work.
    """
    if not getattr(config, "llm_router_claude_subscription", False):
        return False
    if not is_claude_available():
        return False
    from llm_router.claude_usage import get_claude_pressure
    cap = getattr(config, "llm_router_claude_offload_max_pressure", 0.80)
    return get_claude_pressure() < cap


def _model_alias(model: str) -> str:
    """Map a router model spec (e.g. ``anthropic/claude-opus-4-8``) to a CLI alias."""
    normalized = model.removeprefix("anthropic/").lower()
    if "opus" in normalized:
        return "opus"
    if "haiku" in normalized:
        return "haiku"
    return "sonnet"


@dataclass
class ClaudeResult:
    """Result of one Claude Code CLI execution.

    exit_code: 0 = success, 124 = timeout, 1 = error / binary-not-found.
    """
    content: str
    model: str
    exit_code: int
    duration_sec: float

    @property
    def success(self) -> bool:
        return self.exit_code == 0


async def run_claude(
    prompt: str,
    model: str = "sonnet",
    working_dir: str | None = None,
    timeout: int | None = None,
    on_event: "Callable[[str, str], Awaitable[None]] | None" = None,
) -> ClaudeResult:
    """Run a one-shot prompt through the `claude` CLI as a subprocess.

    Invokes ``claude -p <prompt> --output-format text --model <alias>`` via
    ``asyncio.create_subprocess_exec`` (no shell — prompt is injection-safe). The
    environment is scrubbed of API keys/tokens; the CLI uses the subscription's
    own credentials. Never raises — all errors are captured in the result.
    """
    from llm_router.safe_subprocess import get_safe_env

    binary = find_claude_binary()
    if not binary:
        return ClaudeResult(
            content="Claude Code CLI not found. Install from https://claude.com/download",
            model=model, exit_code=1, duration_sec=0.0,
        )

    cwd = working_dir or os.getcwd()
    if timeout is None:
        timeout = int(os.environ.get("LLM_ROUTER_CLAUDE_TIMEOUT", "300"))

    args = [binary, "-p", prompt, "--output-format", "text", "--model", _model_alias(model)]

    start = time.monotonic()
    try:
        env = get_safe_env()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        text_chunks: list[str] = []
        stderr_buf: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            async for line in proc.stderr:
                stderr_buf.append(line)

        stderr_task = asyncio.create_task(_drain_stderr())

        assert proc.stdout is not None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        async for raw in proc.stdout:
            if loop.time() > deadline:
                proc.kill()
                return ClaudeResult(
                    content=f"Claude Code timed out after {timeout}s",
                    model=model, exit_code=124,
                    duration_sec=time.monotonic() - start,
                )
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                text_chunks.append(line)
                if on_event is not None:
                    try:
                        await on_event("line", line[:120])
                    except Exception:
                        pass

        await proc.wait()
        await stderr_task
        duration = time.monotonic() - start

        output = "\n".join(text_chunks).strip()
        if not output and stderr_buf:
            output = b"".join(stderr_buf).decode("utf-8", errors="replace").strip()

        return ClaudeResult(
            content=output, model=model,
            exit_code=proc.returncode or 0, duration_sec=duration,
        )
    except asyncio.TimeoutError:
        return ClaudeResult(
            content=f"Claude Code timed out after {timeout}s",
            model=model, exit_code=124, duration_sec=float(timeout),
        )
    except Exception as e:
        return ClaudeResult(
            content=f"Claude Code error: {e}",
            model=model, exit_code=1, duration_sec=time.monotonic() - start,
        )


# Populate the availability cache at import (before any async routing runs).
_initialize_claude_cache()
