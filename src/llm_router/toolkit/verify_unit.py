"""V1 verifier for one routed code unit: pick the tests that touch the change, then
apply the fail-to-pass gate. Library only; nothing here is wired into a hook, a worker
or a KPI (VERIFIER_PLAN.md, PR A).

    verify_unit(repo, patch) -> UnitResult

Rules (VERIFIER_PLAN.md sections 2-5 and the owner decisions of 2026-10-06):

* The patch is applied (`git apply --check`, then `git apply`) to a throwaway copy made by
  `sandbox.create_workspace`. The caller's tree is never written.
* Candidates are repo tests that import, or sit next to, the changed source files, capped
  at `max_candidates` files and by the wall-clock budget.
* `pass_f2p` (a test that fails on the baseline passes after the patch, no new failures,
  no tampering, no weakened tests) is the ONLY used-eligible status. `pass_p2p` is weak and
  never used-eligible.
* Tests the model added or edited count only if they fail when copied into the baseline,
  and they are never the sole evidence: without an f2p test among the repo's own unmodified
  tests the status is `pass_p2p` with the flag `f2p_model_tests_only`.
* Fail closed: sandbox unproven, kill switch, timeout, OSError, an apply conflict -> unavailable;
  a repo with no pytest runner (or nothing to verify) -> not_applicable. Never pass.
* The test output tail and the command are never part of the result.
* ELIGIBILITY RULE (coarse on purpose): pass_f2p needs every changed path to be a .py file outside
  test/control paths, or a NEW test file. Any changed, renamed or deleted non-.py file anywhere
  (README, JSON, fixtures, snapshots, configs) -> `unavailable`/`non_python_change` (never used,
  never discarded: the patch is not judged wrong, only unverifiable by this gate). Editing an
  existing test file -> at best `pass_p2p`/`edited_tests`. A non-test .py under a test dir -> `fail`.
  RECALL COST: a fix that also touches a README or a data file is never eligible.
* Non-ASCII paths, paths that change under NFKC, case-fold collisions -> `unavailable`/`suspicious_path`,
  checked on the raw patch before anything is applied. Any symlink -> `unavailable`/`symlink_in_patch`.
* A model-edited test file is also run on the baseline: a test that passed there and no longer
  passes or exists after is `fail`/`tests_disappeared`.

KNOWN LIMITS (not caught by any static f2p gate; measured by the hidden-test precision bar of
VERIFIER_PLAN section 6, pinned by the `known_limit_*` tests so a future fix flips them on purpose):
  1. Gutting an assertion helper that lives in a NON-test source module (src/pkg/check.py).
  2. Special-casing the test's input in source code (`if (a, b) == (1, 2): return 3`).
Both yield pass_f2p / used-eligible today.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from llm_router.toolkit import sandbox
from llm_router.toolkit import verify as V

PASS_F2P, PASS_P2P, FAIL = "pass_f2p", "pass_p2p", "fail"
UNAVAILABLE, NOT_APPLICABLE = "unavailable", "not_applicable"
STATUSES = (PASS_F2P, PASS_P2P, FAIL, UNAVAILABLE, NOT_APPLICABLE)

DEFAULT_BUDGET_S = 120.0
MAX_BUDGET_S = 300.0
MAX_CANDIDATES = 60
_SRC_ROOTS = ("src", "lib")
MAX_PATCH_BYTES = 2 * 1024 * 1024
_TEST_DIR_PARTS = frozenset({"tests", "test", "testing", "fixtures", "fixture", "testdata", "test_data",
                            "__snapshots__", "snapshots", "snapshot"})
_SYMLINK_MODE_RX = re.compile(r"^(?:(?:new file|deleted file|old|new) mode 120000|index \w+\.\.\w+ 120000)\b",
                              re.M)
_SAFE_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


@dataclass
class UnitResult:
    verify_status: str
    reason: str                      # a code, never free text from a test run
    n_candidates: int = 0
    n_f2p: int = 0
    ms: int = 0
    sandboxed: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def used_eligible(self) -> bool:
        """pass_f2p only. pass_p2p is recorded as weak and never counts (owner decision)."""
        return self.verify_status == PASS_F2P and self.sandboxed

    def as_dict(self) -> dict:
        return asdict(self)


class _Stop(Exception):
    def __init__(self, status: str, reason: str, *flags: str):
        self.status, self.reason, self.flags = status, reason, list(flags)


# ── runner detection ─────────────────────────────────────────────────────────


def _rglob_py_tests(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in sandbox._SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                if V._is_test_path(rel):
                    out.append(rel)
    return out


def detect_pytest(root: Path) -> bool:
    """True when the repo is a pytest repo: explicit pytest config, a conftest, or python tests."""
    def has(name: str, *needles: str) -> bool:
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return any(n in text for n in needles)

    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        return True
    if has("pyproject.toml", "[tool.pytest") or has("setup.cfg", "[tool:pytest]") or has("tox.ini", "[pytest]"):
        return True
    return bool(_rglob_py_tests(root))


# ── candidate selection ──────────────────────────────────────────────────────


def module_names(rel: str) -> set[str]:
    """Dotted names a changed source file can be imported as (src/pkg/core.py -> pkg.core, core)."""
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[0] in _SRC_ROOTS:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    names = {".".join(parts[i:]) for i in range(len(parts))}
    return {n for n in names if n}


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def select_candidates(root: Path, changed_src: list[str], exclude: set[str] | None = None,
                      cap: int = MAX_CANDIDATES) -> tuple[list[str], bool]:
    """Existing test files in `root` that import, are named for, or sit next to the changed files.

    Returns (candidates, was_capped). Order: importers, then test_<stem>.py, then same-directory
    tests; ties broken by path, so the selection is deterministic.
    """
    exclude = exclude or set()
    wanted: set[str] = set()
    stems = {Path(r).stem for r in changed_src if Path(r).stem != "__init__"}
    dirs = {str(Path(r).parent) for r in changed_src}
    for r in changed_src:
        wanted |= module_names(r)
    scored: dict[str, int] = {}
    for rel in _rglob_py_tests(root):
        if rel in exclude:
            continue
        if wanted & _imports(root / rel):
            scored[rel] = 0
        elif Path(rel).name in {f"test_{s}.py" for s in stems} | {f"{s}_test.py" for s in stems}:
            scored[rel] = 1
        elif (str(Path(rel).parent) in dirs
              or (Path(rel).parent.name in ("tests", "test") and str(Path(rel).parent.parent) in dirs)):
            scored[rel] = 2
    ranked = sorted(scored, key=lambda r: (scored[r], r))
    return ranked[:cap], len(ranked) > cap


# ── workspace helpers ────────────────────────────────────────────────────────


def apply_patch(root: Path, patch: str) -> None:
    """`git apply --check`, then `git apply`, inside the throwaway copy. Raises _Stop on conflict."""
    pf = root.parent / "unit.patch"
    pf.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8")
    for extra in (["--check"], []):
        try:
            proc = subprocess.run(["git", "apply", *extra, str(pf)], cwd=str(root), env=_SAFE_ENV,
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise _Stop(UNAVAILABLE, "os_error") from None
        if proc.returncode != 0:
            raise _Stop(UNAVAILABLE, "apply_conflict")


def changed_paths(baseline: Path, root: Path) -> list[str]:
    now, was = set(V._walk_rel(root)), set(V._walk_rel(baseline))
    return sorted(r for r in now | was
                  if r not in now or r not in was or (baseline / r).read_bytes() != (root / r).read_bytes())


def has_symlink(root: Path) -> bool:
    """Any symlink under root (never follows one)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if any(os.path.islink(os.path.join(dirpath, n)) for n in dirnames + filenames):
            return True
    return False


