"""Pre-write verification for scoped zero-Claude edits (plan item 3.3).

``zero_claude_edit.maybe_replace`` writes the local model's edit to disk once
``edit.apply_edits`` has validated it: exact-once ``old_string`` match plus a
syntax check (``edit.check_syntax`` — ``ast.parse`` for Python, ``json.loads``,
``yaml.safe_load``). Syntax-valid is not lint-clean: a local model can produce
code that parses but shadows a builtin, leaves an unused import, or otherwise
fails the project's own lint gate (the CI ``lint`` job, ``uvx ruff check src/
tests/``) — none of which ``ast.parse`` catches. This module is that one more
gate, run between validation and the write.

It reuses ``llm_router.agentic.acceptance.cmd_check`` for the actual
run-a-command/read-the-exit-code machinery rather than re-implementing it —
the MGEE acceptance checks already solved "run this, trust the exit code, not
the caller's say-so" and there is no reason for the hook to solve it again.

WHAT RUNS, AND WHY NOT MORE
----------------------------
Python touched files only: ``ruff check`` (CI's own linter, same rule
selection — see ``_ruff_config_path``) against the candidate text. There is
no fast "which tests target this file" mapping in this repo today (checked:
no test-to-module index, no per-file test manifest), so the plan's "tests
that obviously target the touched module" fallback is unavailable here and
this module does not attempt to invent one — inventing a weak mapping would
be worse than not having one, because a wrong test selected is a false
confidence, not a faster true one. That leaves "lint/compile only", which is
exactly what this module does: lint when a linter is reachable, and rely on
the compile/syntax check ``edit.check_syntax`` already performed when it is
not (see ``verify_changed_files`` for that fallback's exact behaviour).
Running the WHOLE test suite inside a UserPromptSubmit hook is explicitly
out of scope (plan item 3.3) — it would blow the hook's own wall-clock
budget on a single invocation.

Non-Python changed files are not linted by this module at all: nothing it
calls ("ruff") understands them, and ``edit.check_syntax`` already parses the
other two types it validates (JSON, YAML). This is a scope limit, not a
claim that unchecked types are safe.

DESIGN: verify a temp copy, never the real file
-------------------------------------------------
``maybe_replace`` already holds the full post-edit content in memory
(``new_contents``, a ``path -> text`` dict) before it opens anything for
writing — the write loop is the LAST thing it does. So "verify before
persisting" does not need write-then-roll-back at all: the candidate text is
written to an OS temp file (never the real path, never even the real
directory) and the linter runs against that. The real file is simply never
opened for writing until the check passes. There is no window, ever, in
which the user's file holds unverified content — nothing to roll back, and
no crash-between-write-and-revert case to reason about, because the write
to the real path is the one and only disk write this module's callers ever
make to it. This is strictly safer than write-then-verify-then-revert-on-
failure, which was the other option on the table: that shape has a real
(if brief) window where the file holds broken content, and depends on the
revert itself never failing (disk full, permissions, a second process
reading mid-window). Writing nowhere until verified removes the window
rather than racing to close it.

The temp file is NOT placed inside the repo tree. Ruff resolves project
config (``[tool.ruff]`` in ``pyproject.toml`` here) by walking up from the
file being checked, which would fail for a file outside the repo — so the
repo's config is named explicitly via ``--config`` instead of relying on
that walk. This also sidesteps any path-shaped lint rule (e.g. a per-file
ignore keyed to the real relative path) ever matching the temp path instead
of the real one — this repo's ``[tool.ruff.lint]`` has no per-file-ignores
today, so that gap has no live consequence, but it is named here rather
than discovered later.

BUDGET
------
``verify_changed_files`` is handed whatever is left of the SAME
``deadline_s`` the rest of ``maybe_replace`` already answers to — see
``hooks/auto-route.py``'s ``_hook_deadline``/``_readonly_draft_deadline``,
the single ~55s-by-default wall clock the whole hook invocation must answer
to. It never opens a budget of its own. Too little time left to even start
the linter, or a timeout once it has started, is reported as *unverified*,
and the caller (``zero_claude_edit.maybe_replace``) treats unverified
exactly like a verification FAILURE: escalate to Claude, do not write —
never as a pass. The one case that does NOT escalate is "no linter could be
found at all" (neither ``ruff`` nor ``uv``/``uvx`` on ``PATH``): that
degrades to the pre-3.3 behaviour (the syntax check alone), the same
behaviour every installation had before this module existed, so a machine
without ruff available does not lose the ability to use scoped zero-Claude
edits — it just does not gain this extra gate. A race against the clock
(timeout) and a tool that plainly does not exist are different kinds of
"did not run", and only the owner's "escalate rather than silently write a
broken edit" rule applies to the former.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from llm_router.agentic.acceptance import cmd_check

#: Below this much remaining wall-clock, verification cannot even START a
#: subprocess safely (process spawn + a first ruff invocation is not
#: instant) — treated the same as a timeout once started: unverified, so
#: the caller escalates rather than guessing.
_MIN_VERIFY_S = 2.0

_LINTABLE_SUFFIX = ".py"

# Default ON: measured against this repo's existing zero_claude_edit fixtures
# (tests/test_zero_claude_edit_scope.py) with the var unset — every write-path
# fixture's candidate content is lint-clean under this repo's own ruff config,
# so turning this on by default changes no existing test's outcome. See the
# PR description for the exact command and counts. A machine with neither
# `ruff` nor `uv`/`uvx` on PATH is unaffected either way (see module
# docstring) rather than suddenly blocked.
_DEFAULT_ENABLED = True


def verify_enabled() -> bool:
    """Whether this gate runs at all. See ``_DEFAULT_ENABLED`` for why ON."""
    # Literal name, not a constant: tests/test_env_registry.py scans literal args only.
    raw = os.environ.get("LLM_ROUTER_ZERO_CLAUDE_VERIFY", "").strip().lower()
    if not raw:
        return _DEFAULT_ENABLED
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class VerifyResult:
    """What ``verify_changed_files`` decided.

    ``ok=True`` means the caller may proceed to write — either the lint
    check passed, or there was nothing this module could check (no Python
    files touched, or no linter available: see the module docstring).
    ``ok=False`` means do NOT write; ``reason`` is the one line the caller
    folds into its existing failure/escalation message and ledger entry —
    there is no separate flag for "failed" vs "timed out" because both are
    routed to the exact same escalate-don't-write outcome; the distinction
    lives in the wording of ``reason`` alone.
    """

    ok: bool
    reason: str
    ran: tuple[str, ...] = ()  # which check(s) actually executed, e.g. ("ruff",)


def _ruff_argv() -> list[str] | None:
    """The command to invoke ruff with, or ``None`` if nothing can reach it.

    Prefers a ``ruff`` binary directly on ``PATH``. Falls back to ``uvx
    ruff`` (``uv tool run ruff``, Astral's distribution path) when only
    ``uv``/``uvx`` is installed and ``ruff`` is not — exactly how this
    repo's own CI reaches ruff (``uvx ruff check src/ tests/`` in
    ``.github/workflows/ci.yml``), so a dev machine set up to run this repo
    normally already has one of the two.
    """
    if shutil.which("ruff"):
        return ["ruff"]
    if shutil.which("uvx"):
        return ["uvx", "ruff"]
    return None


def _ruff_config_path(repo_root: Path) -> Path | None:
    """The repo's ruff config file, named explicitly (see module docstring
    for why this is not left to ruff's own upward directory search)."""
    for name in ("ruff.toml", ".ruff.toml", "pyproject.toml"):
        candidate = repo_root / name
        if candidate.is_file():
            return candidate
    return None


