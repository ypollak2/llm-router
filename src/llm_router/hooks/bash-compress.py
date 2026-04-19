#!/usr/bin/env python3
# llm-router-hook-version: 1
"""PostToolUse hook — compress Bash command outputs via RTK-style filtering.

After every bash tool call:
  1. Extract command and output from tool result
  2. Skip if output < OUTPUT_LINE_THRESHOLD lines (not worth compressing)
  3. Apply RTK compression filters (command-specific + generic fallback)
  4. Log compression stats to SQLite (original → compressed tokens)
  5. Inject compressed output as contextForAgent

Token savings: 60-90% reduction on shell commands (git, pytest, cargo, docker, etc.)
by removing noise, duplicates, and verbose output.

Env vars:
  LLM_ROUTER_BASH_COMPRESS       Set to "off" to disable this hook
  LLM_ROUTER_DB_PATH             SQLite path for compression stats (default: ~/.llm-router/usage.db)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# ── RTK Compression ────────────────────────────────────────────────────────────

try:
    from llm_router.compression.rtk_adapter import RTKAdapter
except ImportError:
    RTKAdapter = None


def _estimate_tokens(text: str) -> int:
    """Estimate token count (rough: 1 token ≈ 4 characters)."""
    return max(1, len(text) // 4)


def _compress_bash_output(command: str, output: str) -> dict:
    """Apply RTK compression to bash output.

    Returns dict with:
      - output: compressed output string
      - original_tokens: token count before compression
      - compressed_tokens: token count after compression
      - compression_ratio: percentage reduction
      - strategy: which compression filter was used
    """
    if not RTKAdapter:
        return {
            "output": output,
            "original_tokens": _estimate_tokens(output),
            "compressed_tokens": _estimate_tokens(output),
            "compression_ratio": 1.0,
            "strategy": "unavailable",
        }

    adapter = RTKAdapter(enable=True)
    result = adapter.compress(command, output)

    return {
        "output": result.output,
        "original_tokens": result.original_tokens,
        "compressed_tokens": result.compressed_tokens,
        "compression_ratio": result.compression_ratio,
        "strategy": result.strategy,
    }


async def _log_compression_stat(
    command: str,
    original_tokens: int,
    compressed_tokens: int,
    compression_ratio: float,
    strategy: str,
) -> None:
    """Log compression stat to SQLite database asynchronously."""
    try:
        from llm_router.cost import log_compression_stat
        await log_compression_stat(
            command=command,
            layer="rtk",
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=compression_ratio,
            strategy=strategy,
        )
    except (ImportError, Exception):
        pass  # Fail silently if logging unavailable


def _bare_tool_name(full_name: str) -> str:
    """Strip MCP server prefix: mcp__plugin_playwright__browser_snapshot → browser_snapshot."""
    return full_name.split("__")[-1] if "__" in full_name else full_name


def _extract_bash_output(payload: dict) -> tuple[str, str] | None:
    """Extract command and output from Bash tool result.

    Returns (command, output) tuple or None if extraction fails.
    """
    # Tool params contain the command
    tool_params = payload.get("toolInputs", {})
    if isinstance(tool_params, str):
        try:
            tool_params = json.loads(tool_params)
        except json.JSONDecodeError:
            return None

    command = tool_params.get("command", "")

    # Tool result contains stdout/stderr
    result = payload.get("toolResult", {})
    if isinstance(result, str):
        output = result
    elif isinstance(result, dict):
        output = result.get("text", "")
        if not output:
            # Try to extract from content array
            content = result.get("content", [])
            if isinstance(content, list):
                parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                output = "\n".join(parts)
    else:
        return None

    if not command or not output:
        return None

    return command, output


def main() -> None:
    """Main entry point for PostToolUse hook."""
    # Allow opt-out via env var
    if os.environ.get("LLM_ROUTER_BASH_COMPRESS", "").lower() == "off":
        sys.exit(0)

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    # Only fire for bash tool
    tool_name = payload.get("toolName", "")
    if _bare_tool_name(tool_name) != "execute_shell_command" and not tool_name.endswith("bash"):
        sys.exit(0)

    extraction = _extract_bash_output(payload)
    if not extraction:
        sys.exit(0)

    command, output = extraction

    # Skip compression for small outputs
    original_lines = output.count("\n") + 1
    if original_lines < 5:  # Only compress if worth it
        sys.exit(0)

    # Apply RTK compression
    compression = _compress_bash_output(command, output)

    # Only report if meaningful compression achieved
    compression_pct = (1 - compression["compression_ratio"]) * 100
    if compression_pct < 10:  # Less than 10% reduction isn't worth mentioning
        sys.exit(0)

    tokens_saved = compression["original_tokens"] - compression["compressed_tokens"]

    # Log stat asynchronously
    asyncio.run(_log_compression_stat(
        command=command,
        original_tokens=compression["original_tokens"],
        compressed_tokens=compression["compressed_tokens"],
        compression_ratio=compression["compression_ratio"],
        strategy=compression["strategy"],
    ))

    # Format output
    compressed_lines = compression["output"].count("\n") + 1
    context = (
        f"[LLM-Router RTK] Command output compressed via {compression['strategy']}:\n"
        f"  {original_lines} → {compressed_lines} lines | "
        f"{compression['original_tokens']:,} → {compression['compressed_tokens']:,} tokens | "
        f"{compression_pct:.0f}% reduction\n"
        f"\n{compression['output']}\n"
        f"\n⚡ {tokens_saved:,} tokens saved via RTK compression. "
        f"Full output available if needed."
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "contextForAgent": context,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
