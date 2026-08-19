#!/usr/bin/env python3
"""Every job that runs the full pytest suite must give it the same environment.

WHY THIS EXISTS
===============

The suite is run in two places, deliberately:

  ci.yml          `test`         -- against the source tree, 3.11-3.14
  smoke-test.yml  `wheel-suite`  -- against the BUILT WHEEL (G-D)

G-D exists because the ordinary run cannot see packaging defects. That makes the
two jobs differ in *what they run the suite against*, which is the point -- and
identical in *what the suite needs to run at all*, which is not optional.

They drifted. `ci.yml` sets a dummy OPENAI_API_KEY, with a comment explaining
that a routing-audit fix to `_build_and_filter_chain` made several test modules
require a non-empty provider list (they patch the dispatch layer and never make
a network call, but they need SOME candidate in the chain, and a bare runner has
no keys and no Ollama). `smoke-test.yml` never got it.

Result: G-D failed with 41 errors reading

    ValueError: No providers available for query/budget.
    Configured providers: none

across test_t3_m1, test_t3_m2, test_t3_m4, test_t3_s2, test_t4_m1, test_t4_m2 --
a failure that has nothing to do with wheels, packaging, or anything G-D is
meant to detect. A gate red for a reason unrelated to what it gates is a gate
people learn to skip, and 30_CI_GAP_PLAN is a document about exactly that.

It also cost a wrong diagnosis. §5 of that plan assumed G-D was failing for the
same reason the `test` job was. It was not: the `test` fix landed, `test` went
green on all four versions, and G-D stayed red on this instead.

THE RULE
========

Every step that invokes the full pytest suite must set each variable in
REQUIRED_SUITE_ENV. Adding a third suite runner without the shared environment
fails here rather than six minutes into a wheel build.

This checks presence and value equality, not merely presence: a differing dummy
key would be a subtler version of the same divergence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

#: Environment the suite needs regardless of what it is run against.
#: ci.yml's `test` job is the reference; see its comment for the rationale.
REQUIRED_SUITE_ENV = {
    "OPENAI_API_KEY": "sk-test-dummy-key-for-ci-only-no-real-calls-made",
}

#: Lines that mention pytest without running it. `uv pip install ... pytest ...`
#: is the one that actually occurs (G-D installs test deps into the wheel venv).
_INSTALL_MARKERS = ("pip install", "uv sync", "pip download", "poetry add")

#: Flags that narrow a run to a subset. A narrowed run is a different unit with
#: its own environment contract and is deliberately excluded:
#: `routing-hermetic` runs `pytest -m routing_hermetic` with NO api keys ON
#: PURPOSE -- it exists to prove those tests need no host state. Requiring the
#: shared env there would break the job it is meant to protect.
_NARROWING_FLAGS = (" -m ", " -k ", "--lf", "--last-failed")


def _runs_full_suite(run: str) -> bool:
    """True if the line invokes pytest across the whole suite.

    `pytest`, `pytest tests/`, `python -m pytest -q` qualify.
    `pytest tests/test_one.py`, `pytest -m marker`, `pip install pytest` do not.
    """
    # Join shell line-continuations first. G-D's install step spreads
    # `uv pip install ... \` over several lines, putting the bare word `pytest`
    # on a line that carries no `pip install` marker -- scanning line-by-line
    # reads that as a suite invocation.
    joined: list[str] = []
    buffer = ""
    for raw in run.splitlines():
        buffer += raw.rstrip()
        if buffer.endswith("\\"):
            buffer = buffer[:-1] + " "
            continue
        joined.append(buffer)
        buffer = ""
    if buffer:
        joined.append(buffer)

    for line in joined:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "pytest" not in stripped:
            continue
        if any(marker in stripped for marker in _INSTALL_MARKERS):
            continue
        # `python -m pytest` is an invocation; `pytest -m marker` is a filter.
        # Split on the pytest token so the former's `-m` is not misread.
        _, _, after = stripped.partition("pytest")
        if ".py" in after:
            continue
        if any(flag in f"{after} " for flag in _NARROWING_FLAGS):
            continue
        return True
    return False


def _effective_env(workflow: dict, job: dict, step: dict) -> dict:
    env: dict = {}
    for scope in (workflow.get("env"), job.get("env"), step.get("env")):
        if isinstance(scope, dict):
            env.update(scope)
    return env


def main() -> int:
    violations: list[str] = []
    found_any = False

    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if not isinstance(run, str) or not _runs_full_suite(run):
                    continue
                found_any = True
                env = _effective_env(workflow, job, step)
                for key, expected in REQUIRED_SUITE_ENV.items():
                    actual = env.get(key)
                    if actual is None:
                        violations.append(
                            f"  {path.name} :: job {job_name} :: "
                            f"{step.get('name') or '<unnamed>'}\n"
                            f"      missing {key}"
                        )
                    elif str(actual) != expected:
                        violations.append(
                            f"  {path.name} :: job {job_name} :: "
                            f"{step.get('name') or '<unnamed>'}\n"
                            f"      {key} differs from the reference value"
                        )

    if not found_any:
        # Guards the guard: if the detector stops recognising suite steps, this
        # lint would pass while checking nothing -- the exact shape of defect
        # 30_CI_GAP_PLAN §8 is about.
        print(
            "FAIL: no full-suite pytest step found in any workflow. Either the "
            "suite stopped running in CI, or this lint's detector is broken. "
            "Both are failures.",
            file=sys.stderr,
        )
        return 1

    if violations:
        print("SUITE-ENV PARITY FAIL: a full-suite run is missing shared environment.\n")
        print("\n".join(violations))
        print(
            "\nEvery job running the whole suite needs the same environment to run it,"
            "\nregardless of whether it targets the source tree or the built wheel."
            "\nThis exact gap made G-D red with 41 `No providers available` errors that"
            "\nhad nothing to do with packaging -- see scripts/lint_suite_env_parity.py."
        )
        return 1

    print("suite-env parity OK: every full-suite run has the shared environment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
