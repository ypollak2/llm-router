"""The one permission decision function.

`Policy.decide(tool, args)` is called by code, before every tool call, and by
nothing else. The model never sees it, cannot configure it, and no text the model
or a tool result contains is ever read to decide a permission (a tool result is
data; see `tools.wrap_result`). Every decision, allow or deny, is appended to the
decision log with the rule that produced it.

Rules, in the order they are applied:

  kill        env LLM_ROUTER_TOOLLAYER=off or the KILL file: everything is denied
  tool        only the seven offered tools exist
  path        realpath must be inside the workspace; no `~`; symlinks resolve first
  secret      deny-listed names/dirs are refused for read, search, list, edit, write
  protected   files the verifier names are frozen
  write       `write` creates new files; overwrite needs overwrite:true
  budget      bytes-written budget
  bash        sandbox proven, no substitution/heredoc, allowlisted programs,
              flag rules, every path-like argument contained
"""
from __future__ import annotations

import fnmatch
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_router.toolkit import sandbox

TOOL_NAMES = ("read", "search", "list", "edit", "write", "bash", "finish")

# ── secret deny-list ─────────────────────────────────────────────────────────

SECRET_NAME_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials", "credentials.json", "credentials.yml", "credentials.yaml",
    "credentials.toml", "credentials.ini",
    ".npmrc", ".netrc", ".pgpass", ".pypirc", ".htpasswd", "secrets.json", "secrets.yml",
    "secrets.yaml", "service-account*.json", "*.secret", "*.secrets",
)
SECRET_DIR_NAMES = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".git"})


_PUBLIC_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_secret_name(name: str) -> bool:
    base = os.path.basename(name.rstrip("/")).lower()
    if base.startswith(".env.") and base.endswith(_PUBLIC_ENV_SUFFIXES):
        return False                      # `.env.example` documents variables; it holds no values
    return any(fnmatch.fnmatch(base, g.lower()) for g in SECRET_NAME_GLOBS)


def is_secret_relpath(rel: str) -> bool:
    parts = Path(rel).parts
    if any(p in SECRET_DIR_NAMES for p in parts):
        return True
    return bool(parts) and any(is_secret_name(p) for p in parts)


# ── test-harness control files ───────────────────────────────────────────────
# Anything pytest (or the interpreter it runs in) loads BEFORE or AROUND a test and that can
# therefore change what "passed" means: a conftest.py `pytest_runtest_makereport` hookwrapper
# flips every failure to a pass without touching a test file. The model may not write these
# (policy) and any change to them forces used=False (verifier), at any depth in the tree.
CONTROL_FILE_NAMES = frozenset({
    "conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py",
    ".coveragerc", "sitecustomize.py", "usercustomize.py", "entry_points.txt", "pytest_plugins.py",
})
CONTROL_SUFFIXES = (".pth", ".egg-link")
CONTROL_DIR_SUFFIXES = (".dist-info", ".egg-info")      # importlib.metadata finds pytest11 entry points here


def is_control_relpath(rel: str) -> bool:
    parts = Path(rel).parts
    if not parts:
        return False
    name = parts[-1].lower()
    return (name in CONTROL_FILE_NAMES or name.endswith(CONTROL_SUFFIXES)
            or any(p.lower().endswith(CONTROL_DIR_SUFFIXES) for p in parts))


# ── bash allowlist ───────────────────────────────────────────────────────────

ALLOWED_PROGRAMS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "du", "find", "grep", "rg",
    "sort", "uniq", "cut", "diff", "tree", "which", "echo", "pwd",
    "python", "python3", "pytest", "ruff",
})
_PY_MODULES = frozenset({"pytest", "ruff", "unittest", "py_compile", "compileall", "json.tool"})
_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
                           "-fprintf", "-fls"})
_FORBIDDEN_SUBSTRINGS = ("$(", "`", "${", "<(", ">(", "<<", "\n", "\r", "\x00")
_MAX_COMMAND_CHARS = 2000


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule: str
    reason: str = ""


def _deny(rule: str, reason: str) -> Decision:
    return Decision(False, rule, reason)


_ALLOW = Decision(True, "ok", "")


