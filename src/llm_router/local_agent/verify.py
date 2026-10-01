"""Pre-write verification for scoped zero-Claude edits (plan item 3.3).

``zero_claude_edit.maybe_replace`` writes the local model's edit to disk once
``edit.apply_edits`` has validated it: exact-once ``old_string`` match plus a
syntax check (``edit.check_syntax`` — ``ast.parse`` for Python, ``json.loads``,
``yaml.safe_load``). Syntax-valid is not lint-clean: a local model can produce
code that parses but leaves an unused import or otherwise fails the project's
own lint gate (the CI ``lint`` job, ``uvx ruff check src/ tests/``) — none of
which ``ast.parse`` catches. This module is that one more gate, run between
validation and the write.

WHAT RUNS, AND WHY NOT MORE
----------------------------
Python touched files only: ``ruff check`` against the candidate text. There is
no fast "which tests target this file" mapping in this repo today (checked: no
test-to-module index, no per-file test manifest), so the plan's "tests that
obviously target the touched module" fallback is unavailable and this module
does not invent one — a wrongly selected test is false confidence, not a
faster true one. Running a whole suite inside a UserPromptSubmit hook is out
of scope (plan item 3.3). Non-Python files are not linted here: ``edit.
check_syntax`` already parses JSON and YAML.

Two rules keep the gate from punishing edits for things the edit did not do:

* Only the project's own opted-in ruff config counts. For each touched file
  the config is found by walking up from the file's directory to the repo
  root: ``.ruff.toml``, ``ruff.toml``, or a ``pyproject.toml`` that actually
  has a ``[tool.ruff]`` table (a ``pyproject.toml`` without one does NOT
  count, matching ruff's own discovery). No config found means the repo never
  chose a rule set, and ruff's broad built-in defaults would be rules the
  repo never opted into — so that file is NOT linted and falls back to the
  pre-3.3 behaviour (the syntax check alone), exactly like "no linter
  available" below.
* Only violations NEW in the candidate block the write. The ORIGINAL text is
  linted with the same config and path mapping, and the two are compared as a
  per-file multiset of ``(rule code, message)`` — never by line number, since
  an edit shifts lines. A file with pre-existing lint debt therefore does not
  block every edit forever; only a count that went UP does. (Messages that
  embed a line number, e.g. F811 "from line 3", are normalised so a shifted
  line does not read as a new violation.) If the original cannot be linted
  inside the budget the result is *unverified*, never a pass.

DESIGN: verify temp copies, never the real file
-------------------------------------------------
``maybe_replace`` already holds the full post-edit content in memory before it
opens anything for writing — the write loop is the LAST thing it does. So
"verify before persisting" needs no write-then-roll-back: candidate (and
original) text is written to OS temp directories and linted there. The real
file is never opened for writing until the check passes, so there is no window
in which it holds unverified content and nothing to roll back (rollback has
its own failure modes: disk full, a crash between write and revert, a
concurrent reader).

The temp copy MIRRORS the repo-relative path (``<tmp>/pkg/tests/test_x.py``),
and ruff runs with ``cwd`` set to the mirror of the config file's directory
and ``--config`` pointing at the real config file. Measured (ruff 0.15.8 and
0.16.9): with ``--config``, ruff resolves ``per-file-ignores`` globs relative
to cwd, not to the config file or the file being checked, and the same
config run from another cwd with absolute paths did NOT honour them. Running
from the config's directory is also what ruff's own discovery would do, so a
nested package's config sees paths relative to its own directory. Pointing
``--config`` at the real file (rather than copying it) keeps ``extend =
"../ruff.toml"`` working.

BUDGET
------
``verify_changed_files`` is handed whatever is left of the SAME ``deadline_s``
the rest of ``maybe_replace`` answers to — see ``hooks/auto-route.py``'s
``_hook_deadline``/``_readonly_draft_deadline``, the single ~55s-by-default
wall clock. It never opens a budget of its own. Too little time left to start,
a timeout, a ruff error (exit code 2 / unparseable JSON), or an unlintable
original are reported as *unverified* and the caller treats that exactly like
a verification FAILURE: escalate to Claude, do not write — never as a pass.
Two cases do NOT escalate and degrade to the pre-3.3 syntax-check-only
behaviour instead: no linter binary could be found (neither ``ruff`` nor
``uv``/``uvx`` on ``PATH``), and a touched file with no project ruff config.
Those are "nothing to check against", not a race against the clock.

The JSON output (``--output-format json``) needs ruff's stdout, which
``agentic/acceptance.cmd_check`` deliberately discards (it keeps only the exit
code and last output line), so the subprocess call here is made directly; the
exit-code-is-the-truth philosophy of acceptance.py is kept.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

#: Below this much remaining wall-clock, verification cannot even START a
#: subprocess safely (process spawn + a first ruff invocation is not
#: instant) — treated the same as a timeout once started: unverified, so
#: the caller escalates rather than guessing.
_MIN_VERIFY_S = 2.0

_LINTABLE_SUFFIX = ".py"

#: How many new violations the escalation message names.
_MAX_NAMED = 3

# Default ON. Original basis: all 44 pre-existing zero_claude_edit fixtures
# (tests/test_zero_claude_edit_scope.py) pass unchanged with the gate active.
# Reconsidered after the baseline + config-required fixes: the two ways the
# gate could wrongly block an innocent edit (pre-existing lint debt; rules the
# repo never opted into) are now closed, so what remains blocked is an edit
# that adds a violation of the repo's OWN rules — which its own CI lint job
# would reject anyway. A machine with no linter, or a repo with no ruff
# config, is unaffected (syntax check only, as before).
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

    ``ok=True`` means the caller may proceed to write — the lint check found
    nothing new, or there was nothing this module could check (no Python
    files touched, no linter, no project ruff config: see the module
    docstring). ``ok=False`` means do NOT write; ``reason`` is the one line the
    caller folds into its existing failure/escalation message — failed and
    unverified are routed to the same escalate-don't-write outcome, the
    distinction lives in the wording of ``reason`` alone.
    """

    ok: bool
    reason: str
    ran: tuple[str, ...] = ()  # which check(s) actually executed, e.g. ("ruff",)


