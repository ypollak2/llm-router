"""Technical-operation classifier and ``technical_ops.jsonl`` writer (PLAN v16 P2.1, R-LOC-6).

OFFLINE CORE: nothing imports this module yet. Live condensing, the re-read
signal, proxy SERVE_TECHOPS and Explore are later tasks.

``classify_tool_call`` follows the GE1 list: Grep, Glob, LS, Read, NotebookRead,
TodoRead and Bash commands from a read-only list. It fails closed: anything with
a redirection, command substitution, ``||``/``&``, an env prefix, a parse error,
an unlisted command in a ``;``/``&&``/``|`` chain, or a write form returns None.

The write check here is minimal and local. It should converge with
``llm_router.write_policy`` once #452 (P2.9-b) is merged; it is not imported.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from llm_router import capped_log
from llm_router.paths import state_path

__all__ = ["OpClass", "SERVED_BY", "classify_tool_call", "record_op",
           "technical_ops_path", "read_ops"]


class OpClass(str, Enum):
    SEARCH = "search"
    LIST = "list"
    READ = "read"
    GIT_READ = "git_read"
    FORMAT_CHECK = "format_check"


_TOOL_CLASS = {
    "Grep": OpClass.SEARCH, "Glob": OpClass.LIST, "LS": OpClass.LIST,
    "Read": OpClass.READ, "NotebookRead": OpClass.READ, "TodoRead": OpClass.READ,
}
_CMD_CLASS = {
    "rg": OpClass.SEARCH, "grep": OpClass.SEARCH, "find": OpClass.SEARCH,
    "ls": OpClass.LIST, "tree": OpClass.LIST,
    "cat": OpClass.READ, "head": OpClass.READ, "tail": OpClass.READ, "wc": OpClass.READ,
    "git": OpClass.GIT_READ,
}
_GIT_SUBS = {"status", "log", "diff", "show", "branch", "blame"}
_FIND_WRITE = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
_RG_EXEC = {"--pre", "--hostname-bin"}
_BRANCH_OK = {"-a", "-r", "-v", "-vv", "--list", "-l", "--all", "--remotes", "--verbose",
              "--show-current", "--merged", "--no-merged", "--contains", "--no-color", "--color"}
_FMT_WRITE = {"--fix", "-w", "--write"}
_SEPARATORS = {";", "&&", "|"}
_OPERATOR_CHARS = set("();&|<>")
_ID = re.compile(r"[A-Za-z0-9_.:-]+")
_REASON = re.compile(r"[a-z0-9_]+")


def _formatter(argv: list[str]) -> bool:
    """A listed formatter invoked in check-only mode. Bare names only: a path
    (``./black``, ``/tmp/x/black``) runs whatever that file is."""
    name = argv[0]
    if "/" in name or "\\" in name:
        return False
    rest = argv[1:]
    if name in ("black", "isort", "prettier"):
        pass
    elif name == "ruff" and rest[:1] == ["format"]:
        pass
    elif name == "cargo" and rest[:1] == ["fmt"]:
        pass
    else:
        return False
    return "--check" in rest and not any(a.split("=", 1)[0] in _FMT_WRITE for a in rest)


def _git_ok(argv: list[str]) -> bool:
    rest = argv[1:]
    if rest[:1] == ["-C"] and len(rest) >= 3:
        rest = rest[2:]
    if not rest or rest[0] not in _GIT_SUBS:
        return False
    sub, args = rest[0], rest[1:]
    for a in args:
        option = a.split("=", 1)[0]
        if option.startswith("--") and len(option) >= 5 and any(flag.startswith(option) for flag in ("--ext-diff", "--textconv")):
            return False
        if a == "--output" or a.startswith("--output=") or a == "-o" or a.startswith("-o="):
            return False
    if sub == "branch":
        listing = "--list" in args or "-l" in args
        for a in args:
            if a == "--":
                continue
            if a.startswith("-"):
                if a.split("=", 1)[0] not in _BRANCH_OK:
                    return False
            elif not listing:
                return False
    return True


def _segment_class(argv: list[str]) -> OpClass | None:
    if not argv:
        return None
    if _formatter(argv):
        return OpClass.FORMAT_CHECK
    name = argv[0]
    cls = _CMD_CLASS.get(name)
    if cls is None:
        return None
    args = argv[1:]
    if name == "find" and any(a.split("=", 1)[0] in _FIND_WRITE for a in args):
        return None
    if name == "tree":
        for a in args:
            option = a.split("=", 1)[0]
            if option.startswith("--") and len(option) >= 3 and "--output".startswith(option):
                return None
            if a.startswith("-") and not a.startswith("--") and "o" in a:
                return None
    if name == "rg" and any(a.split("=", 1)[0] in _RG_EXEC for a in args):
        return None
    if name == "git" and not _git_ok(argv):
        return None
    return cls


def _classify_bash(command: str) -> OpClass | None:
    if not command.strip() or "\n" in command or "\r" in command:
        return None
    # Non-posix lexing keeps quotes on each token, so an operator the shell sees
    # (unquoted) is told apart from one that is only an argument ('|', ";").
    lex = shlex.shlex(command, posix=False, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    segments: list[list[str]] = [[]]
    for raw in lex:
        if raw in _SEPARATORS:
            segments.append([])
            continue
        if raw and all(c in _OPERATOR_CHARS for c in raw):
            return None  # redirection, ||, &, subshell, any other operator
        words = shlex.split(raw)
        if len(words) != 1:
            return None
        tok = words[0]
        if tok and all(c in _OPERATOR_CHARS for c in tok):
            # A quoted operator is an argument to the command before it, so the
            # words after it are that command's arguments too: fail closed.
            return None
        segments[-1].append(tok)
    # Quoted text may legally hold < > ; unquoted ones were operator tokens above.
    if "$(" in command or "`" in command:
        return None
    first: OpClass | None = None
    for i, seg in enumerate(segments):
        if seg and "=" in seg[0] and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
            return None
        cls = _segment_class(seg)
        if cls is None:
            return None
        if i == 0:
            first = cls
    return first


def classify_tool_call(tool: str, tool_input: object) -> OpClass | None:
    """Class of a read-only technical operation, or None. Never raises."""
    try:
        if tool in _TOOL_CLASS:
            return _TOOL_CLASS[tool]
        if tool != "Bash" or not isinstance(tool_input, dict):
            return None
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        return _classify_bash(command)
    except Exception:  # noqa: BLE001 - fail closed
        return None


# --------------------------------------------------------------------------- log

SERVED_BY = ("local_model", "local_filter", "passthrough")
_FILENAME = "technical_ops.jsonl"
_DEFAULT_MAX_BYTES = 5_000_000


def technical_ops_path() -> Path:
    return state_path(_FILENAME)


def _max_bytes() -> int:
    try:
        v = int(os.environ.get("LLM_ROUTER_TECH_OPS_MAX_BYTES", ""))
        return v if v > 0 else _DEFAULT_MAX_BYTES
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _token(value: object, limit: int, pattern: re.Pattern = _ID) -> str | None:
    """The value when it is a short identifier (``pattern``, at most ``limit``
    chars), else None. A path, a command or free text is never stored."""
    if not isinstance(value, str) or len(value) > limit or not pattern.fullmatch(value):
        return None
    return value


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def record_op(*, session_id: object, task_id: object, tool: str,
              op_class: OpClass | str | None, bytes_in: int, bytes_out: int,
              served_by: str, skip_reason: str | None = None, ms: int = 0,
              model: str | None = None, re_read: bool = False,
              session_kind: str | None = None, path: Path | None = None) -> bool:
    """Append one row to ``technical_ops.jsonl``; True when written. Never raises.

    Stores no text: ids, the model and the session kind are kept only when they
    are identifiers (``[A-Za-z0-9_.:-]``), the skip reason only when it is a
    lower-case code (``[a-z0-9_]``); anything else is stored as null. ``model``,
    ``re_read`` and ``session_kind`` are the fields ``offload_share`` reads.
    """
    try:
        if served_by not in SERVED_BY:
            return False
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": _token(session_id, 128),
            "task_id": _token(task_id, 128),
            "tool": _token(tool, 64),
            "op_class": op_class.value if isinstance(op_class, OpClass) else _token(op_class, 32, _REASON),
            "bytes_in": _int(bytes_in),
            "bytes_out": _int(bytes_out),
            "served_by": served_by,
            "skip_reason": _token(skip_reason, 64, _REASON),
            "ms": _int(ms),
            "model": _token(model, 128),
            "re_read": re_read is True,
            "session_kind": _token(session_kind, 32),
        }
        capped_log.append(path or technical_ops_path(),
                          (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"),
                          _max_bytes())
        return True
    except Exception:  # noqa: BLE001
        return False


def read_ops(path: Path | None = None) -> list[dict]:
    return capped_log.read_dicts(path or technical_ops_path())
