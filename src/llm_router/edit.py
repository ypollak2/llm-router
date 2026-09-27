"""Edit instruction generation via cheap routed models.

``llm_edit`` is the bridge between cheap-model reasoning and mechanical file
editing. Instead of having Opus/Sonnet read files, understand context, and
figure out what to change (expensive), this module:

1. Reads the relevant file contents (capped at 32 KB each, free).
2. Builds a structured prompt asking the cheap model to produce JSON edit
   instructions in ``{file, old_string, new_string}`` format.
3. Parses the JSON response into ``EditInstruction`` dataclasses.
4. Validates every instruction against the ORIGINAL file content — exact,
   unique match required — then syntax-checks the result for known file
   types, all-or-nothing (:func:`apply_edits`).
5. Returns a formatted result that Claude can apply mechanically.

The caller (``llm_edit`` MCP tool in ``tools/text.py``) drives the retry loop:
routes the prompt via ``route_and_call(TaskType.CODE, ...)`` (which sends
``think: false`` to any Ollama model — see ``providers._ollama_think_enabled``),
validates the reply with :func:`apply_edits`, and — on rejection — feeds the
rejection reason back to the model for up to 3 attempts total. This mirrors
the validated NS3 pattern in ``rsi_engine.scaffold`` (``~/Projects/rsi-engine``):
exact-once SEARCH matching, all-or-nothing apply, a syntax gate, and rejected
edits fed back with the reason rather than silently retried blind.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Maximum bytes to read from each file.  Files larger than this are truncated
# and the model is told how many bytes were omitted.
_MAX_FILE_BYTES = 32_768

#: Default cap on edit attempts (initial try + retries-with-feedback).
MAX_EDIT_ATTEMPTS = 3

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


@dataclass
class EditResult:
    """The structured outcome of an ``llm_edit`` attempt loop.

    Attributes:
        edits: The instructions from the attempt that decided the result —
            the accepted set if ``applied`` is True, otherwise the last
            (rejected) set the model returned, for diagnosis.
        applied: True iff every instruction validated (exact-once match)
            and every touched file still parses for its known type. This is
            "ready to use as-is", not proof of a disk write — ``llm_edit``
            never writes files itself; the caller applies the pairs via its
            own Edit tool. See ``edit_ledger.record_edit_outcome``.
        rejected_reasons: One entry per rejected attempt (parse failure or
            validation failure), oldest first — this is the feedback trail
            fed back into the prompt on each retry.
        attempts: How many model calls were made (1..MAX_EDIT_ATTEMPTS).
    """

    edits: list[EditInstruction]
    applied: bool
    rejected_reasons: list[str] = field(default_factory=list)
    attempts: int = 0


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


def build_edit_prompt(
    task: str,
    file_contents: dict[str, str],
    *,
    feedback: str | None = None,
) -> str:
    """Build the prompt that asks the cheap model for edit instructions.

    Args:
        task: Natural-language description of what to change.
        file_contents: Mapping of ``file_path -> content`` (already read and
            optionally truncated).
        feedback: If a previous attempt was rejected, the reason — appended
            so the retry targets the actual problem instead of repeating it
            blind. ``None`` on the first attempt.

    Returns:
        A structured prompt string ready to send to the cheap model.
    """
    files_section = "\n\n".join(
        f"### File: {path}\n```\n{content}\n```"
        for path, content in file_contents.items()
    )

    # The format example is shown with the REAL first file path, not a
    # placeholder. rsi_engine.scaffold found a placeholder path ("path/of/the/
    # file") gets copied into the model's answer literally (3/3 in that
    # trial) — the model pattern-matches the example instead of reading the
    # actual file list. Using the real path here closes that failure mode.
    example_path = next(iter(file_contents), "the/file/path")

    prompt = f"""You are a precise code editor. Your job is to return exact edit instructions.

## Task
{task}

## Files
{files_section}

