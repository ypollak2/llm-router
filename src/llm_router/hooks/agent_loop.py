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
import time
import urllib.request
from pathlib import Path

from llm_router import trace as _trace
from llm_router.hooks import agent_writes as _writes
from llm_router.hooks import context_budget as _budget
from llm_router.local_context_guard import (
    ContextOverflow,
    check_overflow,
    check_truncated,
    effective_window,
    estimate_payload_tokens,
)

# One executor. The tool primitives, the command parser/runner and the Ollama
# helpers live in llm_router.toolkit; this module keeps its hook-shaped loop and
# the legacy tool names, and calls the shared code. The names below are kept as
# wrappers/aliases because hooks, registries and tests import them from here.
from llm_router.toolkit import tools as _kit
from llm_router.toolkit.adapters import ollama as _ollama


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
    r">\s*/dev/(?!null\b)|"   # a device, but not the discard sink (S)
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


def _deny_secret(path: Path, root: Path) -> None:
    """The toolkit's secret deny-list applies to the hook loop too (PLAN section 0.8:
    `.env` and key files inside the repo used to be readable)."""
    from llm_router.toolkit.policy import is_secret_relpath
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        return
    if rel != "." and is_secret_relpath(rel):
        raise PermissionError("that path is not available")


def execute_tool(name: str, args: dict, project_root: Path) -> str:
    """Execute a tool call and return the result as a string."""
    # Every path this function reports must be relative to the SAME root that
    # _resolve_path validated against, which is the resolved one. Using the
    # caller's unresolved root here made `relative_to` raise for any root
    # containing a symlink — on macOS that is every path under /tmp, where
    # `/tmp` -> `/private/tmp`. list_files and search_files then returned
    # "Error executing …: is not in the subpath of …" for a directory the model
    # was perfectly entitled to read, and it burned its whole iteration budget
    # retrying. Found by the execution trace, not by a test.
    root = project_root.resolve()
    try:
        if name == "read_file":
            path = _resolve_path(args["path"], project_root)
            _deny_secret(path, root)
            if not path.exists():
                return f"Error: File not found: {args['path']}"
            # Line range, so a large file can be read in usable pieces instead
            # of being truncated into uselessness.
            content = _kit.read_text(path, args.get("offset"), args.get("limit"))
            return _budget.truncate_tool_result(content)

        elif name == "write_file":
            path = _resolve_path(args["path"], project_root)
            _deny_secret(path, root)
            content = args["content"]
            before = path.read_text(encoding="utf-8") if path.exists() else None
            allowed, message = _writes.guard(path, before, content, project_root)
            if not allowed:
                return message
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return message

        elif name == "edit_file":
            path = _resolve_path(args["path"], project_root)
            _deny_secret(path, root)
            if not path.exists():
                return f"Error: File not found: {args['path']}"
            content = path.read_text(encoding="utf-8")
            old = args["old_string"]
            new = args["new_string"]
            if old not in content:
                return f"Error: old_string not found in {args['path']}"
            if content.count(old) > 1:
                return f"Error: old_string appears {content.count(old)} times — must be unique"
            updated = content.replace(old, new, 1)
            allowed, message = _writes.guard(path, content, updated, project_root)
            if not allowed:
                return message
            path.write_text(updated, encoding="utf-8")
            return message

        elif name == "list_files":
            path = _resolve_path(args["path"], project_root)
            _deny_secret(path, root)
            if not path.is_dir():
                return f"Error: Not a directory: {args['path']}"
            files = _kit.list_glob(path, root, args.get("pattern", "*"))
            if not files:
                return "(no matching files)"
            return _budget.truncate_tool_result("\n".join(files[:200]))

        elif name == "search_files":
            search_path = _resolve_path(args.get("path", "."), project_root)
            _deny_secret(search_path, root)
            # K: a named file is searched as-is, whatever its suffix.
            results = _kit.search_text(search_path, root, args["pattern"], args.get("file_pattern", "*.py"))
            if not results:
                return "(no matches)"
            return _budget.truncate_tool_result("\n".join(results))

        elif name == "run_command":
            return _run_command_line(args["command"], project_root)

        elif name == FINISH_TOOL:
            # Never dispatched by the loop, but a model can name it through the
            # native tools= path too. An "Unknown tool" string here would be fed
            # back as a result and restart the loop that just ended.
            return "(finish acknowledged)"

        else:
            return f"Error: Unknown tool: {name}"

    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error executing {name}: {e}"


# ── Agent Loop ───────────────────────────────────────────────────────────────