def _is_test_named(rel: str) -> bool:
    name = Path(rel).name
    return name.startswith("test_") or name.endswith("_test.py")


def support_code_changes(changed: list[str]) -> list[str]:
    """A .py under a test dir that is not itself a test file (helpers, fixtures-as-code): it can
    change what an unmodified test asserts, so it is never eligible."""
    return [f"{r}: test support code changed" for r in changed
            if r.endswith(".py") and not _is_test_named(r) and _TEST_DIR_PARTS & set(Path(r).parts[:-1])]


def non_python_changes(changed: list[str]) -> list[str]:
    """Every changed, renamed or deleted path that is not a .py file. A test can read any of them
    (by a computed name, a glob, a listdir), so the rule is path-based, not a guess at what a test opens."""
    return [f"{r}: non-python file changed" for r in changed if not r.endswith(".py")]


def suspicious_paths(paths: list[str], existing: set[str] | None = None) -> list[str]:
    """Non-ASCII paths (lookalike dirs), paths that change under NFKC, and case-fold collisions."""
    out = []
    folded: dict[str, str] = {}
    for r in sorted(existing or ()):
        folded.setdefault(unicodedata.normalize("NFKC", r).casefold(), r)
    for r in paths:
        if not r.isascii() or unicodedata.normalize("NFKC", r) != r:
            out.append(r)
        elif folded.get(r.casefold(), r) != r:
            out.append(r)
    return out


