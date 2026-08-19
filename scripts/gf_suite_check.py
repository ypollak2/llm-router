#!/usr/bin/env python3
"""Run the suite inside mutmut's working copy exactly as the campaign will.

WHY THIS EXISTS RATHER THAN A SHELL ONE-LINER
---------------------------------------------
Twice now, passing the deselect list through the shell has silently produced the wrong
command. This session's shell is zsh, where an unquoted `$ARGS` does NOT word-split, so
22 flags arrive as a single argument — and 1986 mutant names arrive as one name. Both
times the run looked like it worked: pytest collected, mutmut started, a metadata file
was written. The first was caught only by comparing collection counts, the second only
because `run_metadata.json` records `mutant_names_count` (it said 1).

A control that silently applies nothing is indistinguishable from one that works. So the
argument list is built in Python and handed to `subprocess` as a list. There is no shell
to mis-split it.

WHAT THIS CHECKS
----------------
mutmut marks a mutant KILLED whenever the suite fails. So any test that fails in the
working copy for an ENVIRONMENTAL reason — a file that was not copied, a config that
differs, a timeout that only trips on the inflated tree — fails on every mutant run it
covers and marks them all killed regardless of the mutation. That INFLATES the score.

A clean run here is therefore a precondition for any mutation number being meaningful,
not a nicety. mutmut's own stats stage uses `-x` and stops at the first failure; this
runs the whole suite so the full list of environmental failures is visible at once
instead of one per attempt.
"""

from __future__ import annotations

import argparse
import configparser
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GF_CFG = REPO / "config" / "mutmut_gf.cfg"
WORKING_COPY = REPO / "mutants"

#: Matches gf_mutmut.py. See its MUTATION_PYTEST_TIMEOUT for why 30s is not enough here.
PYTEST_TIMEOUT = "300"


def deselect_args() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(GF_CFG)
    raw = parser.get("mutmut", "pytest_add_cli_args", fallback="")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exitfirst", action="store_true", help="stop at the first failure")
    ap.add_argument("--durations", default="20")
    ns = ap.parse_args()

    if not WORKING_COPY.exists():
        sys.exit(f"no working copy at {WORKING_COPY} — run gf_mutmut.py first")

    args = deselect_args()
    if not args:
        sys.exit("no deselect args parsed from the config — refusing to run a check that "
                 "would not match what the campaign actually does")

    cmd = [
        str(REPO / ".venv" / "bin" / "python"), "-m", "pytest",
        "-p", "no:cacheprovider", f"--durations={ns.durations}", "-q",
        *(["-x"] if ns.exitfirst else []),
        *args,
    ]
    print(f"{len(args)} deselect args; running {len(cmd)} argv entries in {WORKING_COPY}")

    proc = subprocess.run(cmd, cwd=WORKING_COPY, env={**os.environ, "PYTEST_TIMEOUT": PYTEST_TIMEOUT})
    print(f"PYTEST_EXIT={proc.returncode}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
