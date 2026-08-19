#!/usr/bin/env python3
"""Reject backslash-escaped quotes in workflow `run:` blocks that have no explicit shell.

WHY THIS EXISTS
===============

All three `windows-latest` legs of PR #253 were red. The plan written for those
failures (30_CI_GAP_PLAN.md §6) listed three suspects -- a subprocess/PYTHONPATH
shim, a `Path.home()` lint, an `importlib` load of a dash-named script -- and all
three were wrong. The actual cause was a quoting bug in the workflow itself:

    python -c "
    ...
    assert after.get('model') == 'opus', f'... {after.get(\\"model\\")}'
    "

`run:` uses **bash** on ubuntu/macos and **pwsh** on windows. bash collapses `\\"`
inside a double-quoted string to `"`, so Python receives valid source. pwsh does
not treat backslash as an escape character at all, so the backslashes survive
verbatim into the Python source and the interpreter dies with a SyntaxError
before running a single assertion.

The failure is therefore invisible on every platform the developer can reach,
and unreproducible without a Windows runner -- the exact shape 30_CI_GAP_PLAN
§8 is about: a check that is green for a reason unrelated to the code.

THE RULE
========

A `run:` block that contains `\\"` and does **not** pin `shell:` is a portability
trap: its meaning depends on which OS picked up the job. Two ways to satisfy
this lint:

1. Don't use `\\"`. Hoist the value into a local first -- this is almost always
   clearer anyway::

       model_after = after.get('model')
       assert model_after == 'opus', f'clobbered: {model_after}'

2. Pin the shell (`shell: bash`) if the escaping genuinely is bash-specific and
   the step is not meant to run under pwsh.

The baseline is zero. There is no accepted-debt list, because there is no
legitimate use of this construct in an unpinned block.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The construct that means one thing in bash and another in pwsh.
TRAP = '\\"'


def _steps(workflow: dict) -> list[tuple[str, dict]]:
    """Yield (job_name, step) for every step in the workflow."""
    out: list[tuple[str, dict]] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                out.append((job_name, step))
    return out


def _shell_is_pinned(workflow: dict, job_name: str, step: dict) -> bool:
    """True if the step's shell is fixed rather than chosen by the runner OS."""
    if step.get("shell"):
        return True
    job = (workflow.get("jobs") or {}).get(job_name) or {}
    for scope in (job.get("defaults") or {}), (workflow.get("defaults") or {}):
        if isinstance(scope, dict) and (scope.get("run") or {}).get("shell"):
            return True
    return False


def main() -> int:
    violations: list[str] = []

    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        try:
            workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"{path.name}: could not parse: {exc}", file=sys.stderr)
            return 1
        if not isinstance(workflow, dict):
            continue

        for job_name, step in _steps(workflow):
            run = step.get("run")
            if not isinstance(run, str) or TRAP not in run:
                continue
            if _shell_is_pinned(workflow, job_name, step):
                continue
            name = step.get("name") or step.get("uses") or "<unnamed step>"
            offending = [
                f"      line {i}: {line.strip()}"
                for i, line in enumerate(run.splitlines(), 1)
                if TRAP in line
            ]
            violations.append(
                f"  {path.name} :: job {job_name} :: {name}\n" + "\n".join(offending)
            )

    if violations:
        print("SHELL-PORTABILITY FAIL: backslash-escaped quotes in an unpinned `run:` block.\n")
        print("\n".join(violations))
        print(
            "\n`run:` is bash on ubuntu/macos and pwsh on windows. bash turns \\\" into \","
            "\npwsh leaves the backslash in place -- so the same block is valid on one"
            "\nplatform and a SyntaxError on the other. This is what made all three"
            "\nwindows legs of PR #253 red while every reachable platform stayed green."
            "\n\nFix by hoisting the value into a local (preferred), or by pinning"
            "\n`shell: bash` if the step is genuinely bash-only."
        )
        return 1

    print("shell-portability OK: no backslash-escaped quotes in unpinned `run:` blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