_HEADER_RX = re.compile(r"^(?:diff --git |--- |\+\+\+ |rename (?:from|to) |copy (?:from|to) )(.*)$", re.M)


def patch_has_suspicious_header(patch: str) -> bool:
    """Checked on the raw patch text, before anything is applied or read. git quotes odd paths
    ("\\321\\..."), so a quote or backslash in a header counts too."""
    for m in _HEADER_RX.finditer(patch):
        line = m.group(0)
        if not line.isascii() or '"' in line or "\\" in line:
            return True
    return False


def _pytest_command(files: list[str]) -> str:
    return "pytest -q " + " ".join(shlex.quote(f) for f in files)


class _Clock:
    def __init__(self, budget_s: float):
        self.t0, self.budget = time.monotonic(), budget_s

    def left(self) -> float:
        return self.budget - (time.monotonic() - self.t0)

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


def _run(clock: _Clock, command: str, cwd: Path, tmp: Path, python_dir: str | None, tag: str) -> V.VerifyRun:
    left = clock.left()
    if left <= 1:
        raise _Stop(UNAVAILABLE, "timeout")
    try:
        run = V.run_command(command, cwd, tmp, python_dir=python_dir, timeout_s=left, tag=tag)
    except OSError:
        raise _Stop(UNAVAILABLE, "os_error") from None
    if run.timed_out:
        raise _Stop(UNAVAILABLE, "timeout")
    return run


# ── the gate ─────────────────────────────────────────────────────────────────


def verify_unit(repo: str | os.PathLike, patch: str, *, budget_s: float = DEFAULT_BUDGET_S,
                max_candidates: int = MAX_CANDIDATES, python_dir: str | None = None) -> UnitResult:
    """Judge `patch` (a unified diff against `repo`) with the repo's own tests. Never raises."""
    clock = _Clock(min(max(float(budget_s), 1.0), MAX_BUDGET_S))
    res = UnitResult(NOT_APPLICABLE, "empty_patch")
    try:
        _judge(res, clock, Path(repo), patch, max_candidates,
               python_dir or os.path.dirname(os.path.abspath(sys.executable)))
    except _Stop as stop:
        res.verify_status, res.reason = stop.status, stop.reason
        res.flags += stop.flags
    except Exception:                           # fail closed on anything unforeseen
        res.verify_status, res.reason = UNAVAILABLE, "internal_error"
    res.ms = clock.ms()
    return res


def _judge(res: UnitResult, clock: _Clock, repo: Path, patch: str, cap: int, python_dir: str) -> None:
    if not patch or not patch.strip():
        raise _Stop(NOT_APPLICABLE, "empty_patch")
    if len(patch.encode("utf-8", "replace")) > MAX_PATCH_BYTES:
        raise _Stop(UNAVAILABLE, "patch_too_large")
    if patch_has_suspicious_header(patch):
        raise _Stop(UNAVAILABLE, "suspicious_path")
    if _SYMLINK_MODE_RX.search(patch):
        raise _Stop(UNAVAILABLE, "symlink_in_patch")
    if not repo.is_dir():
        raise _Stop(UNAVAILABLE, "workspace_error")
    if not detect_pytest(repo):
        raise _Stop(NOT_APPLICABLE, "no_runner")
    if sandbox.kill_switch_reason():
        raise _Stop(UNAVAILABLE, "kill_switch")
    status = sandbox.prove_sandbox()
    if not status.proven:
        raise _Stop(UNAVAILABLE, "sandbox_unproven")
    try:
        ws = sandbox.create_workspace(repo)
    except OSError:
        raise _Stop(UNAVAILABLE, "workspace_error") from None
    try:
        _judge_in(res, clock, ws, patch, cap, python_dir)
    finally:
        ws.cleanup()


