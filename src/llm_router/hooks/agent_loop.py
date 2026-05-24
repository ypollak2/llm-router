"""Mini agent loop — gives any LLM (Ollama, Gemini, OpenAI) file tool access.

Mirrors Claude Code's agent pattern:
  1. LLM receives prompt + tool definitions
  2. LLM outputs tool_calls (read_file, edit_file, run_command, etc.)
  3. This module executes them locally and feeds results back
  4. Repeat until LLM outputs a final text response (no tool_calls)

Safety:
  - All file operations are sandboxed to the project directory
  - Commands run with a timeout (default 30s)
  - Maximum loop iterations prevent infinite loops
  - Dangerous commands (rm -rf, etc.) are blocked
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path


# ── Tool Definitions (sent to the LLM) ───────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Returns the file content as text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to project root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to project root"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file with new content. The old_string must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to project root"},
                    "old_string": {"type": "string", "description": "Exact string to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory. Returns file names, one per line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to project root"},
                    "pattern": {"type": "string", "description": "Glob pattern to filter (e.g., '*.py')"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a pattern in files. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in (relative to project root)"},
                    "file_pattern": {"type": "string", "description": "Glob to filter files (e.g., '*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return its output. Use for running tests, linting, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
]


# ── Tool Execution (runs locally) ────────────────────────────────────────────

# Commands that are too dangerous to run from an automated agent
_BLOCKED_COMMANDS = re.compile(
    r"rm\s+-rf\s+/|"
    r"rm\s+-rf\s+~|"
    r"rm\s+-rf\s+\.\.|"
    r"mkfs|"
    r"dd\s+if=|"
    r">\s*/dev/|"
    r"chmod\s+-R\s+777\s+/|"
    r"curl.*\|\s*(?:ba)?sh|"
    r"wget.*\|\s*(?:ba)?sh",
    re.IGNORECASE,
)


def _resolve_path(path: str, project_root: Path) -> Path:
    """Resolve a path safely within the project root.

    Prevents path traversal attacks (../../etc/passwd).
    """
    # Handle absolute paths by making them relative
    if os.path.isabs(path):
        resolved = Path(path).resolve()
    else:
        resolved = (project_root / path).resolve()

    # Ensure the resolved path is within the project root
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        raise PermissionError(f"Path '{path}' resolves outside project root")

    return resolved


def execute_tool(name: str, args: dict, project_root: Path) -> str:
    """Execute a tool call and return the result as a string."""
    try:
        if name == "read_file":
            path = _resolve_path(args["path"], project_root)
            if not path.exists():
                return f"Error: File not found: {args['path']}"
            content = path.read_text(encoding="utf-8", errors="replace")
            # Truncate very large files
            if len(content) > 50_000:
                return content[:50_000] + f"\n... (truncated, {len(content)} chars total)"
            return content

        elif name == "write_file":
            path = _resolve_path(args["path"], project_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            return f"Written {len(args['content'])} chars to {args['path']}"

        elif name == "edit_file":
            path = _resolve_path(args["path"], project_root)
            if not path.exists():
                return f"Error: File not found: {args['path']}"
            content = path.read_text(encoding="utf-8")
            old = args["old_string"]
            new = args["new_string"]
            if old not in content:
                return f"Error: old_string not found in {args['path']}"
            if content.count(old) > 1:
                return f"Error: old_string appears {content.count(old)} times — must be unique"
            content = content.replace(old, new, 1)
            path.write_text(content, encoding="utf-8")
            return f"Edited {args['path']}: replaced {len(old)} chars with {len(new)} chars"

        elif name == "list_files":
            path = _resolve_path(args["path"], project_root)
            if not path.is_dir():
                return f"Error: Not a directory: {args['path']}"
            pattern = args.get("pattern", "*")
            files = sorted(str(f.relative_to(project_root)) for f in path.glob(pattern) if f.is_file())
            if not files:
                return "(no matching files)"
            return "\n".join(files[:200])  # Cap at 200 entries

        elif name == "search_files":
            search_path = _resolve_path(args.get("path", "."), project_root)
            file_pattern = args.get("file_pattern", "*.py")
            pattern = args["pattern"]
            regex = re.compile(pattern, re.IGNORECASE)
            results = []
            for fpath in search_path.rglob(file_pattern):
                if not fpath.is_file():
                    continue
                try:
                    for i, line in enumerate(fpath.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if regex.search(line):
                            rel = fpath.relative_to(project_root)
                            results.append(f"{rel}:{i}: {line.strip()}")
                            if len(results) >= 50:
                                break
                except (OSError, UnicodeDecodeError):
                    continue
                if len(results) >= 50:
                    break
            if not results:
                return "(no matches)"
            return "\n".join(results)

        elif name == "run_command":
            cmd = args["command"]
            if _BLOCKED_COMMANDS.search(cmd):
                return f"Error: Command blocked for safety: {cmd}"
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    timeout=30, cwd=str(project_root),
                )
                output = result.stdout
                if result.stderr:
                    output += f"\nSTDERR:\n{result.stderr}"
                if result.returncode != 0:
                    output += f"\n(exit code: {result.returncode})"
                # Truncate long output
                if len(output) > 10_000:
                    output = output[:10_000] + "\n... (truncated)"
                return output or "(no output)"
            except subprocess.TimeoutExpired:
                return "Error: Command timed out after 30s"

        else:
            return f"Error: Unknown tool: {name}"

    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error executing {name}: {e}"


# ── Agent Loop ───────────────────────────────────────────────────────────────

_MAX_ITERATIONS = 15  # Safety cap — prevent infinite loops


def _get_ollama_url() -> str:
    return os.environ.get("LLM_ROUTER_OLLAMA_URL") or \
           os.environ.get("OLLAMA_BASE_URL") or \
           "http://localhost:11434"


def run_agent_loop(
    prompt: str,
    model: str,
    project_root: Path,
    timeout_per_call: int = 60,
    system_prompt: str | None = None,
) -> str | None:
    """Run a tool-calling agent loop with an Ollama model.

    Sends the prompt with tool definitions, executes any tool calls,
    feeds results back, and repeats until the model returns a final
    text response (no tool calls).

    Returns the final text response, or None if the loop fails.
    """
    messages = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    else:
        messages.append({
            "role": "system",
            "content": (
                "You are a coding assistant with access to file tools. "
                "Use the tools to read, edit, and test code. "
                "When you're done, provide a summary of what you did."
            ),
        })

    messages.append({"role": "user", "content": prompt})

    ollama_url = _get_ollama_url()

    for iteration in range(1, _MAX_ITERATIONS + 1):
        body = json.dumps({
            "model": model,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
            "stream": False,
            "think": False,
        }).encode()

        req = urllib.request.Request(
            f"{ollama_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_per_call) as resp:
                result = json.loads(resp.read())
        except Exception:
            return None

        msg = result.get("message", {})
        tool_calls = msg.get("tool_calls", [])
        content = msg.get("content", "")
        thinking = msg.get("thinking", "")

        # If no tool calls → this is the final response
        if not tool_calls:
            return content or thinking or None

        # Add assistant message with tool calls to conversation
        messages.append(msg)

        # Execute each tool call and add results
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            tool_args = func.get("arguments", {})

            tool_result = execute_tool(tool_name, tool_args, project_root)

            messages.append({
                "role": "tool",
                "content": tool_result,
            })

    # Hit max iterations — return whatever we have
    return "Agent reached maximum iterations. Partial work may have been done."