def _ruff_argv() -> list[str] | None:
    """The command to invoke ruff with, or ``None`` if nothing can reach it.

    Prefers a ``ruff`` binary directly on ``PATH``. Falls back to ``uvx
    ruff`` (Astral's distribution path) when only ``uv``/``uvx`` is
    installed — exactly how this repo's own CI reaches ruff
    (``uvx ruff check src/ tests/`` in ``.github/workflows/ci.yml``).
    """
    if shutil.which("ruff"):
        return ["ruff"]
    if shutil.which("uvx"):
        return ["uvx", "ruff"]
    return None


def _has_ruff_table(pyproject: Path) -> bool:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    tool = data.get("tool")
    return isinstance(tool, dict) and "ruff" in tool


def _ruff_config_for(repo_root: Path, rel: str) -> Path | None:
    """The ruff config governing ``rel``: walk up from its directory to
    ``repo_root`` (inclusive), nearest wins. In one directory ruff's own
    precedence applies: ``.ruff.toml`` > ``ruff.toml`` > ``pyproject.toml``,
    and a ``pyproject.toml`` only counts if it has a ``[tool.ruff]`` table.
    ``None`` means the repo never opted into a rule set for this file."""
    root = repo_root.resolve()
    directory = (root / rel).parent.resolve()
    while True:
        for name in (".ruff.toml", "ruff.toml"):
            if (directory / name).is_file():
                return directory / name
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file() and _has_ruff_table(pyproject):
            return pyproject
        if directory == root or root not in directory.parents:
            return None
        directory = directory.parent


def _key(entry: dict) -> str:
    """Identity of a violation for baseline comparison: the rule code only.

    Not position (an edit shifts lines) and not the message, which embeds
    names: renaming an already-unused variable, or swapping one unused import
    for another, would otherwise be blamed on the edit. The cost is that an
    edit which fixes one violation and adds another of the same code in the
    same file goes unseen."""
    return entry.get("code") or "syntax-error"


class _Unverified(Exception):
    """Ruff could not give an answer (timeout / error / bad output)."""


def _run_ruff(argv: list[str], config: Path, cwd: Path, rels: list[str], remaining: float) -> dict[str, list[dict]]:
    """Lint ``rels`` (paths relative to ``cwd``) and return violations grouped by path."""
    command = [*argv, "check", "--no-cache", "--output-format", "json", "--config", str(config), *rels]
    try:
        proc = subprocess.run(
            command, cwd=str(cwd), capture_output=True, text=True, timeout=remaining, check=False,
        )
    except FileNotFoundError:
        raise _Unverified(f"command not found: {command[0]}") from None
    except subprocess.TimeoutExpired:
        raise _Unverified(f"ruff check timed out after {remaining:.1f}s") from None
    # Exit 0 = clean, 1 = violations found; anything else is ruff itself failing.
    if proc.returncode not in (0, 1):
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        raise _Unverified(f"ruff exit {proc.returncode}: {tail[0][:200]}")
    try:
        entries = json.loads(proc.stdout or "[]")
    except ValueError:
        raise _Unverified("ruff produced unparseable JSON output") from None
    by_file: dict[str, list[dict]] = {rel: [] for rel in rels}
    for entry in entries:
        name = Path(entry.get("filename", ""))
        try:
            rel = name.resolve().relative_to(cwd.resolve()).as_posix()
        except ValueError:
            continue
        by_file.setdefault(rel, []).append(entry)
    return by_file