# ── Constrained decoding ────────────────────────────────────────────────────
#
# Ollama compiles a JSON Schema in `format` into a GBNF grammar and masks every
# token that would break it, so a malformed tool call is never sampled — as
# opposed to being sampled and then repaired. Measured on 6 repo prompts against
# qwen3-coder:30b:
#
#     tools= alone, raw structured field      50% valid    2.4s mean
#     tools= + the XML repair shim           100% valid    2.4s mean
#     format=<schema>                        100% valid    0.6s mean
#
# Reliability AND latency improve; nothing trades off. The speedup is structural:
# there is no preamble to generate and nothing to re-parse.
#
# The schema is DERIVED from TOOL_DEFINITIONS rather than written out, so a tool
# added or renamed there cannot silently fall out of the grammar — which would
# constrain the model away from a tool it is supposed to have.
# Stopping is a TOOL, not an optional extra key.
#
# The first version of this schema had `required: [tool, arguments]` with an
# optional `done` flag, and traced like this against qwen3-coder:30b:
#
#     iter 1  search_files -> found the answer on line 33
#     iter 2  read_file    grounding.py
#     iter 3  read_file    grounding.py      <- identical
#     iter 4..15           the same call, to exhaustion
#
# The grammar could only express "call something". Continuing was always valid;
# stopping required volunteering a key the model never volunteered. Making
# `finish` an enum member puts the two on equal terms — one token either way.
FINISH_TOOL = "finish"


# ── run_command: sequences and pipelines without a shell (S) ─────────────────
# A trace of the 2026-09-24 continuation replay: the local model oriented itself
# the way Claude does — `git log --oneline -12 && git status --short | head -20`
# — and with no shell, `&&` and `|` reached git as literal arguments. It retried
# variants until its 15 steps were gone (0/20 moments reached an edit). Still no
# shell: the line is tokenized, EVERY segment passes the allowlist before ANY
# runs, and pipes are chained here. Only `>/dev/null`, `2>/dev/null` and `2>&1`
# are honoured; a redirect into a file is refused in favour of write_file.
_parse_command_line = _kit.parse_command_line


def _run_command_line(cmd: str, project_root: Path) -> str:
    if _BLOCKED_COMMANDS.search(cmd):
        return f"Error: Command blocked for safety: {cmd}"
    parsed = _parse_command_line(cmd)
    if isinstance(parsed, str):
        return parsed
    for _op, segments in parsed:                       # check ALL before running ANY
        for seg in segments:
            allowed, refusal = _writes.guard_command(seg["argv"])
            if not allowed:
                return refusal
    # R4: the child gets an ALLOWLISTED environment, never the parent's — the
    # allowlist above permits programs that can read os.environ. Fail-closed.
    try:
        from llm_router.safe_subprocess import get_delegated_env
        child_env = get_delegated_env()
    except Exception:  # noqa: BLE001
        child_env = {"PATH": os.defpath}
    # Execution (concurrent stages over real OS pipes, output cap, deadline) is
    # the toolkit's; this hook-shaped path adds only its own allowlist above.
    return _kit.run_pipelines(parsed, cwd=project_root, env=child_env, timeout_s=30,
                              truncate_fn=_budget.truncate_tool_result)


# I4 (2026-09-24): the tools a DRAFT may use. A draft answers the user's
# prompt before Claude sees it; it may look at the repo, never change it.
READ_ONLY_TOOLS = ("read_file", "list_files", "search_files")


def _tool_definitions(read_only: bool = False) -> list[dict]:
    if not read_only:
        return TOOL_DEFINITIONS
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in READ_ONLY_TOOLS]


def _tool_call_schema(read_only: bool = False) -> dict:
    """JSON Schema for one tool call, built from the live tool definitions."""
    return {
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "enum": [t["function"]["name"] for t in _tool_definitions(read_only)]
                        + [FINISH_TOOL],
            },
            "arguments": {"type": "object"},
            # Retained so a model that volunteers `done` is not punished for it.
            "done": {"type": "boolean"},
            "answer": {"type": "string"},
        },
        "required": ["tool", "arguments"],
    }


constrained_decoding_enabled = _ollama.constrained_decoding_enabled
_LARGE_WINDOW_FAMILIES = _ollama._LARGE_WINDOW_FAMILIES
_default_num_ctx = _ollama.default_num_ctx
_num_ctx = _ollama.num_ctx
_agent_temperature = _ollama.agent_temperature


_MAX_ITERATIONS = 15  # Safety cap — prevent infinite loops


