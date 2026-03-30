"""Edit instruction generation via cheap routed models.

``llm_edit`` is the bridge between cheap-model reasoning and mechanical file
editing. Instead of having Opus/Sonnet read files, understand context, and
figure out what to change (expensive), this module:

1. Reads the relevant file contents (capped at 32 KB each, free).
2. Builds a structured prompt asking the cheap model to produce JSON edit
   instructions in ``{file, old_string, new_string}`` format.
3. Parses the JSON response into ``EditInstruction`` dataclasses.
4. Returns a formatted result that Claude can apply mechanically.

The caller (``llm_edit`` MCP tool in ``server.py``) routes the prompt via
``route_and_call(TaskType.CODE, ...)`` so model selection follows the normal
benchmark-aware, pressure-aware chain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Maximum bytes to read from each file.  Files larger than this are truncated
# and the model is told how many bytes were omitted.
_MAX_FILE_BYTES = 32_768

# JSON block regex: matches ```json ... ``` or bare [ ... ] / { ... } blocks.
_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*([\[{].*?[\]}])\s*```|^\s*([\[{].*[\]}])\s*$",
    re.DOTALL | re.MULTILINE,
)


@dataclass(frozen=True)
class EditInstruction:
    """A single exact-string replacement in a file.

    Attributes:
        file: Relative or absolute path to the file to edit.
        old_string: The exact text to find and replace (must be unique in file).
        new_string: The replacement text.
        description: Optional human-readable reason for this edit.
    """

    file: str
    old_string: str
    new_string: str
    description: str = ""


def read_file_for_edit(path: str, max_bytes: int = _MAX_FILE_BYTES) -> tuple[str, bool]:
    """Read a file, capping output at *max_bytes* bytes.

    Args:
        path: File path (relative to CWD or absolute).
        max_bytes: Maximum bytes to include.  Defaults to 32 KB.

    Returns:
        A tuple ``(content, truncated)`` where ``content`` is the file text
        (UTF-8, errors replaced) and ``truncated`` is True if the file was
        larger than the limit.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return f"[Error reading {path}: {exc}]", False

    if len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="replace"), True
    return raw.decode("utf-8", errors="replace"), False


def build_edit_prompt(task: str, file_contents: dict[str, str]) -> str:
    """Build the prompt that asks the cheap model for edit instructions.

    Args:
        task: Natural-language description of what to change.
        file_contents: Mapping of ``file_path -> content`` (already read and
            optionally truncated).

    Returns:
        A structured prompt string ready to send to the cheap model.
    """
    files_section = "\n\n".join(
        f"### File: {path}\n```\n{content}\n```"
        for path, content in file_contents.items()
    )

    return f"""You are a precise code editor. Your job is to return exact edit instructions.

## Task
{task}

## Files
{files_section}

## Instructions
Return a JSON array of edit objects. Each object must have:
- "file": the file path (exactly as given above)
- "old_string": the exact text to find (must appear verbatim in the file)
- "new_string": the replacement text
- "description": one-line reason for this change

Rules:
- old_string must be an EXACT substring of the file content (including whitespace and indentation).
- old_string must be unique within the file; include enough surrounding lines to ensure uniqueness.
- If no changes are needed, return an empty array [].
- Return ONLY the JSON array, no prose before or after.

Example:
```json
[
  {{
    "file": "src/main.py",
    "old_string": "def foo():\\n    pass",
    "new_string": "def foo():\\n    return 42",
    "description": "Implement foo to return 42"
  }}
]
```"""


def parse_edit_response(raw: str) -> tuple[list[EditInstruction], list[str]]:
    """Parse the cheap model's response into ``EditInstruction`` objects.

    Tries to extract a JSON array from the response, handling markdown code
    fences, leading prose, and minor formatting variations.

    Args:
        raw: The raw string response from the cheap model.

    Returns:
        A tuple ``(instructions, warnings)`` where ``instructions`` is a list
        of valid ``EditInstruction`` objects and ``warnings`` is a list of
        human-readable strings describing any parse errors or skipped items.
    """
    warnings: list[str] = []

    # Try to find a JSON block (fenced or bare)
    json_text = _extract_json(raw)
    if not json_text:
        warnings.append("No JSON array found in model response. No edits will be applied.")
        return [], warnings

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        warnings.append(f"JSON parse error: {exc}. No edits will be applied.")
        return [], warnings

    if not isinstance(data, list):
        warnings.append(f"Expected JSON array, got {type(data).__name__}. No edits applied.")
        return [], warnings

    instructions: list[EditInstruction] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            warnings.append(f"Item {i} is not a dict — skipped.")
            continue
        missing = [k for k in ("file", "old_string", "new_string") if k not in item]
        if missing:
            warnings.append(f"Item {i} missing keys {missing} — skipped.")
            continue
        instructions.append(EditInstruction(
            file=str(item["file"]),
            old_string=str(item["old_string"]),
            new_string=str(item["new_string"]),
            description=str(item.get("description", "")),
        ))

    return instructions, warnings


def _extract_json(text: str) -> str | None:
    """Try several strategies to extract a JSON array from model output."""
    # Strategy 1: fenced code block
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1)

    # Strategy 2: bare array starting with [
    m = re.search(r"(\[.*\])", text, re.DOTALL)
    if m:
        return m.group(1)

    return None


def format_edit_result(
    instructions: list[EditInstruction],
    warnings: list[str],
    model_header: str,
) -> str:
    """Format the final output string returned by the ``llm_edit`` MCP tool.

    The output is designed to be copy-pasteable into Claude Code's Edit tool:
    each instruction is shown with file, description, old_string, and
    new_string clearly delimited.

    Args:
        instructions: Parsed edit instructions.
        warnings: Any parse warnings to surface to the user.
        model_header: The ``LLMResponse.header()`` string for the routed call.

    Returns:
        A multi-line formatted string.
    """
    lines = [model_header, ""]

    if warnings:
        lines.append("**Warnings:**")
        for w in warnings:
            lines.append(f"  - {w}")
        lines.append("")

    if not instructions:
        lines.append("No edits to apply.")
        return "\n".join(lines)

    lines.append(f"**{len(instructions)} edit(s) to apply:**\n")
    for i, instr in enumerate(instructions, 1):
        lines.append(f"### Edit {i}: {instr.file}")
        if instr.description:
            lines.append(f"_{instr.description}_")
        lines.append("")
        lines.append("**Replace:**")
        lines.append(f"```\n{instr.old_string}\n```")
        lines.append("**With:**")
        lines.append(f"```\n{instr.new_string}\n```")
        lines.append("")

    # Also emit machine-readable JSON for Claude to act on
    lines.append("---")
    lines.append("**Raw JSON (for automated application):**")
    lines.append("```json")
    lines.append(json.dumps(
        [
            {
                "file": instr.file,
                "old_string": instr.old_string,
                "new_string": instr.new_string,
                "description": instr.description,
            }
            for instr in instructions
        ],
        indent=2,
        ensure_ascii=False,
    ))
    lines.append("```")

    return "\n".join(lines)
