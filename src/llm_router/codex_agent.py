"""Codex local agent integration — route tasks to OpenAI Codex desktop app.

Codex CLI (`codex exec`) runs tasks non-interactively using the user's
OpenAI subscription. This is free from Claude's quota — a perfect fallback
when Claude limits are tight.

Uses asyncio.create_subprocess_exec (not shell) for safe argument passing.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

CODEX_PATHS = [
    "/Applications/Codex.app/Contents/Resources/codex",
    os.path.expanduser("~/.local/bin/codex"),
    # npm global install locations (openai/codex-plugin-cc installs via npm)
    os.path.expanduser("~/.npm-global/bin/codex"),
    "/usr/local/bin/codex",
    "/opt/homebrew/bin/codex",
]
"""Ordered list of filesystem paths to search for the Codex CLI binary.

The first entry is the macOS desktop app bundle location; the second is
the conventional user-local binary path used by manual/Homebrew installs.
``find_codex_binary()`` checks each in order and returns the first that
exists and is executable.
"""

def _load_codex_models() -> list[str]:
    """Load the Codex model fallback chain, honouring an env-var override.

    Default returns the ChatGPT-subscription-supported set. As of Codex CLI
    v0.133, ChatGPT-account auth refuses ``o3``, ``o4-mini``, ``gpt-4o``,
    and ``gpt-4o-mini`` with HTTP 400 *"not supported when using Codex with
    a ChatGPT account"*. Only ``gpt-5.5`` (the current Codex default) and
    ``gpt-5.4`` are accepted on that tier, so they are the safe defaults.

    API-tier users (paid OpenAI billing via ``codex login --api-key``) can
    extend the list with ``LLM_ROUTER_CODEX_MODELS`` (comma-separated, e.g.
    ``"gpt-5.5,gpt-5.4,o3,gpt-4o"``). Empty / whitespace-only entries are
    dropped silently.
    """
    env = os.environ.get("LLM_ROUTER_CODEX_MODELS", "").strip()
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    return ["gpt-5.5", "gpt-5.4"]


CODEX_MODELS = _load_codex_models()
"""Codex model fallback chain in best-to-fast order.

