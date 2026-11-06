"""Model-free, opt-in batch edits and a bounded argv-only check.

Requires a trusted repository and trusted PATH executables; checks are unsandboxed.
Concurrent modification by another process of the same user is out of scope:
that user can already write the repository. Post-write path checks still reject
relocation outside cwd. A descendant calling setsid() can escape group cleanup;
a sandbox PR follows.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import uuid
import signal
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from llm_router.edit import EditInstruction, check_syntax
from llm_router.paths import llm_router_home, private_opener

_HANDOFF = "Nothing more will be applied by this tool for this attempt. Make the remaining changes yourself with your edit tools, then run the check."
_CHECK_TIMEOUT = 60
_BLOCK = re.compile(
    r"^(?:FILE:[ \t]*(?P<file>[^\n]+)\nFIND:[ \t]*\n```[^\n]*\n(?P<find>(?:(?!^```[ \t]*$).)*?)^```[ \t]*\n"
    r"REPLACE:[ \t]*\n```[^\n]*\n(?P<repl>(?:(?!^```[ \t]*$).)*?)^```[ \t]*$"
    r"|NEW FILE:[ \t]*(?P<new>[^\n]+)\n```[^\n]*\n(?P<body>(?:(?!^```[ \t]*$).)*?)^```[ \t]*$"
    r"|APPEND TO:[ \t]*(?P<app>[^\n]+)\n```[^\n]*\n(?P<more>(?:(?!^```[ \t]*$).)*?)^```[ \t]*$)",
    re.MULTILINE | re.DOTALL,
)



def _failopen(code: str, exc: BaseException) -> None:
    try:
        from llm_router import failopen

        failopen.record(code, exc)
    except Exception:  # noqa: BLE001 - accounting must not raise
        pass

def _parse(raw: str) -> tuple[list[tuple[str, EditInstruction]], list[str]]:
    text = raw.strip()
    if text.startswith(("[", "```")):
        if text.startswith("```"):
            fence = re.fullmatch(r"```(?:json)?[ \t]*\n(.*?)\n```", text, re.DOTALL)
            if not fence:
                return [], ["Malformed JSON fence or leftover content"]
            text = fence[1]
        try:
            def invalid_constant(value):
                raise ValueError(f"Invalid JSON constant: {value}")
            data = json.loads(text, parse_constant=invalid_constant)
        except ValueError as exc:
            return [], [f"Invalid JSON: {exc}"]
        if not isinstance(data, list):
            return [], ["Expected one JSON array"]
        edits = []
        for item in data:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) for key in ("file", "old_string", "new_string")
            ):
                return [], ["Every item requires string file/old_string/new_string fields"]
            edits.append(("replace", EditInstruction(item["file"], item["old_string"], item["new_string"])))
        return edits, []
    blocks = []
    end = 0
    reasons = []
    for match in _BLOCK.finditer(raw):
        if raw[end:match.start()].strip():
            reasons.append("Unparsed text or malformed edit block")
        end = match.end()
        if match["file"]:
            blocks.append(("block_replace", EditInstruction(match["file"].strip(), match["find"], match["repl"])))
        else:
            kind = "new" if match["new"] else "append"
            blocks.append((kind, EditInstruction((match["new"] or match["app"]).strip(), "", match["body"] if kind == "new" else match["more"])))
    if raw[end:].strip():
        reasons.append("Unparsed text or malformed edit block")
    return blocks, reasons


def _validate(root: Path, blocks: list[tuple[str, EditInstruction]]):
    """Compose in memory, matching edit.apply_edits' exact-once/final syntax gate."""
    originals: dict[Path, str | None] = {}
    current: dict[Path, str] = {}
    identities = {}
    reasons = []
    created: set[Path] = set()
    for kind, edit in blocks:
        try:
            lexical = root / edit.file
            target = lexical.resolve()
            if not target.is_relative_to(root) or target == root:
                raise ValueError("path resolves outside cwd or is cwd itself")
            if target not in originals:
                if target.exists() and not target.is_file():
                    raise ValueError("not a regular file")
                info = target.stat() if target.exists() else None
                identities[target] = (info.st_dev, info.st_ino) if info else None
                originals[target] = target.read_bytes().decode("utf-8") if target.exists() else None
                current[target] = originals[target] or ""
            text = current[target]
            if kind == "new":
                if originals[target] is not None or target in created or lexical.is_symlink():
                    raise ValueError("NEW FILE must not overwrite an existing file")
                current[target] = edit.new_string
                created.add(target)
            elif kind == "append":
                current[target] = text + ("" if not text or text.endswith("\n") else "\n") + edit.new_string
                created.add(target)
            else:
                if originals[target] is None and target not in created:
                    raise ValueError("no such file")
                old, new = edit.old_string, edit.new_string
                if kind == "block_replace" and text.count(old) != 1:
                    old, new = old.rstrip("\n"), new.rstrip("\n")
                if not old.strip():
                    raise ValueError("old_string is empty or whitespace-only")
                count = text.count(old)
                if count != 1:
                    raise ValueError(f"old_string appears {count} times (need exactly 1)")
                current[target] = text.replace(old, new, 1)
        except (OSError, ValueError, RuntimeError) as exc:
            reasons.append(f"{edit.file}: {exc}")
    for path, content in current.items():
        if content != originals[path]:
            error = check_syntax(str(path), content)
            if error:
                reasons.append(error)
    changed = {p: text for p, text in current.items() if text != originals[p]}
    if not changed and not reasons:
        reasons.append("the edits change nothing")
    return originals, changed, reasons, identities


