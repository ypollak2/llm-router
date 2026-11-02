"""release.sh (D-349): portable notes extraction, size cap, no direct PyPI upload.

Sources release.sh (main is guarded by BASH_SOURCE) and calls its functions on a
fixture CHANGELOG. No network, no git, no model.
"""
from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]
SH = REPO / "scripts" / "release" / "release.sh"

SMALL = """# Changelog

## [Unreleased]

## [2.0.0] - 2026-01-02

### Added
- thing one
- thing two

## [1.0.0] - 2025-01-01

- old
"""


def _run(body: str, tmp_path, changelog: str, **env):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(changelog)
    out = tmp_path / "notes.md"
    r = subprocess.run(
        ["bash", "-c", f'source "{SH}"; write_release_notes 2.0.0 "{cl}" "{out}"; {body}'],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env},
    )
    assert r.returncode == 0, r.stderr
    return out.read_text()


def test_section_body_is_extracted_without_neighbours(tmp_path):
    notes = _run(":", tmp_path, SMALL)
    assert "### Added" in notes and "thing two" in notes
    assert "1.0.0" not in notes and "old" not in notes and "Unreleased" not in notes
    assert "## [2.0.0]" not in notes


def test_oversize_section_becomes_link_plus_known_issues(tmp_path):
    big = SMALL.replace(
        "- thing two\n",
        "- thing two\n" + "".join(f"- filler entry {i} " + "x" * 80 + "\n" for i in range(1500))
        + "\n### Known issues and disclosures\n- disclosed problem\n\n### Fixed\n- not included\n",
    )
    assert len(big.encode()) > 100_000
    notes = _run(":", tmp_path, big)
    assert len(notes.encode()) < 1000
    assert "Full notes: CHANGELOG.md section [2.0.0]" in notes
    assert "https://github.com/ypollak2/llm-router/blob/v2.0.0/CHANGELOG.md" in notes
    assert "disclosed problem" in notes
    assert "not included" not in notes and "filler" not in notes


def test_oversize_without_known_issues_is_link_only(tmp_path):
    big = SMALL.replace("- thing two\n", "- thing two\n" + "".join("- f " + "x" * 90 + "\n" for _ in range(1200)))
    notes = _run(":", tmp_path, big)
    assert notes.count("\n") == 2 and "Known issues" not in notes


def test_release_sh_never_uploads_to_pypi_or_gnu_head():
    sh = SH.read_text()
    assert "uv publish" not in sh and "pypirc" not in sh and "twine upload" not in sh
    assert "head -n -1" not in sh
    assert "--notes-file" in sh


def test_rollback_never_pushes_or_deletes_remote_tags():
    sh = SH.read_text()
    body = sh[sh.index("rollback() {"):sh.index("NOTES_MAX_BYTES=")]
    assert "git push" not in body and "--delete" not in body
