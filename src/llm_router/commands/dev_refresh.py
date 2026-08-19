"""``llm_router dev-refresh`` — full development refresh in one command.

After editing llm_router source, three layers need to be updated before
the changes take effect end-to-end:

1. **The installed package** at ``~/.local/share/uv/tools/llm-routing/``
   — refreshed by ``uv tool install --reinstall <source>``.
2. **The hook scripts** at ``~/.claude/hooks/llm_router-*`` —
   refreshed by ``llm_router-install-hooks`` (which copies from the
   installed package, not from source — so step 1 must come first).
3. **Running llm_router MCP server processes** — long-lived per Claude
   Code session, they load the package once into memory. New code on
   disk doesn't update them. They must be killed so the next session
   spawns a fresh server with the new code.

Skipping any of the three layers leaves a partially-refreshed runtime,
which historically produced "I reinstalled but my change isn't live"
confusion (most recently while debugging the llm_router welcome banner in
PR #4, where the installer copies from the package but the package
itself wasn't refreshed yet).

Flags:
    --source PATH     Source directory containing pyproject.toml.
                      Defaults to ``$LLM_ROUTER_DEV_SRC`` if set, else the
                      package's editable-install location if detected.
    --skip-mcp-kill   Skip step 3 (don't kill running MCP servers).
                      Useful if your current Claude Code session needs
                      to keep working until you're ready to restart it.
    --dry-run         Print what would happen without doing it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


_MCP_SERVER_PATTERN = "llm-routing/bin/python3 .*llm_router$"


def _find_source(args: list[str]) -> Path | None:
    if "--source" in args:
        idx = args.index("--source")
        if idx + 1 < len(args):
            return Path(args[idx + 1]).expanduser().resolve()
    env_src = os.environ.get("LLM_ROUTER_DEV_SRC")
    if env_src:
        return Path(env_src).expanduser().resolve()
    # Best-effort fallback: if llm_router was installed editably, the source
    # tree contains pyproject.toml at the package's grandparent dir.
    try:
        import llm_router
    except ImportError:
        return None
    here = Path(llm_router.__file__).resolve().parent.parent.parent
    return here if (here / "pyproject.toml").exists() else None


def _list_mcp_servers(self_pid: int) -> list[int]:
    """Return PIDs of running llm_router MCP servers (excluding self).

    Matches processes whose argv ends with the llm_router entrypoint and
    nothing else — i.e., the long-lived MCP-server invocation, not
    other ``llm_router <subcommand>`` calls.
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", _MCP_SERVER_PATTERN], text=True
        )
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in out.splitlines():
        try:
            pid = int(line.split(maxsplit=1)[0])
        except (ValueError, IndexError):
            continue
        if pid == self_pid or pid == os.getppid():
            continue
        pids.append(pid)
    return pids


def cmd_dev_refresh(args: list[str]) -> int:
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    dry_run = "--dry-run" in args
    skip_mcp = "--skip-mcp-kill" in args

    source = _find_source(args)
    if source is None or not (source / "pyproject.toml").exists():
        print(
            "✗ Could not locate llm_router source directory. Pass --source PATH "
            "or set LLM_ROUTER_DEV_SRC=<path-to-source>.",
            file=sys.stderr,
        )
        return 1

    print(f"📂 source: {source}")
    if dry_run:
        print("   (dry run — no commands will execute)")

    # ── Step 1: reinstall package ───────────────────────────────────────
    print("⚙️  step 1/3: uv tool install --reinstall ...")
    if not dry_run:
        result = subprocess.run(
            ["uv", "tool", "install", "--reinstall", str(source)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"✗ uv tool install failed:\n{result.stderr}", file=sys.stderr)
            return 1
        # Surface the "Installed N executables" line (uv's summary).
        for line in reversed(result.stdout.splitlines()):
            if line.strip().startswith("Installed"):
                print(f"   {line.strip()}")
                break

    # ── Step 2: sync hooks to ~/.claude/hooks/ ──────────────────────────
    print("⚙️  step 2/3: llm_router-install-hooks ...")
    if not dry_run:
        result = subprocess.run(
            ["llm_router-install-hooks"], capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(
                f"✗ llm_router-install-hooks failed:\n{result.stderr}",
                file=sys.stderr,
            )
            return 1
        copied = sum(1 for line in result.stdout.splitlines() if "Copied" in line)
        print(f"   {copied} hook(s) refreshed")

    # ── Step 3: kill running MCP servers ────────────────────────────────
    if skip_mcp:
        print("⚙️  step 3/3: skipped (--skip-mcp-kill)")
    else:
        self_pid = os.getpid()
        pids = _list_mcp_servers(self_pid)
        print(f"⚙️  step 3/3: stale MCP servers to restart: {pids if pids else '(none)'}")
        if not dry_run:
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if pids:
                print(f"   sent SIGTERM to {len(pids)} server(s)")

    print()
    print("✓ Refresh complete. Open a fresh Claude Code session for new MCP code.")
    return 0


if __name__ == "__main__":
    sys.exit(cmd_dev_refresh(sys.argv[1:]))
