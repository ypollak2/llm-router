"""Opt-in batch edits with bounded local repair and a bounded argv-only check.

Requires trusted PATH executables; check containment is configurable.
Concurrent modification by another process of the same user is out of scope:
that user can already write the repository. Post-write path checks still reject
relocation outside cwd. A descendant calling setsid() can escape group cleanup;
sandbox restrictions remain inherited by those descendants, and on macOS they are
killed after the check by process-tree walk plus an env-marker sweep (best effort).
Status APPLIED_PASSED_UNSANDBOXED / APPLIED_PASSED_NEW_FILES / APPLIED_PASSED_OUTSIDE_EDITS mark a pass that ran
without a sandbox, or after which the check left executable/config files (conftest.py,
Makefile, +x, ...) or modified existing files beyond the edit set (or deleted, or a scan that could not finish); only APPLIED_AND_PASSED is a clean pass.
"""
from __future__ import annotations

import difflib
import hashlib
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

from llm_router import check_sandbox
from llm_router.edit import EditInstruction, check_syntax
from llm_router.paths import llm_router_home, private_opener

# A check that passed. The two suffixed statuses are not a clean pass: see the "[llm_apply check]" line.
_PASSED = {"APPLIED_AND_PASSED", "APPLIED_PASSED_UNSANDBOXED", "APPLIED_PASSED_NEW_FILES",
           "APPLIED_PASSED_OUTSIDE_EDITS"}
_HANDOFF = "Nothing more will be applied by this tool for this attempt. Make the remaining changes yourself with your edit tools, then run the check."
_CHECK_TIMEOUT = 60
_OUTPUT_CAP = 16 * 1024 * 1024  # bytes of check output kept on disk before the group is killed
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
    consumed = set()  # staged temps that os.replace moved into place
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
                stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))
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
                consumed.add(path)
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
        leftovers = [t for path, t in staged.items() if path not in consumed]
        for temporary_path in (*leftovers, *(() if incomplete else backups.values())):
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


_CONFIG_NAMES = {"conftest.py", "makefile", "rakefile", "package.json", ".envrc", "setup.py", "setup.cfg",
                 "pytest.ini", "tox.ini", "pyproject.toml", "sitecustomize.py", "usercustomize.py", ".npmrc",
                 "tasks.json", ".pre-commit-config.yaml"}
# Persistence scan: a pure-Python stat-only snapshot of ROOT before and after the check. NO git
# (or any other process) runs on the host: git status runs clean filters, fsmonitor and
# attribute drivers named by the repo, i.e. repository-controlled code, outside the sandbox.
# Budgets bound one snapshot. 200,000 entries caps memory (one small tuple per path) and covers
# a repo with a populated node_modules; 5 s caps wall clock. Measured 2026-11-25 on this repo's
# worktree (n=3,219 entries, stat only, warm cache, macOS arm64, 2 runs): ~150,000 entries/s. The
# 200,000 entries / 5 s figures are EXTRAPOLATED from that one small measurement, not measured at
# that size, and content hashing (below) makes a big tree slower; over budget is reported, not hidden. ROOT/.git is
# skipped: it is read-only inside the sandbox.
_SCAN_MAX_ENTRIES = 200_000
_SCAN_SECONDS = 5.0
_CACHE_DIRS = {".pytest_cache", ".mypy_cache", ".ruff_cache"}  # rewritten by every run; no code in them
_HASH_MAX = 1024 * 1024  # content-hash files up to 1 MB, plus any config-named, executable or .pyc file


def _is_config(rel: Path) -> bool:
    name = rel.name.lower()
    posix = rel.as_posix().lower()
    return (name in _CONFIG_NAMES or name.endswith(".pth") or posix.startswith(".github/workflows/")
            or name.endswith(".pyc") or posix in {".cargo/config", ".cargo/config.toml"})


