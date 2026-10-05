"""Verifier type A (V1): run the project's tests before and after.

`used` is decided here and nowhere else. The rule, from PLAN section 5.1/5.2 and
the owner's decision (tests only, V1):

    used = the supplied command passes after (exit 0, at least one test passed)
           AND no test that passed before is missing or failing now
           AND no test file was deleted or weakened
           AND the patch is non-empty

A model's own "done", a syntax check, a reviewer's opinion or a human keeping the
draft never set it. The command runs under the same OS sandbox as the model's
shell; if the sandbox cannot be proven the verifier does not run (and `used` is
unknown, not false).
"""
from __future__ import annotations

import ast
import os
import shlex
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from llm_router.toolkit import sandbox

_TEST_FILE_PARTS = ("tests", "test")


@dataclass
class VerifyRun:
    rc: int | None
    timed_out: bool = False
    passed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    n_skipped: int = 0
    junit: bool = False
    tail: str = ""


@dataclass
class Verdict:
    ran: bool
    level: str = "V1"
    command: str = ""
    ok: bool = False
    reason: str = ""
    sandboxed: bool = False
    baseline_rc: int | None = None
    after_rc: int | None = None
    baseline_passed: int | None = None
    after_passed: int | None = None
    new_failures: list[str] = field(default_factory=list)
    disappeared: list[str] = field(default_factory=list)
    weakened: list[str] = field(default_factory=list)
    tail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def is_pytest_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    if Path(argv[0]).name in ("pytest", "py.test"):
        return True
    return Path(argv[0]).name.startswith("python") and argv[1:3] == ["-m", "pytest"]


def parse_junit(path: Path) -> tuple[set[str], set[str], int] | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    passed, failed, skipped = set(), set(), 0
    for tc in root.iter("testcase"):
        tid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        if tc.find("skipped") is not None:
            skipped += 1
        elif tc.find("failure") is not None or tc.find("error") is not None:
            failed.add(tid)
        else:
            passed.add(tid)
    return passed, failed, skipped