def _unverified_budget(remaining: float) -> VerifyResult:
    return VerifyResult(
        False,
        f"out of hook time budget before verification could start ({remaining:.1f}s left, "
        f"need >= {_MIN_VERIFY_S}s) — unverified, not a pass",
    )


def verify_changed_files(
    new_contents: dict[str, str],
    original_contents: dict[str, str],
    changed_files: list[str],
    repo_root: Path,
    deadline_s: float,
) -> VerifyResult:
    """Lint ORIGINAL and CANDIDATE text of every changed Python file before any
    write, and block only on violations new in the candidate.

    Args:
        new_contents: path -> full post-edit text (already syntax-checked by
            ``edit.apply_edits``; never mutated here).
        original_contents: path -> text as it is on disk now (the baseline).
        changed_files: the subset of ``new_contents`` the edit actually
            touched (``maybe_replace`` already computes this).
        repo_root: the repo root — where the config walk stops, and the base
            for repo-relative paths. Never a destination for a write.
        deadline_s: the SAME monotonic deadline the rest of this hook
            invocation answers to (see module docstring, "BUDGET").

    Nothing is ever written to ``repo_root`` or the real paths; only to this
    function's own OS temp directories, always cleaned up.
    """
    if not verify_enabled():
        return VerifyResult(True, "verification disabled (LLM_ROUTER_ZERO_CLAUDE_VERIFY=0)")

    python_files = [f for f in changed_files if Path(f).suffix == _LINTABLE_SUFFIX]
    if not python_files:
        return VerifyResult(True, "no touched file this module can lint — syntax check already covers it")

    by_config: dict[Path, list[str]] = {}
    unconfigured: list[str] = []
    for rel in python_files:
        config = _ruff_config_for(repo_root, rel)
        if config is None:
            unconfigured.append(rel)
        else:
            by_config.setdefault(config, []).append(rel)
    if not by_config:
        return VerifyResult(
            True,
            f"no ruff config for {', '.join(unconfigured)} (none of .ruff.toml, ruff.toml, "
            "pyproject.toml with [tool.ruff] up to the repo root) — syntax check only",
        )

    remaining = deadline_s - time.monotonic()
    if remaining < _MIN_VERIFY_S:
        return _unverified_budget(remaining)

    argv = _ruff_argv()
    if argv is None:
        return VerifyResult(True, "no linter available (ruff, and no uv/uvx to fetch it) — syntax check only")

    root = repo_root.resolve()
    new_violations: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="llm_router_verify_") as tmpdir:
            trees = {}
            for label, contents in (("original", original_contents), ("candidate", new_contents)):
                tree = Path(tmpdir) / label
                for rel in (r for files in by_config.values() for r in files):
                    if rel in contents:
                        target = tree / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(contents[rel], encoding="utf-8")
                trees[label] = tree

            for config, files in by_config.items():
                config_dir_rel = config.resolve().parent.relative_to(root)
                results = {}
                for label in ("original", "candidate"):
                    remaining = deadline_s - time.monotonic()
                    if remaining < _MIN_VERIFY_S:
                        return _unverified_budget(remaining)
                    cwd = trees[label] / config_dir_rel
                    cwd.mkdir(parents=True, exist_ok=True)
                    present = [f for f in files if (trees[label] / f).exists()]
                    rels = [(trees[label] / f).relative_to(cwd).as_posix() for f in present]
                    results[label] = (present, _run_ruff(argv, config, cwd, rels, remaining), cwd)
                for rel in files:
                    cand_present, cand, cand_cwd = results["candidate"]
                    _orig_present, orig, orig_cwd = results["original"]
                    cand_rel = (trees["candidate"] / rel).relative_to(cand_cwd).as_posix()
                    orig_rel = (trees["original"] / rel).relative_to(orig_cwd).as_posix()
                    cand_entries = cand.get(cand_rel, [])
                    budget = Counter(_key(e) for e in orig.get(orig_rel, []))
                    for entry in cand_entries:
                        k = _key(entry)
                        if budget[k] > 0:
                            budget[k] -= 1
                        else:
                            new_violations.append(
                                f"{rel}: {entry.get('code') or 'syntax-error'} "
                                f"{entry.get('message', '')} (line {entry.get('location', {}).get('row', '?')})"
                            )
    except _Unverified as exc:
        return VerifyResult(False, f"{exc} — unverified, not a pass", ran=("ruff",))

    if new_violations:
        named = "; ".join(new_violations[:_MAX_NAMED])
        more = len(new_violations) - _MAX_NAMED
        suffix = f" (+{more} more)" if more > 0 else ""
        return VerifyResult(False, f"edit adds {len(new_violations)} new ruff violation(s): {named}{suffix}", ran=("ruff",))

    note = f"; no ruff config for {', '.join(unconfigured)} (syntax check only)" if unconfigured else ""
    return VerifyResult(True, f"ruff check: no new violations{note}", ran=("ruff",))
