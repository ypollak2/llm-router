#!/usr/bin/env python3
"""G-C — the acceptance criteria must not change under the executor.

18_REMEDIATION_EXECUTION_PLAN.md states the rule twice:

    "Criteria are hashed before the run (sha256 of the WP's criteria block) and
     re-verified after. A changed hash = automatic FAIL."          (line 34)

    "Pre-register and hash the acceptance check before execution; re-verify the
     hash after. The executor cannot edit the oracle it is graded against."
                                                                   (line 411)

and G-C makes a mismatch a disqualifier: "Any acceptance-criteria hash mismatch
-> WP FAILS".

**No hash was ever computed.** G-C was therefore a gate that could not fire --
the same defect class this audit found in `unregistered()` and
`lint_tool_surface.py`: something shaped like a check, reporting nothing,
counted as protection. This script is its implementation.

WHAT THIS CAN AND CANNOT PROVE
------------------------------
It is honest about three limits, because a gate that overclaims is worse than a
missing one:

1. **It is retroactive, and retroactivity does not work.** The manifest was
   created 2026-08-12, AFTER WP-00..WP-15 had already executed. It cannot show
   that the criteria for those work packages were unmodified while they ran. It
   binds WP-16 onward. Anyone reading a "G-C PASS" for an earlier WP should read
   it as "unchanged since the manifest", never as "unchanged during execution".

2. **It does not stop a determined executor.** Whoever can edit the criteria can
   re-run this with --write. The real protection is that the audit documents are
   now TRACKED: an edit shows up in `git log -p` and in review. What the manifest
   adds is that editing an oracle now requires a second, deliberate, visible act
   instead of a silent one.

3. **It hashes whole files, not criteria blocks.** The plan says "sha256 of the
   WP's criteria block". Per-block hashing was rejected: it needs a parser for
   the block boundaries, and a parser bug would silently narrow what is covered
   -- exactly the under-reporting failure the env registry's scanner blind spot
   demonstrated. Whole-file hashing cannot under-report. It over-reports instead:
   a typo fix in unrelated prose trips the gate and must be re-baselined
   deliberately. That direction of failure is the safe one.

WHY `git ls-files` AND NOT `rglob`
----------------------------------
This gate FAILED on the first clean checkout that ever ran it (PR #253), and it
would have failed on any of them. It had been reported PASS locally roughly a
dozen times.

`rglob` walks the developer's disk. The disk held four gitignored evidence
captures (2 x 63MB plus WAL/SHM) that `.gitignore` excludes from the repository:

    .llm_router/zero-tolerance-audit/evidence/**/*.db

So --write recorded them, verify found them, and the gate reported green -- on
one machine. CI checked out the repository, could not find files that were never
in it, and reported four MISSING entries. MISSING is the loudest failure this
gate has, and it was firing on the gate's own bookkeeping rather than on drift.

The manifest's authority is therefore `git ls-files`, not the filesystem. The
two properties that matters:

- a file that is tracked and changes  -> G-C fails (the point of the gate);
- a file that is untracked            -> never gating, so it can never produce a
  MISSING that reproduces only on someone else's machine.

The untracked hashes are still RECORDED, in a clearly separated non-gating
section. 30_CI_GAP_PLAN §3 left this as an open question -- those four files are
genuine incident evidence (the damaged and pre-repair copies of the operator's
real database from AUDITOR_INCIDENT.md), correctly gitignored because 127MB of a
user's real usage data has no business in a public repo. Deleting their hashes
outright would make "too big to commit" mean "unverifiable", which is the thing
the original comment here was right to refuse. Recording them without gating on
them keeps both: anyone still holding the files can check them, and nobody's
clean checkout can be broken by them.

The separation is STRUCTURAL, by section header, not by a per-line tag. A tag
can be typed onto a line by hand or by a future edit and silently move a file
out of the gated set; a file below the non-gating header is visibly outside it.

If git is unavailable this script FAILS rather than falling back to a
filesystem walk. A silent fallback would reintroduce exactly the defect above,
and a gate that degrades quietly into the broken mode is worse than no gate.

Usage:
    python scripts/verify_criteria_hashes.py            # verify, exit 1 on drift
    python scripts/verify_criteria_hashes.py --write    # re-baseline (deliberate)
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / ".llm-router" / "zero-tolerance-audit"
MANIFEST = AUDIT / "CRITERIA_MANIFEST.sha256"

#: Header that begins the non-gating tail of the manifest. Everything below it
#: is recorded evidence, never compared. See the module docstring for why this
#: is a section boundary and not a per-line marker.
_NON_GATING_HEADER = "# --- BELOW THIS LINE IS RECORDED BUT NOT GATED ---"

#: Gitignored for size: the RED2 unknown-vs-zero reproduction captures.
_UNTRACKED_SUFFIXES = (".db", ".db-shm", ".db-wal")


class GitUnavailable(RuntimeError):
    """Raised when tracked-file membership cannot be established."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tracked_relpaths() -> set[str]:
    """Audit-relative paths that git tracks. The authority for what is gated."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--", str(AUDIT.relative_to(ROOT))],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # git not installed
        raise GitUnavailable(f"could not run git: {exc}") from exc
    if proc.returncode != 0:
        raise GitUnavailable(
            f"git ls-files exited {proc.returncode}: {proc.stderr.strip()}"
        )

    out: set[str] = set()
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        rel = Path(entry)
        try:
            out.add(str(rel.relative_to(AUDIT.relative_to(ROOT))))
        except ValueError:
            continue
    return out


def _gated_entries() -> list[tuple[str, str]]:
    """(sha256, audit-relative path) for every tracked, present audit file.

    The manifest cannot hash itself; a self-referential entry would either never
    match or be excluded from its own check, and both read as passing.
    """
    manifest_rel = str(MANIFEST.relative_to(AUDIT))
    out = []
    for rel in sorted(_tracked_relpaths()):
        if rel == manifest_rel:
            continue
        path = AUDIT / rel
        if not path.is_file():
            # Tracked but absent from the worktree. Reported by verify() as
            # MISSING via the recorded/actual diff, which is the correct signal:
            # a deleted criteria file is real drift.
            continue
        out.append((_sha256(path), rel))
    return out


def _non_gating_entries() -> list[tuple[str, str]]:
    """Hashes of untracked evidence that is present on this machine.

    Recorded so the evidence stays checkable by whoever holds the files. Never
    compared, so its absence elsewhere is not a failure.
    """
    tracked = _tracked_relpaths()
    out = []
    for path in sorted(AUDIT.rglob("*")):
        if not path.is_file() or path.suffix not in _UNTRACKED_SUFFIXES:
            continue
        rel = str(path.relative_to(AUDIT))
        if rel in tracked:
            continue
        out.append((_sha256(path), rel))
    return out


def write_manifest() -> int:
    gated = _gated_entries()
    ungated = _non_gating_entries()

    lines = [
        "# G-C acceptance-criteria manifest (sha256).",
        "# Generated by scripts/verify_criteria_hashes.py --write.",
        "#",
        "# THE GATED SET IS `git ls-files`, NOT THE FILESYSTEM. Recording a file",
        "# that is not in the repository is what made this gate fail on every",
        "# clean checkout while passing on the developer's disk -- see the module",
        "# docstring of scripts/verify_criteria_hashes.py.",
        "#",
        "# THIS MANIFEST IS RETROACTIVE: created after WP-00..WP-15 executed, so",
        "# it binds WP-16 onward and cannot attest to what came before.",
        "",
    ]
    lines += [f"{digest}  {rel}" for digest, rel in gated]

    lines += [
        "",
        _NON_GATING_HEADER,
        "#",
        "# Gitignored incident evidence (the damaged and pre-repair copies of the",
        "# operator's real usage.db, per AUDITOR_INCIDENT.md). Too large and too",
        "# personal to commit, so their hashes are recorded here and NOT compared:",
        "# a clean checkout legitimately does not have them. Anyone still holding",
        "# the files can verify them against these digests.",
        "#",
    ]
    lines += [f"{digest}  {rel}" for digest, rel in ungated]

    MANIFEST.write_text("\n".join(lines) + "\n")
    print(
        f"wrote {MANIFEST.relative_to(ROOT)}: {len(gated)} gated (tracked), "
        f"{len(ungated)} recorded-not-gated"
    )
    return 0


def _parse_manifest() -> tuple[dict[str, str], dict[str, str]]:
    """Return (gated, non_gated) recorded digests, split at the section header.

    The split is positional: anything after the header is non-gating regardless
    of how it looks. Parsing a marker out of the line itself would let a hand
    edit move a file between the two sets invisibly.
    """
    gated: dict[str, str] = {}
    non_gated: dict[str, str] = {}
    target = gated
    for raw in MANIFEST.read_text().splitlines():
        line = raw.strip()
        if line == _NON_GATING_HEADER:
            target = non_gated
            continue
        if not line or line.startswith("#"):
            continue
        digest, _, rest = line.partition("  ")
        rel = rest.replace("  [untracked]", "").strip()
        target[rel] = digest
    return gated, non_gated


def verify() -> int:
    if not MANIFEST.exists():
        print(f"FAIL: no manifest at {MANIFEST.relative_to(ROOT)}. G-C cannot be "
              f"evaluated; run --write to baseline.", file=sys.stderr)
        return 1

    recorded, recorded_ungated = _parse_manifest()
    if not recorded:
        # Guards the guard: an empty manifest would make every comparison below
        # vacuous and print a clean PASS. Same shape as the probe that reported
        # "0/6 reproductions" while measuring nothing.
        print("FAIL: manifest parsed to zero GATED entries -- it cannot detect "
              "drift.", file=sys.stderr)
        return 1

    try:
        actual = {rel: digest for digest, rel in _gated_entries()}
    except GitUnavailable as exc:
        # Deliberately not a filesystem fallback. Walking the disk instead is
        # precisely the behaviour that made this gate green on one machine and
        # red on every other one.
        print(f"FAIL: cannot determine tracked files, so G-C cannot be "
              f"evaluated: {exc}", file=sys.stderr)
        return 1

    changed = sorted(
        r for r in recorded.keys() & actual.keys() if recorded[r] != actual[r]
    )
    added = sorted(actual.keys() - recorded.keys())
    missing = sorted(recorded.keys() - actual.keys())

    # Regression guard for the defect this rewrite fixes: an untracked path in
    # the GATED section can only produce a MISSING that reproduces on one
    # machine. Named explicitly, because "MISSING" alone is what sent the last
    # reader looking for drift that did not exist.
    stowaways = sorted(r for r in recorded if Path(r).suffix in _UNTRACKED_SUFFIXES)
    if stowaways:
        print(
            "FAIL: gitignored paths recorded in the GATED section -- these can "
            "never exist in a clean checkout:",
            file=sys.stderr,
        )
        for rel in stowaways:
            print(f"  {rel}", file=sys.stderr)
        print(
            "Re-baseline with --write; they belong below "
            f"'{_NON_GATING_HEADER}'.",
            file=sys.stderr,
        )
        return 1

    if not (changed or added or missing):
        print(
            f"G-C OK: {len(recorded)} tracked audit artifacts match the manifest "
            f"({len(recorded_ungated)} more recorded but not gated)."
        )
        return 0

    for rel in changed:
        print(f"CHANGED since baseline: {rel}", file=sys.stderr)
    for rel in added:
        print(f"NEW, unrecorded artifact: {rel}", file=sys.stderr)
    for rel in missing:
        # A deleted criteria file is the loudest possible version of the defect
        # G-C exists to catch, so it is a failure, not a warning. Every entry
        # here is now necessarily a TRACKED file, so it is real drift and not
        # the gate tripping over the developer's local disk.
        print(f"MISSING (recorded but gone): {rel}", file=sys.stderr)
    print(
        "\nG-C FAIL. If a change was deliberate, re-baseline with --write IN ITS "
        "OWN COMMIT so the edit to the oracle is visible in review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(write_manifest() if "--write" in sys.argv[1:] else verify())
    except GitUnavailable as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
