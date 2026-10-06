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
"""
from __future__ import annotations

import ast
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
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
    changed = changed_paths(ws.baseline, ws.root)
    if not changed:
        raise _Stop(NOT_APPLICABLE, "empty_patch")
    tampered = V.harness_tampering(ws.baseline, ws.root)
    if tampered:
        raise _Stop(FAIL, "harness_tampered")
    if V.weakened_tests(ws.baseline, ws.root):
        raise _Stop(FAIL, "tests_weakened")

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
