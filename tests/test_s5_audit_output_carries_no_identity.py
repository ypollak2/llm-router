"""S5 / U-06 — audit and report output is a data-handling surface.

This repository is PUBLIC. Two separate incidents, neither caught by a process
the audit designed:

1. An audit specialist wrote the name and filesystem path of one of the
   operator's OTHER PRIVATE PROJECTS into `audit/02_PRODUCT_CLAIM_MATRIX.md`.
   `scripts/` identity tooling blocked that commit.
2. Ten occurrences of the operator's real home directory (`/Users/<name>`)
   were sitting in seven committed audit documents, found only when Phase 49
   went looking for this class deliberately.

The K1 frozen-state generator reproduced the same class on its FIRST run by
printing `sys.executable`, and was caught only because the privacy assertion
was written before the generator was trusted.

An audit produces documents. Documents are output. Output gets committed. No
phase treated discovery output as a surface that needs the same handling as
product output — which is why this test exists rather than a note in a
checklist.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Absolute paths under a user's home. `~` is fine; `/Users/alice` is not.
_HOME_PATH = re.compile(r"/(?:Users|home)/(?!\s)[A-Za-z0-9._-]+")

#: Placeholders that are deliberately not real people.
_ALLOWED = {
    "/Users/someone",        # an illustrative path in a test fixture
    "/Users/canary13user",   # a canary identity used BY the identity gate
    "/home/runner",          # GitHub Actions
    "/Users/runner",
    # An ILLUSTRATION of the scrubbing rule in Docs/GROUND_TRUTH.md:
    #   "/Users/x/proj/a.py -> <HOME:…>/proj/a.py"
    # Documenting how home paths are redacted requires showing one. Allowed by
    # exact match, so `/Users/xavier` would still fail.
    "/Users/x",
}

#: Directories whose output is committed and therefore public.
_SURFACES = ("audit", "Docs", "docs")


def _markdown_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for d in _SURFACES:
        root = REPO / d
        if root.exists():
            out.extend(sorted(root.rglob("*.md")))
    return out


def test_the_scan_has_files_to_scan():
    """Anti-vacuity: an empty file list passes every assertion below."""
    files = _markdown_files()
    assert len(files) > 10, (
        f"only {len(files)} markdown files found under {_SURFACES}; the scan "
        "has stopped matching the tree and is protecting nothing"
    )


def test_no_committed_document_names_a_home_directory():
    offenders: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in _HOME_PATH.finditer(text):
            if m.group(0) in _ALLOWED:
                continue
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(REPO)}:{line}: {m.group(0)}")
    assert not offenders, (
        "committed document(s) naming a home directory in a PUBLIC repo:\n  "
        + "\n  ".join(offenders[:20])
        + "\n\nReplace with `~`. An audit artifact should record the SHAPE of a "
        "machine, never whose machine it was."
    )


def test_the_current_operators_home_is_absent():
    """The specific case, asserted directly.

    The generic rule above could be weakened by someone adding a home path to
    `_ALLOWED`. This one cannot be satisfied that way.
    """
    home = str(pathlib.Path.home())
    if home in ("/", "/root"):
        pytest.skip("no meaningful home directory in this environment")
    offenders = [
        str(p.relative_to(REPO))
        for p in _markdown_files()
        if home in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, (
        f"document(s) containing this machine's home directory ({home}): "
        f"{offenders}"
    )


def test_the_detector_fires_on_a_known_positive(tmp_path):
    """Prove the regex works, rather than trusting a clean result.

    A scan that matches nothing reports the same thing as a clean tree, and
    this file's whole value is the difference between those two.
    """
    probe = "auditor ran from /Users/alice/Projects/secret-thing\nand also ~/ok\n"
    hits = [m.group(0) for m in _HOME_PATH.finditer(probe)]
    assert hits == ["/Users/alice"], f"detector returned {hits}"
    assert not _HOME_PATH.findall("a tilde path ~/Projects/fine is fine")


def test_generated_reports_redact_at_the_point_of_writing():
    """A gate on committed files is the last line, not the first.

    `freeze_state.py` redacts when it writes. Anything else that generates a
    report into a committed directory must too — a scan can only catch what
    someone already committed.
    """
    gen = REPO / "scripts" / "audit" / "freeze_state.py"
    assert gen.exists(), "the frozen-state generator is gone"
    src = gen.read_text(encoding="utf-8")
    assert "_redact" in src, (
        "freeze_state.py no longer redacts home paths at the point of writing"
    )
    # And it is actually applied, not merely defined.
    assert src.count("_redact(") >= 3, (
        "the redactor is defined but applied to fewer than three outputs; it "
        "was originally applied to the dirty-tree listing, env var values and "
        "the backend probes"
    )