def _digest(path):
    """sha256 of a regular file; O_NONBLOCK|O_NOFOLLOW so a swapped-in FIFO or link cannot hang or redirect us."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return "unreadable"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "not-regular"
        h = hashlib.sha256()
        while chunk := os.read(fd, 1 << 20):
            h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)


def _snapshot(root: Path):
    """{path: (size, mtime_ns, mode)} of every non-directory under root (no symlink followed,
    ROOT/.git skipped), or a str reason when the walk exceeds its entry or time budget."""
    state = {}
    deadline = time.monotonic() + _SCAN_SECONDS
    pending = [str(root)]
    git_dir = str(root / ".git")
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if len(state) >= _SCAN_MAX_ENTRIES:
                        return f"more than {_SCAN_MAX_ENTRIES} entries"
                    if time.monotonic() > deadline:
                        return f"over the {_SCAN_SECONDS} s walk budget"
                    if entry.path == git_dir:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    else:
                        digest = None
                        if stat.S_ISREG(info.st_mode) and (
                                info.st_size <= _HASH_MAX or info.st_mode & 0o111
                                or _is_config(Path(entry.name)) or entry.name.endswith(".pyc")):
                            digest = _digest(entry.path)
                        state[Path(entry.path)] = (info.st_size, info.st_mtime_ns, info.st_mode, digest)
        except OSError as exc:
            return f"unreadable: {exc}"
    return state


def _persistence(before, after, edited, root: Path):
    """(changed, flagged, outside): paths created/changed beyond the edit set; the executable/config
    subset a later unsandboxed command could run; and pre-existing files the check modified."""
    edited = {Path(p) for p in edited}
    changed = sorted(p for p in after if p not in edited and before.get(p) != after[p]
                     and not (_CACHE_DIRS & set(p.relative_to(root).parts[:-1])))
    flagged, outside = [], []
    for path in changed:
        mode = after[path][2]
        rel = path.relative_to(root)
        if _is_config(rel) or (stat.S_ISREG(mode) and mode & 0o111):
            flagged.append(str(rel))
        if path in before:
            outside.append(str(rel))
    for path in sorted(before.keys() - after.keys() - edited):
        if not (_CACHE_DIRS & set(path.relative_to(root).parts[:-1])):
            outside.append(f"{path.relative_to(root)} (deleted)")
    return [str(p.relative_to(root)) for p in changed], flagged, outside


class _Scan:
    """Snapshot before a check, diff after: what the check left in ROOT beyond the edit set."""

    def __init__(self, root: Path, edited):
        self.root, self.edited = root, edited
        self.before = _snapshot(root)

    def finish(self) -> tuple[list[str], dict]:
        """(report lines, info); never raises. info: checked, flagged, outside, unverified."""
        if isinstance(self.before, str):  # could not take the baseline: say so, status unchanged
            return ([f"WARNING: persistence check unavailable (tree too large or unreadable: {self.before}): "
                     "files the check left behind were NOT checked. Result is UNVERIFIED."],
                    dict(checked=False, flagged=[], outside=[], unverified=True))
        after = _snapshot(self.root)
        if isinstance(after, str):  # baseline was fine, so a tree that now escapes the budget is suspect
            return ([f"WARNING: persistence check could not finish after the check ({after}); the check "
                     "may have inflated the tree to hide files. Result is UNVERIFIED."],
                    dict(checked=False, flagged=[], outside=[], unverified=True))
        changed, flagged, outside = _persistence(self.before, after, self.edited, self.root)
        lines = []
        if changed:
            lines.append("changed beyond the edit set: " + ", ".join(changed[:20])
                         + (f" (+{len(changed) - 20} more)" if len(changed) > 20 else ""))
        if flagged:
            lines.append("WARNING: the check created or changed executable/config files that a later "
                         "unsandboxed command may run: " + ", ".join(flagged[:20]))
        if outside:
            lines.append("WARNING: the check modified existing files outside the edit set: "
                         + ", ".join(outside[:20]))
        return lines, dict(checked=True, flagged=flagged, outside=outside, unverified=False)


def _run_check(argv: list[str], root: Path, *, require_sandbox: bool = False) -> tuple[int, str, bool]:
    # Default "require": fail closed. "auto" (run unsandboxed when no backend) and
    # "off" are explicit owner opt-outs; the unsandboxed label is in the reply.
    mode = os.environ.get("LLM_ROUTER_APPLY_SANDBOX", "require").strip().lower()
    if mode not in {"auto", "require", "off"}:
        raise check_sandbox.SandboxUnavailable(f"invalid LLM_ROUTER_APPLY_SANDBOX: {mode}")
    sandboxed = False
    cleanup = None
    check_sandbox.set_marker(uuid.uuid4().hex)  # read by environment(), wrap() and _execute_check()
    reason = "disabled by LLM_ROUTER_APPLY_SANDBOX=off"
    if require_sandbox or mode != "off":
        try:
            argv, cleanup = check_sandbox.wrap(argv, root)
            sandboxed = True
        except check_sandbox.SandboxUnavailable as exc:
            if require_sandbox or mode == "require":
                check_sandbox.set_marker(None)
                raise
            reason = str(exc)
    label = (f"check: sandboxed ({check_sandbox.backend_name()})" if sandboxed
             else f"check: NOT sandboxed ({reason})")
    try:
        rc, tail = _execute_check(argv, root)
        return rc, label + "\n" + tail, sandboxed
    except OSError as exc:
        if sandboxed:
            raise check_sandbox.SandboxUnavailable(f"sandbox failed to start: {exc}") from exc
        raise
    finally:
        check_sandbox.set_marker(None)
        if cleanup is not None:
            cleanup()


def _execute_check(argv: list[str], root: Path) -> tuple[int, str]:
    # A temporary file avoids retaining unbounded runner output in memory.
    env = check_sandbox.environment()
    marker = check_sandbox.current_marker()
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True, env=env, close_fds=True,
                                   preexec_fn=check_sandbox.resource_limiter())
        timed_out = flooded = False
        deadline = time.monotonic() + _CHECK_TIMEOUT
        while True:
            try:
                process.wait(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if os.fstat(output.fileno()).st_size > _OUTPUT_CAP:
                    flooded = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
        # Before killpg: a still-live root lets the pgrep -P walk see its setsid() children.
        known = check_sandbox.descendants(process.pid)
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
        detached = check_sandbox.kill_survivors(known, marker)
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 6000))
        tail = output.read().decode("utf-8", errors="replace")[-1500:]
    if timed_out:
        tail = (tail + f"\nCheck timed out after {_CHECK_TIMEOUT} s; process group killed.")[-1500:]
    if flooded:
        tail = (tail + f"\nCheck output exceeded {_OUTPUT_CAP} bytes; process group killed.")[-1500:]
    if detached:
        tail = (tail + f"\nKilled {len(detached)} detached descendant process(es) after the check.")[-1500:]
    if survivors:
        tail = (tail + "\nProcess group member still alive after parent exit or timeout; process group killed.")[-1500:]
    return (-9 if timed_out or survivors or flooded else process.returncode), tail


def _repair_seconds() -> float:
    try:
        seconds = float(os.environ.get("LLM_ROUTER_APPLY_REPAIR_S", "60"))
    except ValueError:
        seconds = 60.0
    # This also bounds infinities and NaN.
    return max(10.0, min(120.0, seconds))


def _diff_summary(root, originals, changed):
    lines = []
    for path, content in changed.items():
        diff = list(difflib.ndiff((originals[path] or "").splitlines(keepends=True), content.splitlines(keepends=True)))
        plus = sum(line.startswith("+ ") for line in diff)
        minus = sum(line.startswith("- ") for line in diff)
        lines.append(f"{path.relative_to(root)}: +{plus}/-{minus} lines")
    return lines


def _local_repair(root, argv, files, task, details, repair):
    """One host-side edit generation round; every post-edit check requires containment."""
    if os.environ.get("LLM_ROUTER_APPLY_REPAIR", "on").strip().lower() in {"off", "0", "false", "no"}:
        repair["repair_reason"] = "disabled"
        return None
    if argv is None:
        repair["repair_reason"] = "no allowlisted check"
        return None
    from llm_router import zero_claude_edit

    model = zero_claude_edit.edit_model(allow_probe=False)
    if not model:
        repair["repair_reason"] = "no local model"
        return None
    repair["repair_model"] = model
    try:
        _, cleanup = check_sandbox.wrap(argv, root)
        cleanup()
    except check_sandbox.SandboxUnavailable as exc:
        repair["repair_reason"] = "sandbox unavailable"
        details.append(f"local repair: failed (sandbox unavailable: {exc})")
        return None

    started = time.monotonic()
    repair["repair_attempted"] = True
    snapshot = {}
    wrote = False
    reason = "no valid edits"
    try:
        for path in files:
            if not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError("repair file is no longer inside cwd or existing")
            snapshot[path] = path.read_bytes()
        contents = {str(path.relative_to(root)): content.decode("utf-8") for path, content in snapshot.items()}
        deadline = started + _repair_seconds()
        proposed, instructions, failure = zero_claude_edit.generate_edits(
            task, contents, model, deadline_s=deadline)
        if time.monotonic() > deadline:
            reason = "timed out"
        elif proposed is None or failure or not instructions:
            reason = "no valid edits"
        elif any(edit.file not in contents for edit in instructions):
            reason = "file outside repair batch"
        else:
            originals, changed, reasons, identities = _validate(root, [("replace", edit) for edit in instructions])
            if reasons:
                reason = "validation rejected"
            else:
                _write_batch(root, originals, changed, identities)
                wrote = True
                scan = _Scan(root, set(changed) | set(files))
                rc, tail, sandboxed = _run_check(argv, root, require_sandbox=True)
                scan_lines, _info = scan.finish()
                repair.update(repair_check_rc=rc, repair_sandboxed=sandboxed)
                details.append(f"Local repair check rc={rc}\n{tail}")
                details.extend(scan_lines)
                if rc == 0:
                    repair["repair_files"] = sorted(map(str, changed))
                    repair["repair_passed"] = True
                    repair["repair_reason"] = "passed"
                    details.extend(_diff_summary(root, originals, changed))
                    details.append(f"Edits came from local model {model}; Claude should review the diff.")
                    return len(changed), rc, sandboxed
                reason = "check still failed"
    except check_sandbox.SandboxUnavailable as exc:
        reason = "sandbox unavailable at re-run"
        details.append(f"local repair sandbox: {exc}")
    except Exception as exc:
        reason = "repair error"
        details.append(f"local repair error: {exc}")
    finally:
        if wrote and not repair["repair_passed"]:
            try:
                current, restore, identities = {}, {}, {}
                for path, content in snapshot.items():
                    if path.resolve() != path or not path.is_relative_to(root):
                        raise OSError(f"{path}: path moved before restore")
                    info = path.stat() if path.exists() else None
                    identities[path] = (info.st_dev, info.st_ino) if info else None
                    # Bytes: the failed check may have left content that is not UTF-8.
                    current[path] = path.read_bytes() if info else None
                    restore[path] = content
                _write_batch(root, current, restore, identities)
            except Exception as exc:
                details.append(f"ROLLBACK INCOMPLETE: {exc}")
        repair["repair_s"] = round(time.monotonic() - started, 6)
    repair["repair_reason"] = reason
    details.append(f"local repair: failed ({reason})")
    return None


def llm_apply(edits: str, check: str = "", cwd: str = "") -> str:
    """Apply a complete batch, check it, and optionally try bounded local repair.

    Beta, off by default: set LLM_ROUTER_APPLY=on. Several edits and a check
    become one call after you have read the code. Accepts JSON old/new pairs or
    FILE/FIND/REPLACE, NEW FILE, APPEND TO fenced blocks. Every path must stay
    inside cwd (a git work tree). Local text-only repair requires a sandboxed
    check and Claude review; disable with LLM_ROUTER_APPLY_REPAIR=off. No shell execution.
    """
    started = time.monotonic()
    status = "REJECTED"
    n_blocks = n_files = 0
    changed_files: set[str] = set()
    repair_trigger = None
    check_ran = False
    sandboxed = False
    check_rc = None
    details: list[str] = []
    check_report: dict = {}
    enabled = os.environ.get("LLM_ROUTER_APPLY", "").strip().lower() in {"on", "1", "true", "yes"}
    repair = dict(repair_attempted=False, repair_passed=False, repair_model=None,
                  repair_s=None, repair_reason=None, repair_check_rc=None, repair_sandboxed=None)
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
            repo = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                                   "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                                  capture_output=True, text=True, timeout=5,
                                  env=check_sandbox.environment())  # no inherited GIT_* variables
            if repo.returncode or repo.stdout.strip() != "true":
                details.append("cwd must be inside a git work tree.")
            else:
                argv = None
                blocks, reasons = _parse(edits)
                parse_failed = bool(reasons)
                n_blocks = len(blocks)
                if not blocks:
                    reasons.append("no edit instructions to apply")
                originals, changed, validation, identities = _validate(root, blocks)
                reasons.extend(validation)
                if reasons:
                    details.extend(reasons)
                    argv, _ = _check_argv(check, root)
                else:
                    _write_batch(root, originals, changed, identities)
                    changed_files.update(map(str, changed))
                    n_files = len(changed)
                    status = "APPLIED_NOT_CHECKED"
                    details.extend(_diff_summary(root, originals, changed))
                    argv, check_reason = _check_argv(check, root)
                    if argv is None:
                        details.append(check_reason)
                    else:
                        try:
                            scan = _Scan(root, changed)
                            check_rc, tail, sandboxed = _run_check(argv, root)
                            scan_lines, info = scan.finish()
                            check_report.update(sandboxed=sandboxed, **info)
                            check_ran = True
                            status = "APPLIED_CHECK_FAILED" if check_rc else (
                                "APPLIED_PASSED_UNSANDBOXED" if not sandboxed else
                                "APPLIED_PASSED_NEW_FILES" if check_report.get("flagged") else
                                "APPLIED_PASSED_OUTSIDE_EDITS" if check_report.get("outside") or check_report.get("unverified")
                                else "APPLIED_AND_PASSED")
                            label, _, output = tail.partition("\n")
                            details.append(label)
                            details.append(f"Check rc={check_rc}\n{output}")
                            details.extend(scan_lines)
                        except check_sandbox.SandboxUnavailable as exc:
                            details.append(f"Check not run: no usable sandbox ({exc}); "
                                           "set LLM_ROUTER_APPLY_SANDBOX=auto to allow unsandboxed checks")
                        except OSError as exc:
                            details.append(f"Check not run: {exc}")
                repair_files = None
                if status == "APPLIED_CHECK_FAILED":
                    repair_trigger = "B"
                    repair_files = list(changed)
                    task = "make this check pass with minimal edits\n" + "\n".join(details) + f"\nCheck command: {check}"
                elif validation and not parse_failed and blocks:
                    candidates = [(root / edit.file).resolve() for _, edit in blocks]
                    if all(path.is_relative_to(root) and path.is_file() for path in candidates):
                        repair_trigger = "A"
                        repair_files = list(dict.fromkeys(candidates))
                        # Validated fields only: never forward the raw request (extra keys, text outside blocks).
                        shown = "\n\n".join(f"{kind} FILE: {e.file}\nFIND:\n{e.old_string}\nREPLACE:\n{e.new_string}"
                                             for kind, e in blocks)
                        task = "apply the intent of these edits\n" + shown + "\nRejection reasons:\n" + "\n".join(reasons)
                if repair_files is not None:
                    repaired = _local_repair(root, argv, repair_files, task, details, repair)
                    if repaired is not None:
                        repaired_files, check_rc, sandboxed = repaired
                        n_files = max(n_files, repaired_files)
                        check_ran = True
                        status = "REPAIRED_BY_LOCAL_AND_PASSED"
    except Exception as exc:
        details.append(str(exc))
    finally:
        changed_files.update(repair.pop("repair_files", []))
        program_replaced = (n_blocks + 1 if status in _PASSED or (
            status == "REPAIRED_BY_LOCAL_AND_PASSED" and repair_trigger == "B") else 0)
        local_replaced = int(status == "REPAIRED_BY_LOCAL_AND_PASSED")
        credit = dict(program=program_replaced, local=local_replaced, files=sorted(changed_files))
        row = dict(program_replaced=program_replaced, local_replaced=local_replaced,
                   files=credit["files"], repair_trigger=repair_trigger, ts=datetime.now(timezone.utc).isoformat(), status=status, n_blocks=n_blocks,
                   n_files=n_files, check_ran=check_ran, check_rc=check_rc, sandboxed=sandboxed,
                   duration_s=round(time.monotonic() - started, 6),
                   source="local" if repair["repair_passed"] else "program", **repair)
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
    if enabled:
        if check_report:
            details.insert(0, "[llm_apply check] " + json.dumps(dict(
                sandboxed=check_report.get("sandboxed"), persistence_checked=check_report.get("checked"),
                flagged_new_files=check_report.get("flagged", []), outside_edits=check_report.get("outside", []), unverified=check_report.get("unverified", False))))
        details.insert(0, "[llm_apply credit] " + json.dumps(credit))
    return "\n".join([status, *details])