def verify_changed_files(
    new_contents: dict[str, str],
    changed_files: list[str],
    repo_root: Path,
    deadline_s: float,
) -> VerifyResult:
    """Lint the CANDIDATE text of every changed Python file before any write.

    Args:
        new_contents: path -> full post-edit text (already syntax-checked by
            ``edit.apply_edits``; never mutated here).
        changed_files: the subset of ``new_contents`` the edit actually
            touched (``maybe_replace`` already computes this before this
            call).
        repo_root: the repo the real files live in — used only to find the
            project's ruff config (``_ruff_config_path``), never as a
            destination for a write.
        deadline_s: the SAME monotonic deadline the rest of this hook
            invocation answers to (see module docstring, "BUDGET").

    Returns:
        A :class:`VerifyResult`. Nothing is ever written to ``repo_root`` or
        to the real paths in ``changed_files`` — this function only reads
        ``new_contents`` and writes to its own OS temp files, which it
        always cleans up.
    """
    if not verify_enabled():
        return VerifyResult(True, "verification disabled (LLM_ROUTER_ZERO_CLAUDE_VERIFY=0)")

    python_files = [f for f in changed_files if Path(f).suffix == _LINTABLE_SUFFIX]
    if not python_files:
        return VerifyResult(True, "no touched file this module can lint — syntax check already covers it")

    remaining = deadline_s - time.monotonic()
    if remaining < _MIN_VERIFY_S:
        return VerifyResult(
            False,
            f"out of hook time budget before verification could start ({remaining:.1f}s left, "
            f"need >= {_MIN_VERIFY_S}s) — unverified, not a pass",
        )

    argv = _ruff_argv()
    if argv is None:
        return VerifyResult(True, "no linter available (ruff, and no uv/uvx to fetch it) — syntax check only")

    with tempfile.TemporaryDirectory(prefix="llm_router_verify_") as tmpdir:
        tmp_to_real: dict[str, str] = {}
        tmp_paths: list[str] = []
        for i, rel in enumerate(python_files):
            tmp_path = str(Path(tmpdir) / f"{i}_{Path(rel).name}")
            Path(tmp_path).write_text(new_contents[rel], encoding="utf-8")
            tmp_to_real[tmp_path] = rel
            tmp_paths.append(tmp_path)

        remaining = deadline_s - time.monotonic()
        if remaining < _MIN_VERIFY_S:
            return VerifyResult(
                False,
                f"out of hook time budget before verification could start ({remaining:.1f}s left, "
                f"need >= {_MIN_VERIFY_S}s) — unverified, not a pass",
            )

        command = list(argv) + ["check"]
        config_path = _ruff_config_path(repo_root)
        if config_path is not None:
            command += ["--config", str(config_path)]
        command += tmp_paths

        check = cmd_check(command, timeout=remaining)
        result = check({})

    if not result.ok:
        reason = result.reason
        for tmp_path, rel in tmp_to_real.items():
            reason = reason.replace(tmp_path, rel)
        if "timed out" in reason:
            return VerifyResult(False, f"ruff check timed out after {remaining:.1f}s — unverified, not a pass", ran=("ruff",))
        return VerifyResult(False, f"ruff check failed: {reason}", ran=("ruff",))

    return VerifyResult(True, "ruff check passed", ran=("ruff",))