def _before_write(path: Path) -> None:
    """Test seam for concurrent filesystem changes before a write."""


def _before_replace(path: Path) -> None:
    """Test seam after identity checking and immediately before replacement."""


def _write_batch(root, originals, changed, identities) -> None:
    """Pin parent directories; retain all recovery copies if rollback fails."""
    staged, backups = {}, {}
    parents = {root: os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)}
    written, directories = [], []
    incomplete = False

    def parent_fd(path):
        _before_write(path)
        parent = path.parent
        resolved = parent.resolve(strict=True)
        if parent.is_symlink() or resolved != parent or not resolved.is_relative_to(root):
            raise OSError(f"{path}: parent changed or resolves outside cwd")
        fd = parents.get(parent)
        if fd is None:
            # Walk from the pinned root without following any symlink component.
            fd = os.dup(parents[root])
            try:
                for part in parent.relative_to(root).parts:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
            except BaseException:
                os.close(fd)
                raise
            parents[parent] = fd
        pinned, current = os.fstat(fd), parent.stat()
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"{path}: parent changed")
        return fd

    def temporary(path, content):
        fd = parent_fd(path)
        name = ".llm-apply-" + uuid.uuid4().hex
        handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content.encode("utf-8"))
                if identities[path] is not None:
                    os.fchmod(stream.fileno(), stat.S_IMODE(os.stat(path.name, dir_fd=fd).st_mode))
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise
        return path.parent / name

    try:
        for path, content in changed.items():
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                parent_fd(directory)
                os.mkdir(directory.name, dir_fd=parents[directory.parent])
                directories.append(directory)
            staged[path] = temporary(path, content)
            if originals[path] is not None:
                backups[path] = temporary(path, originals[path])
        for path, temporary_path in staged.items():
            fd = parent_fd(path)
            if identities[path] is None:
                os.link(temporary_path.name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
            else:
                info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != identities[path]:
                    raise OSError(f"{path}: file changed since validation")
                _before_replace(path)
                os.replace(temporary_path.name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
            written.append(path)
            try:
                resolved = path.resolve(strict=True)
                current = path.parent.stat()
                pinned = os.fstat(fd)
                inside = resolved.is_relative_to(root) and (
                    current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino)
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                raise OSError("path moved outside cwd during write")
    except BaseException as exc:
        failures = []
        for path in reversed(written):
            try:
                # Recovery must address the pinned directory even after relocation.
                fd = parents[path.parent]
                if originals[path] is None:
                    os.unlink(path.name, dir_fd=fd)
                else:
                    # Preserve the backup until *all* restorations have succeeded.
                    restore = ".llm-apply-" + uuid.uuid4().hex
                    os.link(backups[path].name, restore, src_dir_fd=fd, dst_dir_fd=fd)
                    try:
                        os.replace(restore, path.name, src_dir_fd=fd, dst_dir_fd=fd)
                    except BaseException:
                        # A successful replace consumed the link; only a failed one leaves it.
                        os.unlink(restore, dir_fd=fd)
                        raise
            except BaseException as failure:
                failures.append(f"{path}: {failure}")
        if failures:
            incomplete = True
            if not isinstance(exc, Exception):
                exc.add_note("ROLLBACK INCOMPLETE: " + "; ".join(failures)
                             + "; backups: " + ", ".join(map(str, backups.values())))
                raise
            raise OSError(f"{exc}; ROLLBACK INCOMPLETE: {'; '.join(failures)}; backups: "
                          + ", ".join(map(str, backups.values()))) from exc
        raise
    finally:
        for temporary_path in (*staged.values(), *(() if incomplete else backups.values())):
            try:
                os.unlink(temporary_path.name, dir_fd=parents[temporary_path.parent])
            except OSError as exc:
                # Cleanup must not hide a rollback failure or remove backups.
                _failopen("CHZ-FO-APPLY-TEMP-CLEANUP", exc)
        for fd in parents.values():
            os.close(fd)
        for directory in reversed(directories):
            try:
                directory.rmdir()
            except OSError as exc:
                _failopen("CHZ-FO-APPLY-STAGING-DIR", exc)


def _check_argv(command: str, root: Path | None = None) -> tuple[list[str] | None, str]:
    if not command:
        return None, "Check not run: no check supplied."
    if any(char in command for char in ";|&$`><\n\r\x00"):
        return None, "Check not run: shell metacharacters are not allowed."
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return None, f"Check not run: {exc}"
    if not argv:
        return None, "Check not run: no runner supplied."
    root = root or Path.cwd().resolve()
    token = argv[0]
    bare = {"pytest", "ruff", "mypy", "npm", "pnpm", "go", "cargo", "python", "python3"}
    local = {".venv/bin/python", "venv/bin/python", ".venv/bin/pytest", "venv/bin/pytest"}
    if token in bare:
        executable = shutil.which(token)
        if executable:
            executable = os.path.abspath(executable)
    elif token in local:
        resolved = (root / token).resolve()
        executable = str(resolved) if resolved.is_relative_to(root) and resolved.is_file() else None
    else:
        executable = None
    name = Path(token).name
    allowed = name in {"pytest", "ruff", "mypy"}
    allowed |= name in {"python", "python3"} and argv[1:3] == ["-m", "pytest"]
    allowed |= argv[:2] in [["npm", "test"], ["pnpm", "test"], ["go", "test"], ["cargo", "test"]]
    # Allowlist, not denylist: three review rounds each found new ways to make a
    # runner read config, plugins or files from outside the repo (-qc, @argfile,
    # --pyargs, -modfile, -gcflags). Only these switches and in-repo paths pass.
    if name in {"pytest", "python", "python3"}:
        lead, switches, valued = 0 if name == "pytest" else 2, {
            "-q", "-qq", "-v", "-vv", "-x", "-s", "-l", "--lf", "--ff", "--sw",
            "--tb=short", "--tb=line", "--tb=no", "--no-header"}, {"-k", "-m", "--maxfail"}
    elif name == "ruff":
        lead, switches, valued = 0, {"check", "format", "--check", "--diff", "--fix", "-q", "--quiet"}, set()
    elif name == "mypy":
        lead, switches, valued = 0, {"--strict", "--no-incremental"}, set()
    elif argv[:2] == ["go", "test"]:
        lead, switches, valued = 1, {"-v", "-short", "-race"}, {"-run", "-count"}
    elif argv[:2] == ["cargo", "test"]:
        lead, switches, valued = 1, {"-q", "--quiet"}, set()
    else:  # npm test / pnpm test: no extra arguments
        lead, switches, valued = 1, set(), set()

    def inside(arg):
        if arg.startswith(("-", "@", "~")) or os.path.isabs(arg) or ".." in Path(arg).parts:
            return False
        node = arg.split("::", 1)[0]  # pytest node ids: path::test
        if go_style and node in {"./...", "."}:
            return True
        target = (root / node).resolve()
        return target.is_relative_to(root) and target.exists()

    go_style = argv[:2] == ["go", "test"]
    lead_runner_is_pytest = name in {"pytest", "python", "python3"}
    rest = argv[1 + lead:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        option, _, value = arg.partition("=")
        harmless_group = (lead_runner_is_pytest and arg.startswith("-") and not arg.startswith("--")
                          and len(arg) > 1 and set(arg[1:]) <= set("qvxsl"))
        if arg in switches or harmless_group:
            index += 1
            continue
        if option in valued:
            text = value if value else (rest[index + 1] if index + 1 < len(rest) else None)
            if text is None or text.startswith(("-", "@")) or not all(
                    char.isalnum() or char in " _.:[]()|^$*+?-" for char in text):
                return None, f"Check not run: argument not allowed: {arg}"
            index += 1 if value else 2
            continue
        if not inside(arg):
            return None, f"Check not run: argument not allowed: {arg}"
        index += 1
    if not allowed or not executable:
        return None, "Check not run: command is not a known test/lint runner or trusted executable."
    argv[0] = executable
    return argv, ""


def _run_check(argv: list[str], root: Path) -> tuple[int, str]:
    # A temporary file avoids retaining unbounded runner output in memory.
    allowed_env = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR",
                   "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "CI", "NO_COLOR"}
    env = {k: v for k, v in os.environ.items() if k in allowed_env or k.startswith("LC_")}
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        timed_out = False
        try:
            process.wait(timeout=_CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
        survivors = False
        try:
            os.killpg(process.pid, 0)
            survivors = True
        except ProcessLookupError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 6000))
        tail = output.read().decode("utf-8", errors="replace")[-1500:]
    if timed_out:
        tail = (tail + f"\nCheck timed out after {_CHECK_TIMEOUT} s; process group killed.")[-1500:]
    if survivors:
        tail = (tail + "\nProcess group member still alive after parent exit or timeout; process group killed.")[-1500:]
    return (-9 if timed_out or survivors else process.returncode), tail


def llm_apply(edits: str, check: str = "", cwd: str = "") -> str:
    """Apply a complete batch without a model, then run an allowlisted check.

    Beta, off by default: set LLM_ROUTER_APPLY=on. Several edits and a check
    become one call after you have read the code. Accepts JSON old/new pairs or
    FILE/FIND/REPLACE, NEW FILE, APPEND TO fenced blocks. Every path must stay
    inside cwd (a git work tree). No local repair or shell execution.
    """
    started = time.monotonic()
    status = "REJECTED"
    n_blocks = n_files = 0
    check_ran = False
    check_rc = None
    details: list[str] = []
    enabled = os.environ.get("LLM_ROUTER_APPLY", "").strip().lower() in {"on", "1", "true", "yes"}
    ledger = None
    try:
        if enabled:
            try:
                home = llm_router_home()
                home.mkdir(parents=True, exist_ok=True)
                ledger = open(home / "apply_ledger.jsonl", "a", encoding="utf-8", opener=private_opener)
            except OSError as exc:
                raise OSError(f"ledger not writable: {exc}") from exc
        if not enabled:
            details.append("llm_apply is disabled; enable with LLM_ROUTER_APPLY=on.")
        else:
            root = Path(cwd or os.getcwd()).resolve(strict=True)
            repo = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                                  capture_output=True, text=True, timeout=5)
            if repo.returncode or repo.stdout.strip() != "true":
                details.append("cwd must be inside a git work tree.")
            else:
                blocks, reasons = _parse(edits)
                n_blocks = len(blocks)
                if not blocks:
                    reasons.append("no edit instructions to apply")
                originals, changed, validation, identities = _validate(root, blocks)
                reasons.extend(validation)
                if reasons:
                    details.extend(reasons)
                else:
                    _write_batch(root, originals, changed, identities)
                    n_files = len(changed)
                    status = "APPLIED_NOT_CHECKED"
                    for path, content in changed.items():
                        diff = list(difflib.ndiff((originals[path] or "").splitlines(keepends=True), content.splitlines(keepends=True)))
                        plus = sum(line.startswith("+ ") for line in diff)
                        minus = sum(line.startswith("- ") for line in diff)
                        details.append(f"{path.relative_to(root)}: +{plus}/-{minus} lines")
                    argv, reason = _check_argv(check, root)
                    if argv is None:
                        details.append(reason)
                    else:
                        try:
                            check_rc, tail = _run_check(argv, root)
                            check_ran = True
                            status = "APPLIED_AND_PASSED" if check_rc == 0 else "APPLIED_CHECK_FAILED"
                            details.append(f"Check rc={check_rc}\n{tail}")
                        except OSError as exc:
                            details.append(f"Check not run: {exc}")
    except Exception as exc:
        details.append(str(exc))
    finally:
        row = dict(ts=datetime.now(timezone.utc).isoformat(), status=status, n_blocks=n_blocks,
                   n_files=n_files, check_ran=check_ran, check_rc=check_rc,
                   duration_s=round(time.monotonic() - started, 6), source="program")
        if ledger is not None:
            try:
                with ledger:
                    ledger.write(json.dumps(row) + "\n")
            except OSError as exc:
                details.append(f"ledger: not written ({exc})")
    if not check_ran:
        details.append("Check rc=not run")
    if status in {"REJECTED", "APPLIED_CHECK_FAILED"}:
        details.append(_HANDOFF)
    return "\n".join([status, *details])