def _judge_in(res: UnitResult, clock: _Clock, ws: "sandbox.Workspace", patch: str, cap: int,
              python_dir: str) -> None:
    apply_patch(ws.root, patch)
    if has_symlink(ws.root):                    # before anything reads the copy
        raise _Stop(UNAVAILABLE, "symlink_in_patch")
    if clock.left() <= 1:
        raise _Stop(UNAVAILABLE, "timeout")
    changed = changed_paths(ws.baseline, ws.root)
    if not changed:
        raise _Stop(NOT_APPLICABLE, "empty_patch")
    tampered = V.harness_tampering(ws.baseline, ws.root)
    if tampered:
        raise _Stop(FAIL, "harness_tampered")
    if V.weakened_tests(ws.baseline, ws.root):
        raise _Stop(FAIL, "tests_weakened")
    if suspicious_paths(changed, set(V._walk_rel(ws.baseline))):
        raise _Stop(UNAVAILABLE, "suspicious_path")
    if support_code_changes(changed):
        raise _Stop(FAIL, "test_support_changed")
    if non_python_changes(changed):
        raise _Stop(UNAVAILABLE, "non_python_change")

    changed_py = [r for r in changed if r.endswith(".py")]
    model_tests = [r for r in changed_py if V._is_test_path(r) and (ws.root / r).is_file()]
    changed_src = [r for r in changed_py if not V._is_test_path(r)]
    if not changed_src and not model_tests:
        raise _Stop(NOT_APPLICABLE, "no_python_change")
    if model_tests:
        res.flags.append("model_tests_added")

    candidates, capped = select_candidates(ws.baseline, changed_src, exclude=set(changed), cap=cap)
    res.n_candidates = len(candidates)
    if capped:
        res.flags.append("candidates_capped")
    if not candidates:
        if model_tests:
            res.flags.append("model_tests_only")
        raise _Stop(NOT_APPLICABLE, "no_candidates")

    tmp = Path(tempfile.mkdtemp(prefix="vu-", dir=str(ws.tmp)))
    cmd = _pytest_command(candidates)
    base = _run(clock, cmd, ws.baseline, tmp, python_dir, "base")
    after = _run(clock, cmd, ws.root, tmp, python_dir, "after")
    verdict = V.verify(cmd, ws, python_dir=python_dir, timeout_s=max(clock.left(), 1),
                       baseline_run=base, after_run=after)
    if not verdict.ran:
        raise _Stop(UNAVAILABLE, "sandbox_unproven" if "sandbox not proven" in verdict.reason else "os_error")
    res.sandboxed = verdict.sandboxed

    # the repo's own (unmodified) tests: fail on the baseline, pass after
    f2p_own = sorted(base.failed & after.passed) if base.junit and after.junit else []

    if not verdict.ok:
        _classify_not_ok(res, verdict, base, after)

    edited = [r for r in model_tests if (ws.baseline / r).is_file()]
    if edited:
        ecmd = _pytest_command(edited)
        ebase = _run(clock, ecmd, ws.baseline, tmp, python_dir, "ebase")
        eafter = _run(clock, ecmd, ws.root, tmp, python_dir, "eafter")
        if not (ebase.junit and eafter.junit):
            raise _Stop(UNAVAILABLE, "no_junit")
        if ebase.passed - eafter.passed:
            raise _Stop(FAIL, "tests_disappeared")

    f2p_model: list[str] = []
    if model_tests:
        mb = ws.parent / "model-baseline"
        shutil.copytree(ws.baseline, mb, symlinks=True)
        for r in model_tests:
            (mb / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ws.root / r, mb / r)
        mcmd = _pytest_command(model_tests)
        mbase = _run(clock, mcmd, mb, tmp, python_dir, "mbase")
        mafter = _run(clock, mcmd, ws.root, tmp, python_dir, "mafter")
        if mafter.rc != 0 or not mafter.junit:
            raise _Stop(FAIL if mafter.junit and mafter.failed else UNAVAILABLE,
                        "model_tests_fail" if mafter.junit and mafter.failed else "no_junit")
        f2p_model = sorted(mbase.failed & mafter.passed) if mbase.junit else []
        if len(f2p_model) < len(mafter.passed):
            res.flags.append("model_test_passes_on_baseline")

    res.n_f2p = len(f2p_own) + len(f2p_model)
    if f2p_own and edited:      # eligibility needs every changed path to be non-test .py or a NEW test
        res.flags.append("edited_existing_test")
        res.verify_status, res.reason = PASS_P2P, "edited_tests"
        return
    if f2p_own:
        res.verify_status, res.reason = PASS_F2P, "f2p"
        return
    if f2p_model:
        res.flags.append("f2p_model_tests_only")
    res.verify_status, res.reason = PASS_P2P, "no_f2p"


def _classify_not_ok(res: UnitResult, v: "V.Verdict", base: V.VerifyRun, after: V.VerifyRun) -> None:
    if v.new_failures:
        raise _Stop(FAIL, "new_failures")
    if v.disappeared:
        raise _Stop(FAIL, "tests_disappeared")
    if after.rc != 0:
        if not after.junit:
            raise _Stop(UNAVAILABLE, "no_junit")
        if after.failed - base.failed:
            raise _Stop(FAIL, "tests_fail")
        if after.failed:
            raise _Stop(UNAVAILABLE, "baseline_failing", "preexisting_failures")
        raise _Stop(UNAVAILABLE, "tests_error")
    if not after.junit:
        raise _Stop(UNAVAILABLE, "no_junit")
    raise _Stop(UNAVAILABLE, "no_test_passed")