## Instructions
Return a JSON array of edit objects. Each object must have:
- "file": the file path (exactly as given above, e.g. "{example_path}")
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
    "file": "{example_path}",
    "old_string": "def foo():\\n    pass",
    "new_string": "def foo():\\n    return 42",
    "description": "Implement foo to return 42"
  }}
]
```"""

    if feedback:
        prompt += f"\n\n## Your previous answer was rejected\n{feedback}\nAnswer again, fixing that."

    return prompt


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


def check_syntax(path: str, text: str) -> str | None:
    """Syntax-check *text* for known file types. Returns an error message, or None.

    Only checks types this repo can validate without a network call or an
    external interpreter: Python (``ast.parse``), JSON, and YAML. Anything
    else (including no extension) is not checked — that is a scope
    limitation, not a claim that unchecked files are fine.
    """
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".py":
            ast.parse(text)
        elif suffix == ".json":
            json.loads(text)
        elif suffix in (".yaml", ".yml"):
            yaml.safe_load(text)
    except Exception as exc:
        kind = suffix.lstrip(".") or "text"
        return f"{path} is no longer valid {kind}: {exc}"
    return None


def apply_edits(
    file_contents: dict[str, str],
    instructions: list[EditInstruction],
) -> tuple[dict[str, str] | None, list[str]]:
    """Validate every instruction, then apply — all-or-nothing.

    Each ``old_string`` is checked against the file's CURRENT state (so two
    edits to the same file compose in order) and must appear EXACTLY ONCE.
    If any instruction fails that check, or if any touched file no longer
    parses for its known type afterward, the WHOLE batch is rejected — a
    partial apply would leave files in a state nobody reviewed.

    Args:
        file_contents: The ORIGINAL file contents (path -> text), as read
            from disk. Never mutated.
        instructions: Parsed edit instructions to validate and apply.

    Returns:
        ``(new_contents, reasons)``. On success, ``new_contents`` is the
        full ``path -> text`` mapping with edits applied and ``reasons`` is
        empty. On rejection, ``new_contents`` is ``None`` and ``reasons``
        lists every problem found (so the caller can feed all of them back
        at once rather than discovering them one retry at a time).
    """
    reasons: list[str] = []
    new_contents = dict(file_contents)

    if not instructions:
        return None, ["no edit instructions to apply"]

    for instr in instructions:
        if instr.file not in file_contents:
            reasons.append(f"{instr.file}: not one of the files given to llm_edit")
            continue
        if not instr.old_string.strip():
            reasons.append(f"{instr.file}: old_string is empty or whitespace-only")
            continue
        text = new_contents[instr.file]
        count = text.count(instr.old_string)
        if count != 1:
            reasons.append(
                f"{instr.file}: old_string appears {count} times (need exactly 1) — "
                "include more surrounding context to make it unique"
            )
            continue
        new_contents[instr.file] = text.replace(instr.old_string, instr.new_string, 1)

    if reasons:
        return None, reasons

    for path, text in new_contents.items():
        if text == file_contents.get(path):
            continue  # untouched file — nothing new to check
        err = check_syntax(path, text)
        if err:
            reasons.append(err)

    if reasons:
        return None, reasons

    if new_contents == file_contents:
        return None, ["the edits change nothing"]

    return new_contents, []


def format_edit_result(
    instructions: list[EditInstruction],
    warnings: list[str],
    model_header: str,
    *,
    applied: bool | None = None,
    attempts: int | None = None,
) -> str:
    """Format the final output string returned by the ``llm_edit`` MCP tool.

    The output is designed to be copy-pasteable into Claude Code's Edit tool:
    each instruction is shown with file, description, old_string, and
    new_string clearly delimited.

    Args:
        instructions: Parsed edit instructions.
        warnings: Any parse/validation warnings to surface to the user.
        model_header: The ``LLMResponse.header()`` string for the routed call.
        applied: If given, whether validation passed (exact-once + syntax) —
            i.e. the returned pairs are ready to apply as-is. ``None`` keeps
            the legacy (pre-hardening) output shape for callers that don't
            track it.
        attempts: If given, how many model calls the retry loop made.

    Returns:
        A multi-line formatted string.
    """
    lines = [model_header, ""]

    if applied is not None or attempts is not None:
        bits = []
        if applied is not None:
            bits.append(f"applied={applied}")
        if attempts is not None:
            bits.append(f"attempts={attempts}")
        lines.append(f"_Structured: {', '.join(bits)}_")
        lines.append("")

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