_OLLAMA_URL_DEFAULT = _ollama.OLLAMA_URL_DEFAULT
_validated_ollama_url = _ollama.validated_ollama_url
_get_ollama_url = _ollama.get_ollama_url


# Tool names the model may call — used to spot a tool call the model dumped
# into its text `content` instead of the structured `tool_calls` field.
_TOOL_NAMES = "|".join(t["function"]["name"] for t in TOOL_DEFINITIONS)
_TOOLCALL_TEXT_RE = re.compile(r'\{\s*"name"\s*:\s*"(?:' + _TOOL_NAMES + r')"', re.IGNORECASE)

_KNOWN_TOOLS = {t["function"]["name"] for t in TOOL_DEFINITIONS}


def _repair_xml_toolcalls(content: str) -> list[dict]:
    """Recover tool calls emitted in Qwen's ``<function=name>`` XML dialect (shared
    implementation: llm_router.toolkit.adapters.ollama)."""
    return _ollama.repair_xml_toolcalls(content, _KNOWN_TOOLS)


def _repair_toolcalls(content: str) -> list[dict]:
    """Recover tool calls a model emitted as TEXT instead of structured output
    (shared implementation: llm_router.toolkit.adapters.ollama)."""
    return _ollama.repair_toolcalls(content, _KNOWN_TOOLS)


def _parse_constrained(content: str) -> tuple[list[dict], str | None]:
    """Parse the constrained-decoding reply.

    Returns ``(tool_calls, final_answer)``. A grammar-constrained model answers
    with the schema object rather than filling Ollama's `tool_calls` field, so
    without this the loop sees "no tool calls", concludes the model only chatted,
    and discards a perfectly good call — the same failure the XML dialect caused,
    arriving through a different door.

    ``done: true`` is how a constrained model ENDS the loop. The grammar requires
    a `tool` key, so a finished model still has to name one; `done` is what says
    to ignore it. Without that exit, finishing a task would mean emitting one more
    pointless read.
    """
    if not content:
        return [], None
    try:
        obj = json.loads(content)
    except (ValueError, TypeError):
        return [], None
    if not isinstance(obj, dict):
        return [], None

    name = obj.get("tool")
    args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}

    if name == FINISH_TOOL or obj.get("done") is True:
        # The answer may arrive at the top level or inside arguments — the
        # grammar permits both and the model uses both.
        answer = obj.get("answer") or args.get("answer") or args.get("result")
        return [], str(answer) if answer else ""

    known = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    if not isinstance(name, str) or name not in known:
        return [], None
    return [{"function": {"name": name, "arguments": args}}], None