See :func:`_load_codex_models` for the env-var override and the tier-specific
default rationale.
"""

# ── BLOCKING I/O MITIGATION ──────────────────────────────────────────────
# CRITICAL: is_codex_available() is called from _build_and_filter_chain(),
# which is async. Any synchronous filesystem I/O (os.path.isfile, os.access,
# Path.is_dir) blocks the entire asyncio event loop, causing routing hangs
# when the filesystem is slow (network mounts, USB drives) or unavailable.
#
# Solution: Cache results at module import time, before any async code runs.
# Pre-compute both Codex binary and plugin availability on module load.
#
# SELF-HEAL (added later): a startup-only probe permanently misses Codex
# binaries installed *after* the MCP daemon launched — a real failure mode
# we hit in production. The cache is now positive-only-trusted: a False
# result triggers a re-probe at most once per _PROBE_INTERVAL_SEC. Worst
# case is one delayed call per minute on a slow filesystem; the upside is
# the cache self-heals without requiring an MCP restart whenever Codex,
# Codex.app, or the npm plugin land on disk.
# ─────────────────────────────────────────────────────────────────────────

_CODEX_BINARY_PATH: str | None = None
_CODEX_PLUGIN_AVAILABLE: bool = False
_PROBE_INTERVAL_SEC: float = 60.0
_LAST_PROBE_TS: float = 0.0


def _initialize_codex_cache() -> None:
    """Initialize Codex availability cache at module import time.

    Called once during module initialization to populate module-level caches
    with synchronous filesystem checks. This ensures is_codex_available() and
    is_codex_plugin_available() never block the event loop during async routing.
    """
    global _CODEX_BINARY_PATH, _CODEX_PLUGIN_AVAILABLE

    # Compute Codex binary path synchronously (once, at import time)
    env_path = os.environ.get("CODEX_PATH")
    if env_path:
        full = os.path.expanduser(env_path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            _CODEX_BINARY_PATH = full
            _CODEX_PLUGIN_AVAILABLE = _check_codex_plugin()
            return

    for path in CODEX_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            _CODEX_BINARY_PATH = full
            break

    _CODEX_PLUGIN_AVAILABLE = _check_codex_plugin()


def _check_codex_plugin() -> bool:
    """Synchronously check for Codex plugin (called only at module init time)."""
    candidates = [
        Path.home() / ".claude" / "plugins" / "codex",
        Path.cwd() / ".claude" / "plugins" / "codex",
    ]
    try:
        return any(d.is_dir() for d in candidates)
    except Exception:
        return False


def find_codex_binary() -> str | None:
    """Search for an executable Codex CLI binary.

    Search order:
      1. ``CODEX_PATH`` env var — user-specified override for custom installs
      2. ``CODEX_PATHS`` list — known macOS app bundle and standard binary paths

    Returns:
        The absolute path to the first matching binary, or ``None`` if
        no executable Codex binary is found at any known location.
    """
    # Honour explicit override first — covers non-standard install paths,
    # Linux installs, or symlinks managed by a version manager.
    env_path = os.environ.get("CODEX_PATH")
    if env_path:
        full = os.path.expanduser(env_path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full

    for path in CODEX_PATHS:
        full = os.path.expanduser(path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full
    return None


def is_codex_available() -> bool:
    """Check whether a usable Codex CLI binary exists on this system.

    Cache strategy:

    * **Positive cache is trusted.** If a binary was found previously the
      function returns ``True`` immediately — binaries virtually never
      vanish at runtime and trusting the cache keeps the hot path
      allocation-free.
    * **Negative cache re-probes.** A ``None`` cache triggers a fresh
      filesystem probe at most once per ``_PROBE_INTERVAL_SEC``. This
      self-heals the failure mode where the MCP daemon launched before
      Codex was installed (cache locked to ``False`` for the daemon's
      lifetime, no MCP restart possible from a user perspective).

    The 60-second floor keeps the worst case bounded: at most one
    filesystem hit per minute on a slow / network-mounted home, while
    a normal local install heals on the next routing decision.
    Calling this from async code remains safe under those assumptions.

    Returns:
        ``True`` if a Codex binary is currently findable on disk.
    """
    global _CODEX_BINARY_PATH, _CODEX_PLUGIN_AVAILABLE, _LAST_PROBE_TS

    # Fast path: positive cache.
    if _CODEX_BINARY_PATH is not None:
        return True

    # Negative cache → rate-limited re-probe.
    now = time.monotonic()
    if now - _LAST_PROBE_TS < _PROBE_INTERVAL_SEC:
        return False
    _LAST_PROBE_TS = now

    _CODEX_BINARY_PATH = find_codex_binary()
    if _CODEX_BINARY_PATH is not None:
        # Binary just appeared — plugin often lands alongside it; re-check
        # too so the next is_codex_plugin_available() call reflects reality.
        try:
            _CODEX_PLUGIN_AVAILABLE = _check_codex_plugin()
        except Exception:
            # Plugin probe failure is non-fatal; the binary alone is enough
            # to route through Codex.
            pass
    return _CODEX_BINARY_PATH is not None


def _reset_codex_cache_for_tests() -> None:
    """Drop the cached binary path and last-probe timestamp so the next
    ``is_codex_available()`` call performs a fresh probe.

    Production code never calls this — tests use it to exercise the
    self-heal path deterministically without ``time.sleep(60)``.
    """
    global _CODEX_BINARY_PATH, _CODEX_PLUGIN_AVAILABLE, _LAST_PROBE_TS
    _CODEX_BINARY_PATH = None
    _CODEX_PLUGIN_AVAILABLE = False
    _LAST_PROBE_TS = 0.0


def is_codex_plugin_available() -> bool:
    """Check whether the openai/codex-plugin-cc Claude Code plugin is installed.

    The plugin provides slash commands (/codex:review, /codex:rescue, etc.)
    and background job management on top of the Codex CLI binary.

    Returns the cached result from module import time (pre-computed to avoid
    blocking I/O in async contexts). Calling this during async routing is safe.

    Returns:
        ``True`` if the plugin directory was found at module import time.
    """
    return _CODEX_PLUGIN_AVAILABLE


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
    model: str = "gpt-5.5",
    working_dir: str | None = None,
    timeout: int | None = None,
    on_event: "Callable[[str, str], Awaitable[None]] | None" = None,
) -> CodexResult:
    """Run a task through the Codex CLI agent as a subprocess.

    Invokes ``codex exec`` with an explicit argument list via
    ``asyncio.create_subprocess_exec`` — no shell expansion is involved,
    so the prompt string is safe from injection regardless of content.
    
    SECURITY: Subprocess runs with a filtered environment that excludes
    all API keys and tokens to prevent exposure via /proc/[pid]/environ.

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
        model: Which OpenAI model to use (default: ``"gpt-5.5"`` — the current
            Codex CLI default; both ``gpt-5.5`` and ``gpt-5.4`` work on
            ChatGPT-account auth, see :func:`_load_codex_models`).
        working_dir: Working directory for the Codex process.  Defaults
            to the current working directory.
        timeout: Maximum seconds to wait before killing the process.
            Defaults to ``LLM_ROUTER_CODEX_TIMEOUT`` env var (300s).

    Returns:
        A ``CodexResult`` with the process output, model name, exit code,
        and wall-clock duration.  Never raises; all errors are captured
        in the result.
    """
    from llm_router.safe_subprocess import get_safe_env
    
    binary = find_codex_binary()
    if not binary:
        return CodexResult(
            content="Codex CLI not found. Install from https://openai.com/codex",
            model=model, exit_code=1, duration_sec=0.0,
        )

    cwd = working_dir or os.getcwd()

    # Use configurable timeout if not explicitly provided
    if timeout is None:
        from llm_router.timeout_config import codex_timeout
        timeout = codex_timeout()

    # All arguments passed as separate list items — no shell expansion.
    # --skip-git-repo-check: llm_router is a pure-LLM consumer, not a code-edit
    # client; without this flag Codex CLI v0.133+ refuses to run when ``cwd``
    # is outside a trusted git repo ("Not inside a trusted directory and
    # --skip-git-repo-check was not specified") and exits non-zero — which
    # the router then logs as "Codex exited 1" and skips Codex for the
    # entire chain. Always-on is safe because we never ask Codex to mutate
    # the working tree.
    # --json: emit JSONL events line-by-line for streaming progress visibility
    #
    # -c model_provider=openai: LLM Router's own installer (commands/install.py,
    # _install_codex_gateway_config) sets `model_provider = "llm_router"` as the
    # GLOBAL default in ~/.codex/config.toml, looping Codex's own calls back
    # through LLM Router's local gateway (127.0.0.1:17900) for its own routing
    # benefits. That gateway doesn't yet speak the exact OpenAI "responses"
    # wire shape Codex's client expects, so every call through it fails with
    # an undecodable-stream error. This dispatch must always reach the real
    # ChatGPT-authenticated backend regardless of that global default, so the
    # provider is pinned back to the built-in "openai" provider here — scoped
    # to this one subprocess call, no edit to the user's global config.
    args = [
        binary, "exec",
        "--json",
        "-m", model,
        "-c", "model_provider=openai",
        "--color", "never",
        "--skip-git-repo-check",
        "-C", cwd,
        prompt,
    ]

    start = time.monotonic()
    try:
        # Use safe environment that excludes API keys and tokens
        safe_env = get_safe_env()

        # Codex CLI validates every configured model_provider at startup —
        # including ones this call never uses — and hard-fails the whole turn
        # if any declared `env_key` is unset. A stray [model_providers.llm_router]
        # block (base_url http://127.0.0.1:17900/v1, env_key LLM_ROUTER_API_KEY)
        # in ~/.codex/config.toml trips this even though we always target the
        # ChatGPT-authenticated OpenAI provider (-m gpt-5.x), never "llm_router".
        # Only the variable's presence is checked, not its value, so a
        # placeholder here — scoped to this subprocess only — satisfies Codex
        # without touching the user's global CLI config.
        safe_env.setdefault("LLM_ROUTER_API_KEY", "unused-placeholder")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=safe_env,
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
                return CodexResult(
                    content=f"Codex timed out after {timeout}s",
                    model=model, exit_code=124,
                    duration_sec=time.monotonic() - start,
                )
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                text_chunks.append(line)
                continue

            ev_type = ev.get("type", "")
            if ev_type == "item.completed":
                text = ev.get("item", {}).get("text", "")
                if text:
                    text_chunks.append(text)
                    if on_event:
                        try:
                            await on_event("item.completed", text[:120])
                        except Exception:
                            pass
            elif ev_type == "turn.completed":
                if on_event:
                    usage = ev.get("usage", {})
                    try:
                        await on_event(
                            "turn.completed",
                            f"done — {usage.get('output_tokens','?')} tokens",
                        )
                    except Exception:
                        pass
            elif ev_type in ("turn.started", "thread.started"):
                if on_event:
                    try:
                        await on_event(ev_type, "")
                    except Exception:
                        pass

        await proc.wait()
        await stderr_task
        duration = time.monotonic() - start

        output = "\n".join(text_chunks).strip()
        if not output and stderr_buf:
            output = b"".join(stderr_buf).decode("utf-8", errors="replace").strip()

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


# Initialize Codex cache at module import time (before any async routing code runs).
# This is critical to prevent blocking I/O from hanging the event loop.
_initialize_codex_cache()
