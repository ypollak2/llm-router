#!/usr/bin/env python3
"""Extract every documented shell command and prove it runs.

WHY THIS EXISTS
===============

README.md promises `pip install llm-routing && llm_router install --host claude-code`
and "Get Started (60 seconds)". Those are testable claims, and nothing tested them.

This audit examined four documented claims this week and all four were false — a
gate reported PASS on gitignored files, a test passed on the developer's own
routing history, SECURITY.md asserted the opposite of shipped behaviour, and a
docstring claimed a sandbox that shell execution bypasses. The prior on an
unexecuted doc claim is not neutral.

WHAT IT CHECKS

Commands are PARSED out of fenced blocks, not listed here, so a command added to
the README later is covered without editing this script — the same reason
lint_workflow_shell_portability parses rather than greps.

Each is classified:

    RUNNABLE   safe to execute in a sandbox and expected to exit 0
    SKIPPED    explicitly marked non-executable, or needs credentials/network
               that a clean check cannot supply

A command is only SKIPPED for a stated reason. "It failed and I do not know why"
is not one — that is the finding.

GUARDS THE GUARD

Extracting zero commands FAILS. A doc-checker that finds nothing passes
vacuously, which is failure mode #1 in the list above and the single most likely
way this script becomes decorative.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

#: Docs whose shell blocks are treated as promises to the user.
_DOCS = ["README.md"]

#: Prefixes that identify a line as a command rather than output or a comment.
_CMD_START = ("pip ", "pipx ", "uv ", "llm_router", "python ", "python3 ", "claude ", "npx ")

#: Commands whose non-zero exit is CORRECT on a clean machine, with what must
#: appear in their output to prove they failed usefully rather than merely failed.
#: `llm_router doctor` reporting "hooks not installed" on a fresh box is the tool
#: working; a checker that demanded exit 0 would push someone to weaken it.
_EXPECTED_NONZERO: dict[str, str] = {
    "llm_router doctor": "fix:",
}

#: Commands that cannot be verified in a clean sandbox, each with the reason.
#: A reason is mandatory — an unexplained skip is how a check quietly stops checking.
_SKIP: dict[str, str] = {
    "claude mcp": "requires an installed Claude Code host, not present in CI",
    "ollama": "requires a running Ollama daemon; optional dependency by design",
    "llm_router install": "mutates ~/.claude/settings.json — needs an isolated HOME, see --deep",
    "llm_router-onboard": "interactive prompts",
    "llm_router-quickstart": "interactive prompts",
    "--watch": "long-running watch mode; never exits, so it cannot be checked this way",
    "pip install llm_router": (
        "installs the PUBLISHED package over the source tree being checked. "
        "Verifying it needs a genuinely isolated environment — the clean-container "
        "install job in doc 34 Step 1 — not this in-tree checker, which would "
        "shadow the working copy and then report on the wrong code."
    ),
}


@dataclass
class Cmd:
    doc: str
    line: int
    text: str

    @property
    def skip_reason(self) -> str | None:
        """Match anywhere, not just at the start.

        The first version checked `startswith` only, and let
        `pip install X && llm_router install --host claude-code` through — the
        compound form of a command already on the skip list. Found by running the
        extractor rather than by reviewing it, which is the entire argument for
        this script existing.
        """
        for needle, reason in _SKIP.items():
            if needle in self.text:
                return reason
        return None


def extract(doc: Path) -> list[Cmd]:
    """Pull command lines out of fenced code blocks."""
    out: list[Cmd] = []
    in_block = False
    for i, raw in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        if raw.lstrip().startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip an inline trailing comment so `pip install x  # note` still runs.
        line = re.split(r"\s+#\s", line)[0].strip()
        if line.startswith(_CMD_START):
            out.append(Cmd(doc.name, i, line))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="execute RUNNABLE commands")
    ap.add_argument("--time-budget", type=float, default=None,
                    help="fail if the total runtime of RUNNABLE commands exceeds this many "
                         "seconds — for checking a documented time claim rather than "
                         "asserting one")
    args = ap.parse_args()

    cmds: list[Cmd] = []
    for name in _DOCS:
        path = _ROOT / name
        if not path.is_file():
            print(f"FAIL: documented file {name} not found", file=sys.stderr)
            return 1
        cmds.extend(extract(path))

    if not cmds:
        # The vacuity guard. See the module docstring.
        print(
            "FAIL: extracted ZERO commands from "
            f"{', '.join(_DOCS)}. Either the docs stopped showing commands, or "
            "this extractor is broken. A doc-checker that finds nothing passes "
            "while checking nothing — that is the defect this guards against.",
            file=sys.stderr,
        )
        return 1

    runnable = [c for c in cmds if not c.skip_reason]
    skipped = [c for c in cmds if c.skip_reason]

    print(f"extracted {len(cmds)} commands: {len(runnable)} runnable, {len(skipped)} skipped\n")
    for c in skipped:
        print(f"  SKIP  {c.doc}:{c.line}  {c.text}")
        print(f"        reason: {c.skip_reason}")
    if skipped:
        print()

    if not args.run:
        for c in runnable:
            print(f"  RUNNABLE  {c.doc}:{c.line}  {c.text}")
        print("\n(dry run — pass --run to execute)")
        return 0

    failures = []
    import time as _time
    _t0 = _time.perf_counter()
    for c in runnable:
        proc = subprocess.run(  # noqa: S602 — commands come from our own README
            c.text,
            shell=True,
            capture_output=True,
            # text=True alone decodes with the LOCALE encoding, which on windows
            # is cp1252. The commands under test print ✓ / ✗ / ⚡ / 💰, so the
            # reader thread died with UnicodeDecodeError on the child's own
            # output — and because the read failed, the expected-non-zero check
            # never saw the remediation text either, turning a healthy `llm_router
            # doctor` into a reported failure.
            #
            # This is the MIRROR of CHZ-WIN-01: that fix made the child WRITE
            # utf-8; this one makes the parent READ it. Fixing one side and not
            # the other just moves the crash across the pipe.
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        expect = next((v for k, v in _EXPECTED_NONZERO.items() if k in c.text), None)
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            ok = True
        elif expect is not None:
            # Non-zero is allowed, but only if it still tells the user what to do.
            ok = expect in out
        else:
            ok = False
        status = "ok" if ok else f"EXIT {proc.returncode}"
        note = "  (expected non-zero, remediation printed)" if ok and proc.returncode else ""
        print(f"  [{status:>7}]  {c.doc}:{c.line}  {c.text}{note}")
        if not ok:
            failures.append((c, proc))

    if failures:
        print(f"\nFAIL: {len(failures)} documented command(s) do not work:\n")
        for c, proc in failures:
            print(f"  {c.doc}:{c.line}  {c.text}")
            # PRINT THE WHOLE DIAGNOSTIC, not the first few lines. The first
            # version capped this at 4, and the first real failure it caught —
            # `llm_router doctor` crashing on windows — was truncated mid-traceback
            # at the frame BEFORE the exception. A checker that detects a
            # failure and hides its cause turns one CI round-trip into two.
            #
            # Bounded generously rather than absolutely: a runaway command
            # should not bury the summary, but 60 lines carries any traceback
            # worth reading.
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            shown = err[-60:] if len(err) > 60 else err
            if len(err) > 60:
                print(f"      … {len(err) - 60} earlier line(s) omitted …")
            for line in shown:
                print(f"      {line}")
            print()
        print("Fix the doc where it is wrong, or the code where the doc is right.")
        return 1

    elapsed = _time.perf_counter() - _t0
    print(f"\ndocs-commands OK: {len(runnable)} documented commands all exit 0 "
          f"({elapsed:.1f}s total)")

    if args.time_budget is not None and elapsed > args.time_budget:
        # The README says "Get Started (60 seconds)". An unmeasured time claim is
        # the same defect as an unexecuted command — it reads as verified and is
        # not. Either it holds, or the README states the real number.
        print(
            f"\nFAIL: documented commands took {elapsed:.1f}s, over the "
            f"{args.time_budget:.0f}s the docs claim.\n"
            f"Either speed it up, or change the README to the measured number — "
            f"a slower honest claim passes, an unmeasured one does not.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