def resolve_in_workspace(raw: object, root: Path) -> tuple[Path | None, Decision | None]:
    """Realpath of `raw` if it is inside `root`, else (None, a deny Decision)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, _deny("path", "path is missing or not a string")
    if "\x00" in raw:
        return None, _deny("path", "path contains a NUL byte")
    if raw.strip().startswith("~"):
        return None, _deny("path", "home-directory paths are not available")
    candidate = Path(raw) if os.path.isabs(raw) else root / raw
    real = Path(os.path.realpath(candidate))
    if real != root and root not in real.parents:
        return None, _deny("path_outside_workspace", f"'{raw}' resolves outside the workspace")
    return real, None


@dataclass
class Policy:
    root: Path
    bash_allowed: bool = False
    bash_reason: str = "bash not enabled"
    protected: frozenset[Path] = frozenset()
    max_bytes_written: int = 1_000_000
    bytes_written: int = 0
    log_path: Path | None = None
    decisions: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(os.path.realpath(self.root))
        self.protected = frozenset(Path(os.path.realpath(p)) for p in self.protected)

    # ── logging ──────────────────────────────────────────────────────────────
    def _record(self, tool: str, args: object, d: Decision) -> Decision:
        from llm_router.persist_redaction import persist_redact
        try:
            shown = json.dumps(args, default=str, sort_keys=True)[:300]
        except (TypeError, ValueError):
            shown = repr(args)[:300]
        row = {"ts": round(time.time(), 3), "kind": "decision", "tool": tool,
               "allow": d.allow, "rule": d.rule, "reason": d.reason[:200],
               "args": persist_redact(shown)}
        self.decisions.append(row)
        if self.log_path is not None:
            from llm_router.toolkit.result import append_private_jsonl
            append_private_jsonl(self.log_path, row)
        return d

    def denied(self) -> list[dict]:
        return [r for r in self.decisions if not r["allow"]]

    # ── the decision ─────────────────────────────────────────────────────────
    def decide(self, tool: str, args: object) -> Decision:
        return self._record(str(tool), args, self._decide(tool, args))

    def _decide(self, tool: str, args: object) -> Decision:
        why = sandbox.kill_switch_reason()
        if why:
            return _deny("kill", f"tool layer is switched off ({why})")
        if tool not in TOOL_NAMES:
            return _deny("tool", f"'{tool}' is not an available tool")
        if not isinstance(args, dict):
            return _deny("args", "arguments must be an object")
        if tool == "finish":
            return _ALLOW
        if tool == "bash":
            return self._decide_bash(args)
        if tool in ("read", "list", "search"):
            return self._decide_read(tool, args)
        return self._decide_write(tool, args)

    def _decide_read(self, tool: str, args: dict) -> Decision:
        raw = args.get("path")
        if tool in ("list", "search") and (raw is None or raw == ""):
            raw = "."
        real, bad = resolve_in_workspace(raw, self.root)
        if bad is not None:
            return bad
        rel = os.path.relpath(real, self.root)
        if rel != "." and is_secret_relpath(rel):
            return _deny("secret", "that path is not available")
        if tool == "search":
            pat = args.get("pattern")
            if not isinstance(pat, str) or not pat:
                return _deny("args", "search needs a non-empty pattern")
        return _ALLOW

    def _decide_write(self, tool: str, args: dict) -> Decision:
        real, bad = resolve_in_workspace(args.get("path"), self.root)
        if bad is not None:
            return bad
        rel = os.path.relpath(real, self.root)
        if rel == ".":
            return _deny("path", "cannot write to the workspace root itself")
        if is_secret_relpath(rel):
            return _deny("secret", "that path is not available")
        if real in self.protected or any(p in real.parents for p in self.protected):
            return _deny("protected", f"{rel} is frozen: the verifier runs it")
        if is_control_relpath(rel):
            return _deny("protected", f"{rel} configures the test harness and is not editable")
        size = 0
        if tool == "write":
            content = args.get("content")
            if not isinstance(content, str):
                return _deny("args", "write needs string content")
            size = len(content.encode("utf-8"))
            if real.exists() and args.get("overwrite") is not True:
                return _deny("write_exists", f"{rel} already exists; use edit, or pass overwrite:true")
            if real.is_dir():
                return _deny("path", f"{rel} is a directory")
        else:
            edits = args.get("edits")
            if not isinstance(edits, list) or not edits:
                return _deny("args", "edit needs a non-empty edits list")
            for e in edits:
                if not isinstance(e, dict) or not isinstance(e.get("old_string"), str) \
                        or not isinstance(e.get("new_string"), str):
                    return _deny("args", "each edit needs string old_string and new_string")
                size += len(e["new_string"].encode("utf-8"))
        if self.bytes_written + size > self.max_bytes_written:
            return _deny("budget_bytes", f"bytes-written budget ({self.max_bytes_written}) exhausted")
        return _ALLOW

    # ── bash ─────────────────────────────────────────────────────────────────
    def _decide_bash(self, args: dict) -> Decision:
        if not self.bash_allowed:
            return _deny("bash_off", f"bash is disabled: {self.bash_reason}")
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return _deny("args", "bash needs a command")
        if len(cmd) > _MAX_COMMAND_CHARS:
            return _deny("bash_shape", "command is too long")
        for bad in _FORBIDDEN_SUBSTRINGS:
            if bad in cmd:
                return _deny("bash_shape", "command substitution, heredocs and multi-line commands are refused")
        from llm_router.toolkit.tools import parse_command_line
        parsed = parse_command_line(cmd)
        if isinstance(parsed, str):
            return _deny("bash_parse", parsed.split("\n")[0][:200])
        for _op, segments in parsed:
            for seg in segments:
                d = self._check_segment(seg["argv"])
                if not d.allow:
                    return d
        return _ALLOW

    def _check_segment(self, argv: list[str]) -> Decision:
        if not argv:
            return _deny("bash_shape", "empty command")
        prog = argv[0]
        if "/" in prog or prog.startswith(("~", ".")) or "=" in prog:
            return _deny("program_not_allowed", f"'{prog}': programs are run by bare name only")
        if prog not in ALLOWED_PROGRAMS:
            return _deny("program_not_allowed", f"'{prog}' is not in the allowlist "
                         f"({', '.join(sorted(ALLOWED_PROGRAMS))})")
        rest = argv[1:]
        base = "python" if prog.startswith("python") else prog
        if base == "python":
            d = self._check_python(rest)
            if not d.allow:
                return d
        elif prog == "find" and any(a in _FIND_ACTIONS for a in rest):
            return _deny("bash_flags", "find with an action flag can delete or run commands")
        elif prog == "ruff":
            sub = next((a for a in rest if not a.startswith("-")), "")
            if sub not in ("check", "format", "--version", ""):
                return _deny("bash_flags", f"ruff {sub} is not allowed")
            if any(a in ("--fix", "--unsafe-fixes", "--fix-only", "--add-noqa") for a in rest):
                return _deny("bash_flags", "ruff fixes write files; use the edit tool")
            if sub == "format" and not any(a in ("--check", "--diff") for a in rest):
                return _deny("bash_flags", "ruff format writes files; use --check or --diff")
        elif prog in ("sort",) and any(a in ("-o", "--output") or a.startswith("--output=") for a in rest):
            return _deny("bash_flags", "sort -o writes files")
        return self._check_paths(rest)

    def _check_python(self, rest: list[str]) -> Decision:
        # Interpreter options only: stop at -m <module> or the script name, so
        # `python -m pytest -c cfg.ini` is not mistaken for inline code.
        for a in rest:
            if a == "-m" or not a.startswith("-") or a == "--":
                break
            if a == "-" or (not a.startswith("--") and "c" in a[1:] and not a.startswith("-W")):
                return _deny("inline_code", "python inline code (-c) is not allowed; write a file and run it")
        if "-m" in rest:
            i = rest.index("-m")
            mod = rest[i + 1] if i + 1 < len(rest) else ""
            if mod not in _PY_MODULES:
                return _deny("python_module", f"python -m {mod or '?'} is not allowed "
                             f"(allowed: {', '.join(sorted(_PY_MODULES))})")
            return _ALLOW
        script = next((a for a in rest if not a.startswith("-")), None)
        if script is None:
            return _deny("python_shape", "python needs -m <module> or a script inside the workspace")
        real, bad = resolve_in_workspace(script, self.root)
        if bad is not None:
            return bad
        if not real.is_file():
            return _deny("python_shape", f"{script} is not a file in the workspace")
        return _ALLOW

    def _check_paths(self, rest: list[str]) -> Decision:
        for tok in rest:
            value = tok
            if tok.startswith("-"):
                if "=" not in tok:
                    continue
                value = tok.split("=", 1)[1]
            if not value:
                continue
            if is_secret_name(value) or any(p in SECRET_DIR_NAMES for p in Path(value).parts):
                return _deny("secret", "that path is not available")
            pathlike = value.startswith(("/", "~", ".")) or "/" in value or ".." in value
            if pathlike:
                real, bad = resolve_in_workspace(value, self.root)
                if bad is not None:
                    return bad
                rel = os.path.relpath(real, self.root)
                if rel != "." and is_secret_relpath(rel):
                    return _deny("secret", "that path is not available")
        return _ALLOW

    def note_written(self, n: int) -> None:
        self.bytes_written += n