def run_command(command: str, cwd: Path, tmp: Path, *, python_dir: str | None,
                timeout_s: float, tag: str) -> VerifyRun:
    """Run the supplied command once, sandboxed. Never uses a shell."""
    argv = shlex.split(command)
    junit_path = tmp / f"junit-{tag}.xml"
    use_junit = is_pytest_argv(argv)
    if use_junit:
        argv = argv + [f"--junit-xml={junit_path}", "-p", "no:cacheprovider"]
    launcher = sandbox.SandboxLauncher(cwd, tmp)
    proc = subprocess.Popen(launcher.wrap(argv), cwd=str(cwd), env=launcher.env(python_dir),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", **launcher.popen_extra())
    sandbox.register(proc)
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        sandbox.kill_tree(proc)
        out, _ = proc.communicate()
    finally:
        sandbox.unregister(proc)
        sandbox.kill_tree(proc)          # anything the test run left behind
    run = VerifyRun(rc=None if timed_out else proc.returncode, timed_out=timed_out, tail=(out or "")[-1500:])
    if use_junit:
        parsed = parse_junit(junit_path)
        if parsed is not None:
            run.passed, run.failed, run.n_skipped = parsed
            run.junit = True
    return run


# ── test deletion / weakening ────────────────────────────────────────────────


def _is_test_path(rel: str) -> bool:
    p = Path(rel)
    return p.suffix == ".py" and (p.name.startswith("test_") or p.name.endswith("_test.py")
                                  or any(part in _TEST_FILE_PARTS for part in p.parts[:-1]))


def _test_stats(text: str) -> tuple[int, int, int] | None:
    """(test functions, assert statements, skip/xfail markers) or None if unparseable."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    funcs = asserts = marks = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            funcs += 1
        elif isinstance(node, ast.Assert):
            asserts += 1
        elif isinstance(node, ast.Attribute) and node.attr in ("skip", "skipif", "xfail"):
            marks += 1
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("skip", "xfail", "importorskip"):
            marks += 1
    return funcs, asserts, marks


def weakened_tests(baseline: Path, root: Path, frozen: set[str] | None = None) -> list[str]:
    """Test files deleted, frozen-but-modified, or with fewer tests/asserts or more skips."""
    frozen = frozen or set()
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(baseline):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), baseline)
            if not _is_test_path(rel) and rel not in frozen:
                continue
            before, after = baseline / rel, root / rel
            if not after.exists():
                out.append(f"{rel}: deleted")
                continue
            b, a = before.read_bytes(), after.read_bytes()
            if b == a:
                continue
            if rel in frozen:
                out.append(f"{rel}: modified but frozen by the verifier")
                continue
            sb, sa = _test_stats(b.decode("utf-8", "replace")), _test_stats(a.decode("utf-8", "replace"))
            if sb is None:
                continue
            if sa is None:
                out.append(f"{rel}: no longer parses")
            elif sa[0] < sb[0] or sa[1] < sb[1]:
                out.append(f"{rel}: fewer tests/asserts ({sb[0]}/{sb[1]} -> {sa[0]}/{sa[1]})")
            elif sa[2] > sb[2]:
                out.append(f"{rel}: more skip/xfail markers ({sb[2]} -> {sa[2]})")
    return out


def frozen_paths(command: str, root: Path) -> set[str]:
    """Workspace files/dirs the verify command names (the verifier's own tests)."""
    out: set[str] = set()
    try:
        argv = shlex.split(command)
    except ValueError:
        return out
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        cand = tok.split("::", 1)[0]
        p = root / cand
        if p.exists() and (p.resolve() == root.resolve() or root.resolve() in p.resolve().parents):
            out.add(os.path.relpath(p, root))
    out.discard(".")
    return out


# ── the verdict ──────────────────────────────────────────────────────────────


def verify(command: str, workspace: "sandbox.Workspace", *, python_dir: str | None = None,
           timeout_s: float = 300, patch_nonempty: bool = True,
           baseline_run: VerifyRun | None = None) -> Verdict:
    """Type A verification. `baseline_run` may be passed when it was taken before
    the model ran; otherwise it is taken now on the pristine baseline copy."""
    v = Verdict(ran=False, command=command)
    status = sandbox.prove_sandbox()
    if not status.proven:
        v.reason = f"verifier did not run: sandbox not proven ({status.reason})"
        return v
    v.sandboxed = True
    tmp = Path(tempfile.mkdtemp(prefix="vf-", dir=str(workspace.tmp)))
    try:
        base = baseline_run or run_command(command, workspace.baseline, tmp, python_dir=python_dir,
                                           timeout_s=timeout_s, tag="before")
        after = run_command(command, workspace.root, tmp, python_dir=python_dir,
                            timeout_s=timeout_s, tag="after")
    except OSError as exc:
        v.reason = f"verifier could not start: {exc}"
        return v
    v.ran = True
    v.baseline_rc, v.after_rc = base.rc, after.rc
    v.tail = after.tail
    if base.junit:
        v.baseline_passed = len(base.passed)
    if after.junit:
        v.after_passed = len(after.passed)
    frozen = frozen_paths(command, workspace.root)
    v.weakened = weakened_tests(workspace.baseline, workspace.root, frozen)
    if base.junit and after.junit:
        v.new_failures = sorted(after.failed - base.failed)
        v.disappeared = sorted(base.passed - after.passed - after.failed)
    if after.timed_out:
        v.reason = "verify command timed out after the change"
    elif after.rc != 0:
        v.reason = f"verify command failed after the change (exit {after.rc})"
    elif not after.junit and is_pytest_argv(shlex.split(command)):
        v.reason = "no junit result produced; cannot confirm a test passed"
    elif after.junit and not after.passed:
        v.reason = "no test passed (nothing ran, or everything was skipped)"
    elif v.new_failures:
        v.reason = f"{len(v.new_failures)} new failing test(s)"
    elif v.disappeared:
        v.reason = f"{len(v.disappeared)} previously passing test(s) no longer run"
    elif v.weakened:
        v.reason = "test files deleted or weakened: " + "; ".join(v.weakened[:3])
    elif not patch_nonempty:
        v.reason = "the patch is empty; tests passing proves nothing about the task"
    else:
        v.ok = True
        v.reason = "supplied test run passed with no new failures"
    return v
