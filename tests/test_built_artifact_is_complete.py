"""Every source package must survive the build into the sdist AND the wheel.

WHY THIS EXISTS
===============

13.0.0 shipped without ``llm_router.agents``. ``import llm_router.server`` failed
with ``No module named 'llm_router.agents'``, so the MCP server exited before
registering a single tool and the client reported ``CONNECTION_CLOSED`` —
indistinguishable from a network fault.

Two unanchored exclusion patterns, in two different files. Without a leading
slash both ``.gitignore`` and hatch's sdist ``exclude`` match a directory of
that name at ANY depth:

    .gitignore     "agents/"  also matched src/llm_router/agents/   (5 files)
                   "Library/" also matched src/llm_router/library/  (6 files)
    pyproject      "agents/"  also matched src/llm_router/agents/

The first pair was caught by CI before the tag. The second SHIPPED, because
``uv build`` builds the wheel FROM THE SDIST — so an sdist exclusion silently
becomes a wheel exclusion.

WHY EVERYTHING ELSE WAS GREEN
=============================

A local ``uv build --wheel`` builds straight from source and included the
files. The suite, the linters, the identity gate and CI all ran against the
source tree, where nothing was missing. Every check was answering "is the
source correct?" when the question that mattered was "is the ARTIFACT
complete?".

That is the gap this file closes: it builds both artifacts and looks inside
them, rather than trusting that what is on disk is what gets published.

Marked slow — it runs the real build.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "llm_router"


def _source_subpackages() -> set[str]:
    """Every importable subpackage under src/llm_router (has an __init__.py)."""
    return {
        p.parent.relative_to(_SRC).as_posix()
        for p in _SRC.rglob("__init__.py")
        if "__pycache__" not in p.parts and p.parent != _SRC
    }


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build BOTH artifacts the way the release workflow does.

    `uv build` with no target flag — the same command publish.yml runs. Using
    `--wheel` here would reproduce the bug's blind spot exactly: it bypasses
    the sdist, which is where the exclusion applied.
    """
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [
            *("uv", "build", "-o", str(out)),
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        pytest.skip(f"uv build unavailable or failed: {result.stderr[-400:]}")
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    if not wheels or not sdists:
        pytest.skip("build produced no artifacts")
    return wheels[0], sdists[0]


def test_source_has_subpackages_to_check():
    """Guards the guard: an empty expectation makes both tests below vacuous."""
    packages = _source_subpackages()
    assert len(packages) >= 3, (
        f"only {len(packages)} subpackages found under {_SRC} — the detector is "
        f"broken, and the completeness checks would pass against an empty set"
    )


def test_wheel_contains_every_source_subpackage(built):
    wheel, _ = built
    names = zipfile.ZipFile(wheel).namelist()
    missing = sorted(
        pkg
        for pkg in _source_subpackages()
        if not any(f"llm_router/{pkg}/" in n for n in names)
    )
    assert not missing, (
        f"these subpackages exist in src/ but not in the built WHEEL: {missing}.\n"
        f"Check for an unanchored pattern in pyproject's build excludes or in "
        f".gitignore — without a leading slash they match at any depth."
    )


def test_sdist_contains_every_source_subpackage(built):
    """The sdist matters as much as the wheel: `uv build` builds one from the other."""
    _, sdist = built
    with tarfile.open(sdist) as tar:
        names = tar.getnames()
    missing = sorted(
        pkg
        for pkg in _source_subpackages()
        if not any(f"llm_router/{pkg}/" in n for n in names)
    )
    assert not missing, (
        f"these subpackages exist in src/ but not in the built SDIST: {missing}.\n"
        f"This is the one that shipped broken in 13.0.0 — the wheel is built "
        f"FROM the sdist, so an sdist exclusion silently becomes a wheel one."
    )


def test_no_source_file_is_hidden_from_git():
    """A file that exists locally and is not committed does not exist for anyone else.

    Separate from the build checks because it fails EARLIER and for a different
    reason: the build cannot include what the repository never received.
    """
    files = [
        str(p.relative_to(_REPO))
        for p in _SRC.rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    if not files:  # pragma: no cover
        pytest.skip("no source files found")
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=_REPO,
        input="\n".join(files),
        capture_output=True,
        text=True,
        timeout=60,
    )
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, (
        f"{len(ignored)} source file(s) under src/ are gitignored and will never "
        f"be committed:\n" + "\n".join(f"  {n}" for n in ignored[:10])
    )


def test_the_critical_modules_survive_the_build(built):
    """The startup gate's modules specifically — their absence stops the server.

    `server._CRITICAL_MODULES` names what must import or the process exits. A
    missing one is not a degraded install, it is a server that refuses to boot
    with a remediation message telling the user to reinstall, which does not
    help. `llm_router.agents.session` is on that list and is exactly what 13.0.0
    dropped.
    """
    sys.path.insert(0, str(_REPO / "src"))
    from llm_router import server

    wheel, _ = built
    names = zipfile.ZipFile(wheel).namelist()
    missing = []
    for dotted in server._CRITICAL_MODULES:
        rel = dotted.replace(".", "/")
        if not any(f"{rel}.py" in n or f"{rel}/" in n for n in names):
            missing.append(dotted)
    assert not missing, (
        f"critical modules absent from the built wheel: {missing}. The server "
        f"calls _critical_modules_or_die() at startup and will sys.exit(1)."
    )
