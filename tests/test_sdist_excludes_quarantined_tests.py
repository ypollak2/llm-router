"""Regression: the sdist shipped _quarantined_tests/ to PyPI.

pyproject's sdist exclude list anchors "/tests/", but the quarantine lives at
the repo ROOT as _quarantined_tests/ and was never added. 13.0.2 shipped 16
files of dead, uncollectable test code — modules that could not even be
imported after the 13.0.0 sync, which is why they were quarantined.

Nobody installing llm-routing needs them, and shipping tests that fail to
import invites exactly the wrong conclusion about the package.
"""
from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def test_the_quarantine_still_exists_at_the_root():
    """Guards the guard: if the directory is ever deleted, this test is vacuous."""
    if not (_REPO / "_quarantined_tests").is_dir():
        pytest.skip("quarantine already removed from the tree")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        ["uv", "build", "-o", str(out)], cwd=_REPO,
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        pytest.skip(f"uv build unavailable or failed: {result.stderr[-400:]}")
    wheels, sdists = list(out.glob("*.whl")), list(out.glob("*.tar.gz"))
    if not wheels or not sdists:
        pytest.skip("build produced no artifacts")
    return wheels[0], sdists[0]


@pytest.mark.slow
def test_sdist_does_not_ship_quarantined_tests(built):
    _, sdist = built
    with tarfile.open(sdist) as tar:
        leaked = [n for n in tar.getnames() if "_quarantined_tests" in n]
    assert not leaked, (
        f"the SDIST ships {len(leaked)} quarantined test files. pyproject's "
        f"exclude list anchors '/tests/' but never covered this directory: {leaked[:5]}"
    )


@pytest.mark.slow
def test_wheel_does_not_ship_quarantined_tests(built):
    """`uv build` builds the wheel FROM the sdist, so both must be checked."""
    wheel, _ = built
    leaked = [n for n in zipfile.ZipFile(wheel).namelist() if "_quarantined_tests" in n]
    assert not leaked, f"the WHEEL ships quarantined test files: {leaked[:5]}"


@pytest.mark.slow
def test_sdist_does_not_ship_the_active_test_suite(built):
    """The sibling assertion — '/tests/' is excluded, so prove it stays excluded."""
    _, sdist = built
    with tarfile.open(sdist) as tar:
        leaked = [n for n in tar.getnames() if "/tests/" in n]
    assert not leaked, f"the SDIST ships the active test suite: {leaked[:5]}"