def run_agent_loop(
    prompt: str,
    model: str,
    project_root: Path,
    timeout_per_call: int = 60,
    system_prompt: str | None = None,
    deadline_s: float | None = None,
    read_only: bool = False,
    session_id: str | None = None,
    read_log: list[str] | None = None,
) -> str | None:
    """Run a tool-calling agent loop with an Ollama model.

    ``read_only`` (I4) is the draft mode: only READ_ONLY_TOOLS are offered, any
    other call is refused, and a direct answer without opening a file is a valid
    result — a draft for a general question has nothing to read.
    ``session_id`` seeds retrieval from the files the session recently touched.
    ``read_log`` (I6), when given, receives one ``tool(target)`` entry per read
    tool that ran, so the caller can tell Claude what the draft actually saw.

    Sends the prompt with tool definitions, executes any tool calls,
    feeds results back, and repeats until the model returns a final
    text response (no tool calls).

    Returns the final text response, or None if the loop fails.
    """
    # Repo knowledge for a model that cannot see the repo. Crosses the shared
    # choke point (llm_router.context_injection) so a new execution path cannot
    # quietly skip it — tests/test_okf_choke_point.py enforces that.
    try:
        from llm_router.context_injection import inject_system_prompt
        system_prompt = inject_system_prompt(system_prompt, prompt,
                                             root=str(project_root),
                                             session_id=session_id)
    except Exception:                                        # noqa: BLE001
        pass

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

    if constrained_decoding_enabled():
        # Ollama's own guidance: pass the schema in the prompt as well as in
        # `format`. The grammar makes the SHAPE valid; the prompt is what makes
        # the CONTENT sensible — a model that has not been told what the fields
        # mean will emit schema-valid nonsense.
        messages[0]["content"] += (
            "\n\nReply with a single JSON object:\n"
            '  {"tool": "<tool name>", "arguments": {...}}\n'
            "to call a tool, or\n"
            '  {"tool": "finish", "arguments": {"answer": "<your answer>"}}\n'
            "when you can answer. Call ONE tool at a time. Never repeat a call you "
            "already made — if you have the information, call finish immediately."
        )

    messages.append({"role": "user", "content": prompt})

    ollama_url = _get_ollama_url()
    tools_used = 0  # How many tool calls actually executed across the whole loop.

    # A model that asks the identical question twice will not answer it
    # differently the third time. Traced: 13 consecutive identical read_file
    # calls, burning the whole budget and returning "reached maximum iterations"
    # — which the caller has to treat as failure anyway, after paying for it.
    last_signature: str | None = None
    repeats = 0

    # WALL CLOCK, not just per-call. 15 iterations at a 60s per-call timeout is a
    # 15-minute worst case, and this loop runs in UserPromptSubmit — before the
    # user sees anything at all. A budget that bounds the whole loop is what makes
    # running it by default tolerable; without one the honest setting is off.
    started = time.monotonic()
    _trace.emit("loop.start", model=model, project_root=str(project_root),
                deadline_s=deadline_s, max_iterations=_MAX_ITERATIONS,
                prompt=prompt)

    for iteration in range(1, _MAX_ITERATIONS + 1):
        if deadline_s is not None and (time.monotonic() - started) >= deadline_s:
            # Out of time. Return partial work only if tools actually ran —
            # otherwise this is a loop that stalled, and the caller's ladder
            # should try the next model rather than relay a stall as an answer.
            _trace.emit("loop.end", reason="budget_exhausted", iteration=iteration,
                        tools_used=tools_used,
                        elapsed_s=round(time.monotonic() - started, 1))
            return (
                f"Agent stopped after {deadline_s:g}s (budget exhausted) having "
                f"made {tools_used} tool call(s). Partial work may have been done."
            ) if tools_used else None
        # Evict from the END the loop can afford to lose, before llama.cpp
        # evicts from the end it cannot.
        messages = _budget.prune(messages)

        payload = {
            "model": model,
            "messages": messages,
            "tools": _tool_definitions(read_only),
            "stream": False,
            "think": False,
            "options": {"temperature": _agent_temperature()},
        }
        if constrained_decoding_enabled():
            payload["format"] = _tool_call_schema(read_only)
        _ctx = _num_ctx(model)
        if _ctx:
            payload["options"]["num_ctx"] = _ctx

        try:
            # Checked against the FULL payload (tools + format included, not
            # just `messages`): `_budget.prune` above only trims old tool
            # results and never accounts for tool-definition JSON size. See
            # local_context_guard module docstring.
            check_overflow(payload, num_ctx=_ctx, base_url=ollama_url, model=model,
                           site="hooks.agent_loop")
        except ContextOverflow as _exc:
            _trace.emit("llm.error", iteration=iteration, error=str(_exc))
            _trace.emit("loop.end", reason="local_context_overflow", iteration=iteration,
                        tools_used=tools_used)
            return (
                f"Agent stopped after {iteration} iteration(s): the prompt would overflow "
                f"the local model's context window ({_exc}). Escalating."
            ) if tools_used else None

        body = json.dumps(payload).encode()

        req = urllib.request.Request(
            f"{ollama_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
        )

        _trace.emit("llm.request", iteration=iteration, model=model,
                    n_messages=len(messages), payload_bytes=len(body))
        _t0 = time.monotonic()
        # A call that starts inside the budget must also END inside it: a 60s
        # per-call timeout begun at second 50 of a 55s hook overruns by 55s.
        call_timeout = float(timeout_per_call)
        if deadline_s is not None:
            call_timeout = max(0.1, min(call_timeout, deadline_s - (_t0 - started)))
        try:
            with urllib.request.urlopen(req, timeout=call_timeout) as resp:
                result = json.loads(resp.read())
        except Exception as _exc:
            _trace.emit("llm.error", iteration=iteration,
                        error=f"{type(_exc).__name__}: {_exc}",
                        ms=int((time.monotonic() - _t0) * 1000))
            _trace.emit("loop.end", reason="llm_unreachable", iteration=iteration,
                        tools_used=tools_used)
            return None

        # The pre-call check above is an estimate; this is Ollama's own number
        # for what it actually evaluated. Recorded for `doctor` / `status`
        # only (CHZ-FO-LOCAL-CTX-TRUNCATED) -- it does not change what this
        # iteration does with the reply, since `check_overflow` already
        # refused anything that should not have been sent in the first place.
        _window, _ = effective_window(num_ctx=_ctx, base_url=ollama_url, model=model)
        check_truncated(result.get("prompt_eval_count"), estimate_payload_tokens(payload),
                        _window, site="hooks.agent_loop", model=model)

        msg = result.get("message", {})
        tool_calls = msg.get("tool_calls", [])
        content = msg.get("content", "")
        thinking = msg.get("thinking", "")
        _trace.emit("llm.response", iteration=iteration,
                    ms=int((time.monotonic() - _t0) * 1000),
                    n_tool_calls=len(tool_calls), content=content,
                    thinking_len=len(thinking or ""))

        # Constrained decoding puts the call in `content` as schema JSON, not in
        # Ollama's `tool_calls` field. Tried before the repair shim because it is
        # the exact shape we asked the grammar for.
        if not tool_calls and content:
            tool_calls, _final = _parse_constrained(content)
            if _final is not None:
                # The model declared itself done. Same guard as below: a loop that
                # ran no tool has not done the work it was entered for.
                return _final if (_final and (tools_used or read_only)) else None

        # Repair shim (Fix #2): recover a tool call the model dumped into text
        # instead of the structured tool_calls field. No-op for good models.
        if not tool_calls and content:
            tool_calls = _repair_toolcalls(content)

        # If (still) no tool calls → this is the final response.
        if not tool_calls:
            # Loud-fallback guard (Fix #4): this loop is only entered for tasks
            # that need file/command tools. If the model produced a final text
            # response WITHOUT ever executing a tool, it only chatted — return
            # None so the caller's fallback ladder tries the next model, rather
            # than passing off a plausible-looking no-op as success.
            if tools_used == 0 and not read_only:
                _trace.emit("loop.end", reason="final_text_without_any_tool_call",
                            iteration=iteration, tools_used=0, content=content)
                return None
            _trace.emit("loop.end", reason="final_text", iteration=iteration,
                        tools_used=tools_used,
                        elapsed_s=round(time.monotonic() - started, 1))
            return content or thinking or None

        # Add assistant message with tool calls to conversation
        messages.append(msg)

        # Execute each tool call and add results
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            tool_args = func.get("arguments", {})

            signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
            if signature == last_signature:
                repeats += 1
                # Say WHY it did not run. A silent skip produces the same call
                # again; naming the repeat is what changes the next turn.
                tool_result = (
                    f"You already called {tool_name} with exactly these arguments "
                    f"and got the result above. Do not repeat it. Either use a "
                    f"different tool, or call `{FINISH_TOOL}` with your answer now."
                )
                _trace.emit("tool.repeat_refused", iteration=iteration,
                            tool=tool_name, repeats=repeats + 1)
                if repeats >= 2:
                    messages.append({"role": "tool", "content": tool_result})
                    _trace.emit("loop.end", reason="repeated_identical_call",
                                iteration=iteration, tool=tool_name,
                                tools_used=tools_used)
                    return (
                        f"Stopped: the model repeated {tool_name} identically "
                        f"{repeats + 1} times without making progress."
                    ) if tools_used else None
            else:
                last_signature = signature
                repeats = 0
                _trace.emit("tool.call", iteration=iteration, tool=tool_name,
                            args=tool_args)
                _tt0 = time.monotonic()
                if read_only and tool_name not in READ_ONLY_TOOLS:
                    tool_result = (f"Refused: {tool_name} is not available — this "
                                   f"draft is read-only. Answer from what you have read.")
                else:
                    tool_result = execute_tool(tool_name, tool_args, project_root)
                    if read_log is not None and tool_name in READ_ONLY_TOOLS:
                        _a = tool_args if isinstance(tool_args, dict) else {}
                        read_log.append(f"{tool_name}({_a.get('path') or _a.get('pattern') or ''})")
                _trace.emit("tool.result", iteration=iteration, tool=tool_name,
                            ms=int((time.monotonic() - _tt0) * 1000),
                            result_len=len(tool_result or ""),
                            result=tool_result)
            tools_used += 1

            messages.append({
                "role": "tool",
                # The task travels WITH the result. A window that evicts from the
                # front cannot take the question away if the question is also at
                # the back.
                "content": tool_result + _budget.restate(prompt),
            })

    # Hit max iterations — return whatever we have
    _trace.emit("loop.end", reason="max_iterations", iterations=_MAX_ITERATIONS,
                tools_used=tools_used,
                elapsed_s=round(time.monotonic() - started, 1))
    return "Agent reached maximum iterations. Partial work may have been done."
