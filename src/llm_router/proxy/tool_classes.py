"""Tool classes for the action census (GE1): what kind of work a step's tool results came from.

A proxy continuation answers the results of the tool calls in the assistant message before
it. ``prev_tool_class`` on the ledger row names the class of those calls:

``technical_op``  only reads: Grep, Glob, LS, Read, NotebookRead, TodoRead, or a Bash command
                  every part of which is on the read-only list below
``edit``          Edit, Write, MultiEdit, NotebookEdit
``exec``          any other Bash command (it may write, run code or reach the network)
``agent``         Task / Agent (a sub-agent launch)
``web``           WebFetch, WebSearch
``other``         everything else (MCP tools, AskUserQuestion, ...)

Read-only Bash: ``git status|log|diff|show|branch|blame`` (``git branch`` only with read-only
flags), ``ls``, ``cat``, ``head``, ``tail``, ``wc``, ``find`` (no ``-exec``/``-delete``/
``-fprint``), ``rg``, ``grep``, ``tree`` (no ``-o``), formatters run with ``--check``, plus
``cd`` and ``pwd``, which only navigate. Parts joined by ``&&``, ``||``, ``;``, ``|`` or a
newline must ALL be read-only. Any output redirection other than to ``/dev/null`` or ``2>&1``,
command substitution, process substitution or a background ``&`` makes the command ``exec``.
The list is conservative on purpose: a false ``technical_op`` would let a local model take a
step that changes the user's files; a false ``exec`` only leaves a step with Claude.

The Bash command is classified in memory and never stored: the ledger keeps tool NAMES and
this class only.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Iterable

TECHNICAL_OP = "technical_op"
EDIT = "edit"
EXEC = "exec"
AGENT = "agent"
WEB = "web"
OTHER = "other"
CLASSES = (TECHNICAL_OP, EDIT, EXEC, AGENT, WEB, OTHER)

#: When the results answer tools of several classes, the step takes the first class in this
#: order that any of them has. ``technical_op`` comes last: a step is a technical op only when
#: EVERY tool it answers is one.
PRECEDENCE = (EDIT, EXEC, AGENT, WEB, OTHER, TECHNICAL_OP)

_BY_NAME = {
    **dict.fromkeys(("grep", "glob", "ls", "read", "notebookread", "todoread"), TECHNICAL_OP),
    **dict.fromkeys(("edit", "write", "multiedit", "notebookedit"), EDIT),
    **dict.fromkeys(("task", "agent"), AGENT),
    **dict.fromkeys(("webfetch", "websearch"), WEB),
}

_PLAIN_READERS = frozenset({"ls", "cat", "head", "tail", "wc", "rg", "grep", "cd", "pwd"})
_GIT_READ = frozenset({"status", "log", "diff", "show", "branch", "blame"})
_GIT_BRANCH_READ_FLAGS = frozenset({"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes",
                                    "--show-current", "--verbose", "--no-color", "--color"})
_FIND_WRITES = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
                          "-fprintf", "-fls"})
_FORMATTERS = frozenset({"black", "ruff", "isort", "prettier", "rustfmt", "cargo", "gofmt"})

# Harmless redirections, removed before the "any other redirection" test.
_SAFE_REDIRECT = re.compile(r"(?:\d?>>?|&>>?)\s*/dev/null|\d?>&\d")
_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def is_bash(name: Any) -> bool:
    return isinstance(name, str) and name.lower() == "bash"


def _segment_is_read_only(segment: str) -> bool:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        return False
    while words and _ENV_ASSIGN.match(words[0]):
        words = words[1:]
    if not words:
        return False
    cmd, args = words[0].rsplit("/", 1)[-1], words[1:]
    if cmd in _PLAIN_READERS:
        return True
    if cmd == "find":
        return not any(a in _FIND_WRITES for a in args)
    if cmd == "tree":
        return not any(a.startswith("-o") for a in args)   # -o FILE writes the listing
    if cmd == "git":
        return _git_is_read_only(args)
    if cmd in _FORMATTERS:
        return _formatter_check_only(cmd, args)
    return False


def _git_is_read_only(args: list[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):   # global options before the subcommand
        if args[i] == "-C":
            i += 1                                       # skip its path
        elif args[i] not in ("--no-pager",) and not args[i].startswith("--git-dir="):
            return False
        i += 1
    if i >= len(args) or args[i] not in _GIT_READ:
        return False
    sub, rest = args[i], args[i + 1:]
    if any(a == "--output" or a.startswith("--output=") for a in rest):
        return False
    if sub == "branch":
        return all(a in _GIT_BRANCH_READ_FLAGS for a in rest)
    return True


def _formatter_check_only(cmd: str, args: list[str]) -> bool:
    if not any(a in ("--check", "--check-only") for a in args):
        return False
    if cmd == "ruff":
        return bool(args) and args[0] == "format"
    if cmd == "cargo":
        return bool(args) and args[0] == "fmt"
    return True


def classify_bash(command: Any) -> str:
    """``technical_op`` when every part of ``command`` only reads, else ``exec``."""
    if not isinstance(command, str) or not command.strip():
        return EXEC
    text = _SAFE_REDIRECT.sub(" ", command).strip()
    if any(tok in text for tok in (">", "`", "$(", "<(")):
        return EXEC
    if re.search(r"(?<![&|])&(?![&>])", text):           # background job
        return EXEC
    parts = _SPLIT.split(text)
    if any(not p.strip() for p in parts):
        return EXEC
    return TECHNICAL_OP if all(_segment_is_read_only(p) for p in parts) else EXEC


def classify_tool(name: Any, tool_input: Any = None) -> str:
    """The class of one tool call. Bash needs its command; without one it is ``exec``."""
    if is_bash(name):
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
        return classify_bash(cmd)
    return _BY_NAME.get(name.lower(), OTHER) if isinstance(name, str) else OTHER


def step_tool_class(calls: Iterable[tuple[Any, Any]]) -> str | None:
    """The class of a step that answers ``calls`` (``(name, input)`` pairs), or ``None`` when
    it answers none. Mixed classes resolve by :data:`PRECEDENCE`."""
    found = {classify_tool(name, inp) for name, inp in calls}
    if not found:
        return None
    return next(c for c in PRECEDENCE if c in found)
