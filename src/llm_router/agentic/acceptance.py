"""Objective acceptance-check runners for MGEE milestones.

A milestone is DONE only when an objective, *executable* check passes — never on
the executing model's self-report (docs/agentic-router.md §4.2). Each factory
returns an ``AcceptanceCheck`` (``artifacts -> AcceptanceResult``) usable directly
as ``Milestone.acceptance``.

``reproducible()`` wraps any check to detect non-determinism (flaky): a flaky
failure is reported with ``deterministic=False`` so the engine re-runs it once
instead of escalating on noise.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from llm_router.agentic.ledger import AcceptanceCheck, AcceptanceResult


def canary_check(marker: str, field: str = "output") -> AcceptanceCheck:
    """Pass iff ``marker`` appears in ``artifacts[field]``."""
    def check(artifacts: dict[str, Any]) -> AcceptanceResult:
        text = str(artifacts.get(field, ""))
        ok = marker in text
        return AcceptanceResult(ok, "" if ok else f"canary {marker!r} not found in {field}")
    return check


def validator_check(
    fn: Callable[[dict[str, Any]], bool], desc: str = ""
) -> AcceptanceCheck:
    """Pass iff the pure predicate ``fn(artifacts)`` is truthy."""
    def check(artifacts: dict[str, Any]) -> AcceptanceResult:
        try:
            ok = bool(fn(artifacts))
        except Exception as exc:  # noqa: BLE001 — a broken validator fails closed, never hangs
            return AcceptanceResult(False, f"validator error: {exc}")
        return AcceptanceResult(ok, "" if ok else f"validator failed: {desc or fn}")
    return check


def _git(cwd: str | None, *args: str) -> str | None:
    """Run a git command, or return ``None`` if it could not run."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _repo_changes(cwd: str | None, paths: Sequence[str] = ()) -> str | None:
    """Everything that changed in ``cwd``, scoped to ``paths`` when given.

    ``None`` means UNKNOWN and every caller must treat it as a failure to verify.
    An empty string is a real answer — "nothing changed". ``None`` is the absence
    of one, and conflating them is how a check that could not run reports success.

    Two sources, because one is not enough and the gap is the common case:

    * ``git diff HEAD`` — modifications to TRACKED files.
    * ``git ls-files --others`` — NEW files, which ``git diff HEAD`` cannot see
      at all. Verified: ``git diff HEAD -- untracked.py`` prints nothing and
      exits 0. A milestone like "implement X" usually creates a file, so a
      diff-only check would have reported the repository unchanged for exactly
      the work it was meant to confirm — failing closed, but for the wrong
      reason, which trains people to disable it.

    Scoping to ``paths`` matters for soundness, not tidiness: against the whole
    tree, an unrelated dirty file elsewhere can satisfy a symbol assertion, and
    the check would pass on somebody else's work.
    """
    pathspec = ["--", *paths] if paths else []
    diff = _git(cwd, "diff", "HEAD", *pathspec)
    new_files = (
        _git(cwd, "ls-files", "--others", "--exclude-standard", *pathspec)
        if diff is not None
        else None
    )

    if diff is None or new_files is None:
        # Not a git repository (or git is unusable). Fall back to reading the
        # declared files off disk.
        #
        # This is weaker than a diff — it cannot tell "created" from "was
        # already there" — but it keeps the property that matters: the evidence
        # comes from the FILESYSTEM, never from the executor's own account. A
        # milestone run in a plain directory is a legitimate case (the bounded
        # operational path does exactly this), and refusing to verify it at all
        # would make the honest path unusable while the gameable one still
        # worked, which is how a safety check gets switched off.
        #
        # With no declared files there is nothing to read, and that IS unknown.
        if not paths:
            return None
        base = Path(cwd or ".")
        parts: list[str] = []
        for rel in paths:
            try:
                body = (base / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # absent → its path never appears → the check fails
            parts.append(f"--- /dev/null\n+++ b/{rel}\n")
            parts.extend(f"+{line}\n" for line in body.splitlines())
        return "".join(parts)

    parts = [diff]
    for rel in (new_files or "").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        # Render a new file in diff shape so path and symbol assertions can be
        # written once and work for both created and modified files.
        parts.append(f"--- /dev/null\n+++ b/{rel}\n")
        try:
            body = (Path(cwd or ".") / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.extend(f"+{line}\n" for line in body.splitlines())
    return "".join(parts)


def diff_check(
    *,
    files: Sequence[str] = (),
    symbols: Sequence[str] = (),
    files_field: str = "files",
    diff_field: str = "diff",
    cwd: str | None = None,
) -> AcceptanceCheck:
    """Assert the REPOSITORY changed as required. Reads git, not the agent.

    RED3-02 + RED3-08 (P0), and they had to be fixed in one change.

    RED3-08: ``artifacts[diff_field]`` was never populated — no ``cwd`` was
    wired through, so the diff was always empty and the symbol assertion was
    vacuous. The check was dead.

    RED3-02: the evidence came from ``artifacts`` at all. Those are reported BY
    the executor — the party being graded. An agent that wrote nothing could
    return ``{"files": [...], "diff": "...def foo..."}`` and pass. The oracle was
    asking the defendant for the verdict.

    Fixing only RED3-08 would have been strictly worse than leaving it alone: it
    turns a check everyone knows is dead into one that looks alive and can be
    told what to say. So the diff now comes from ``git diff HEAD`` in ``cwd``,
    and the agent-reported fields corroborate only — they can never satisfy.

    A diff that cannot be read is UNKNOWN, and unknown fails. A verification step
    that cannot see the repository has verified nothing, and reporting success
    from that state is the defect in its purest form.
    """
    def check(artifacts: dict[str, Any]) -> AcceptanceResult:
        effective_cwd = cwd or artifacts.get("cwd") or None
        # Scoped to the declared files. Unscoped, an unrelated dirty file
        # elsewhere in the tree can satisfy a symbol assertion and the milestone
        # passes on somebody else's work.
        diff_text = _repo_changes(effective_cwd, files)

        if diff_text is None:
            return AcceptanceResult(
                False,
                "could not read the repository diff — verification did not run. "
                "Refusing to report success for an unobserved change.",
                deterministic=True,
            )

        # `git diff` renders paths on its ---/+++ lines, so testing against the
        # diff body checks the repository rather than the agent's account of it.
        missing_files = [f for f in files if f not in diff_text]
        missing_syms = [s for s in symbols if s not in diff_text]

        if missing_files or missing_syms:
            parts = []
            if missing_files:
                parts.append(f"missing files in the repo diff: {missing_files}")
            if missing_syms:
                parts.append(f"missing symbols in the repo diff: {missing_syms}")
            claimed = set(artifacts.get(files_field, []) or [])
            if claimed and not diff_text.strip():
                parts.append(
                    f"the agent reported touching {sorted(claimed)} but the "
                    f"repository is unchanged"
                )
            return AcceptanceResult(False, "; ".join(parts))

        if (files or symbols) and not diff_text.strip():
            return AcceptanceResult(
                False, "the repository is unchanged", deterministic=True
            )
        return AcceptanceResult(True)
    return check


def cmd_check(
    command: Sequence[str], *, cwd: str | None = None, timeout: float = 60.0
) -> AcceptanceCheck:
    """Pass iff ``command`` (argv list, never a shell string) exits 0.

    A timeout or missing binary is a *deterministic* failure — it won't loop.
    """
    def check(_artifacts: dict[str, Any]) -> AcceptanceResult:
        try:
            # argv list, no shell; check=False → we inspect returncode ourselves.
            proc = subprocess.run(
                list(command), cwd=cwd, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except FileNotFoundError:
            return AcceptanceResult(False, f"command not found: {command[0]}")
        except subprocess.TimeoutExpired:
            return AcceptanceResult(False, f"timed out after {timeout}s")
        if proc.returncode == 0:
            return AcceptanceResult(True)
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        return AcceptanceResult(False, f"exit {proc.returncode}: {tail[0][:200]}")
    return check


def lint_check(
    paths: Sequence[str], *, linter: str = "ruff", cwd: str | None = None, timeout: float = 60.0
) -> AcceptanceCheck:
    """Pass iff ``<linter> check <paths>`` exits 0. If the linter binary isn't
    installed the result is marked non-deterministic (unknown), not a hard fail."""
    resolved = shutil.which(linter)
    if resolved is None:
        def unavailable(_artifacts: dict[str, Any]) -> AcceptanceResult:
            return AcceptanceResult(False, f"linter {linter!r} not available", deterministic=False)
        return unavailable
    return cmd_check([resolved, "check", *paths], cwd=cwd, timeout=timeout)


def reproducible(check: AcceptanceCheck, *, times: int = 2) -> AcceptanceCheck:
    """Run ``check`` ``times`` times; if the pass/fail verdict disagrees across
    runs the failure is flagged ``deterministic=False`` (flaky) so the engine
    re-runs once rather than escalating on noise. Agreeing runs pass through."""
    n = max(2, times)

    def wrapped(artifacts: dict[str, Any]) -> AcceptanceResult:
        results = [check(artifacts) for _ in range(n)]
        verdicts = {r.ok for r in results}
        if len(verdicts) > 1:
            return AcceptanceResult(False, "non-reproducible acceptance verdict", deterministic=False)
        return results[0]
    return wrapped


# ── Oracle integrity (RED3-03 P0) ────────────────────────────────────────────


def _function_is_stub(fn: "ast.FunctionDef | ast.AsyncFunctionDef") -> bool:
    """True when ``fn``'s body does no work: pass / ... / return True / raise NIE.

    A leading docstring is skipped, and only single-statement bodies qualify —
    a partially implemented check is not a stub and must not be reported as one.

    ``return True`` is perfectly reasonable in most code. In an *acceptance
    check* it is the entire defect: the function whose job is to prove work
    happened, asserting that it did without looking.
    """
    import ast

    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]  # drop the docstring
    if len(body) != 1:
        return False

    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    ):
        return True
    if (
        isinstance(stmt, ast.Return)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is True
    ):
        return True
    if isinstance(stmt, ast.Raise):
        exc = stmt.exc
        name = None
        if isinstance(exc, ast.Name):
            name = exc.id
        elif isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
            name = exc.func.id
        if name == "NotImplementedError":
            return True
    return False


def is_stub_check(check: AcceptanceCheck) -> bool:
    """True when ``check`` is a do-nothing function masquerading as verification.

    RED3-03 (P0): a ``return True`` stub submitted as the acceptance check for a
    security-hole task was ACCEPTED, and the milestone recorded DONE. "Done"
    then means "the executor said so", which is the exact property the whole
    MGEE design claims to avoid.

    Source is unavailable for a C function, a lambda built at runtime, or an
    interactively-defined object. Unavailable is NOT a stub — returning True
    there would reject legitimate checks, and a gate with false positives gets
    disabled. It is reported as not-a-stub and the caller keeps its other
    defences.
    """
    import ast
    import inspect
    import textwrap

    try:
        source = textwrap.dedent(inspect.getsource(check))
    except (OSError, TypeError):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _function_is_stub(node)
        if isinstance(node, ast.Lambda):
            b = node.body
            if isinstance(b, ast.Constant) and b.value is True:
                return True
    return False


def reject_stubs(check: AcceptanceCheck) -> AcceptanceCheck:
    """Wrap ``check`` so a do-nothing oracle fails instead of passing.

    Applied at the boundary rather than inside each factory: the stub does not
    come from this module's factories, it comes from an executor that was asked
    to supply its own acceptance check and supplied ``return True``.
    """
    if not is_stub_check(check):
        return check

    def refuse(_artifacts: dict[str, Any]) -> AcceptanceResult:
        return AcceptanceResult(
            False,
            "the acceptance check is a stub (no-op body) — it cannot verify "
            "anything, so the milestone is not DONE",
            deterministic=True,
        )

    return refuse
