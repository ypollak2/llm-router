"""Filesystem operation tools — llm_fs_find, llm_fs_rename, llm_fs_edit_many.

Routes filesystem reasoning to cheap models (Haiku/Ollama/Gemini Flash)
instead of burning Opus tokens on glob/grep generation and bulk rename logic.

Pattern:
  1. Describe the task in natural language.
  2. Cheap model generates glob patterns, grep commands, or {file, old, new} JSON.
  3. Claude executes the mechanical output using Read/Edit/Bash tools.
"""

from __future__ import annotations

import glob as _glob

from mcp.server.fastmcp import Context

from llm_router.edit import build_edit_prompt, format_edit_result, parse_edit_response, read_file_for_edit
from llm_router.router import route_and_call
from llm_router.types import TaskType

# Maximum files to process in a single bulk-edit call.
_MAX_FILES = 20


async def llm_fs_find(
    description: str,
    ctx: Context,
    root: str | None = None,
) -> str:
    """Generate glob/grep commands to find files matching a natural-language description.

    Routes to Haiku/Ollama so the cheap model does pattern thinking.
    Claude executes the returned commands with Glob/Grep/Bash.

    Args:
        description: What you're looking for, e.g. "all Python files that import sqlite3"
            or "TypeScript files with TODO comments added in the last week".
        root: Optional root directory to search in. Defaults to current working directory.
    """
    root_line = f"Root directory: {root}" if root else "Root directory: current working directory"
    prompt = f"""Generate shell commands to find files matching this description:

{description}

{root_line}

Return a JSON object with:
- "glob_patterns": list of glob patterns (e.g. ["**/*.py", "src/**/*.ts"])
- "grep_commands": list of shell grep/rg commands to narrow results further
- "explanation": brief description of the search strategy

Return ONLY the JSON object, no prose."""

    resp = await route_and_call(
        TaskType.QUERY, prompt,
        complexity_hint="simple",
        ctx=ctx,
    )
    return f"{resp.header()}\n\n{resp.content}"


async def llm_fs_rename(
    description: str,
    ctx: Context,
    dry_run: bool = True,
) -> str:
    """Generate shell commands for a file rename/reorganisation operation.

    Describe what you want to rename and the cheap model produces the mv/git mv
    commands. Use ``dry_run=True`` (default) to get echo-prefixed commands safe
    to inspect before running.

    Args:
        description: What to rename and how, e.g. "rename all _old.py files in
            src/ to remove the _old suffix" or "move all test_*.py files from
            tests/unit/ into tests/".
        dry_run: When True, commands are prefixed with ``echo`` for safe review.
            Set to False to get directly executable commands.
    """
    dry_hint = (
        "Prefix every command with 'echo' so it can be reviewed safely (dry-run mode)."
        if dry_run
        else "Generate directly executable commands (no echo prefix)."
    )

    prompt = f"""Generate shell commands to perform this file rename operation:

{description}

{dry_hint}

Return a JSON object with:
- "commands": list of shell commands (mv, rename, git mv, etc.)
- "explanation": one-line description of what each command does
- "warnings": list of potential issues or conflicts to watch out for (empty list if none)
- "reversible": true or false — whether the operation is easily reversible

Return ONLY the JSON object, no prose."""

    resp = await route_and_call(
        TaskType.QUERY, prompt,
        complexity_hint="simple",
        ctx=ctx,
    )
    return f"{resp.header()}\n\n{resp.content}"


async def llm_fs_edit_many(
    task: str,
    ctx: Context,
    files: list[str] | None = None,
    glob_pattern: str | None = None,
    max_files: int = _MAX_FILES,
) -> str:
    """Generate bulk edit instructions across multiple files.

    Extends the ``llm_edit`` pattern to many files at once: the cheap model
    reads all target files and returns a JSON array of ``{file, old_string,
    new_string}`` edit instructions. Claude applies them mechanically.

    Use this for cross-file refactors, bulk renames within files, or updating
    repeated patterns across a module.

    Args:
        task: Natural-language description of what to change, e.g.
            "replace all `import sqlite3` with `import aiosqlite as sqlite3`"
            or "update the copyright year from 2024 to 2025 in all file headers".
        files: Explicit list of file paths to process.
        glob_pattern: Glob pattern to find files (e.g. "src/**/*.py"). Use
            either ``files`` or ``glob_pattern``, not both.
        max_files: Cap on files processed in one call (default 20). Raise if
            you need more — but consider splitting into batches for large refactors.
    """
    # Resolve file list
    resolved: list[str] = []
    if files:
        resolved = [str(f) for f in files[:max_files]]
    elif glob_pattern:
        resolved = sorted(_glob.glob(glob_pattern, recursive=True))[:max_files]

    if not resolved:
        return (
            "**Error**: No files to process. "
            "Provide a `files` list or a `glob_pattern` (e.g. `src/**/*.py`)."
        )

    # Read file contents — capped at 32 KB each (free local read)
    file_contents: dict[str, str] = {}
    for path in resolved:
        content, truncated = read_file_for_edit(path)
        if truncated:
            content += "\n\n[... file truncated at 32 KB — only first 32 KB shown ...]"
        file_contents[path] = content

    prompt = build_edit_prompt(task, file_contents)
    resp = await route_and_call(
        TaskType.CODE, prompt,
        complexity_hint="moderate",
        ctx=ctx,
    )

    instructions, warnings = parse_edit_response(resp.content)
    return format_edit_result(instructions, warnings, resp.header())


def register(mcp) -> None:
    """Register filesystem tools with the FastMCP instance."""
    mcp.tool()(llm_fs_find)
    mcp.tool()(llm_fs_rename)
    mcp.tool()(llm_fs_edit_many)
