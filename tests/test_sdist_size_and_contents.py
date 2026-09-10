"""The sdist must stay publishable — small, and free of working-tree scratch.

13.2.0 nearly shipped a 101 MB source distribution. `data/policy/embed_cache.jsonl`
is a 224 MB RouterArena embedding cache living at the repo root; it is untracked, so
git never mentioned it, and the sdist exclude list covered `/scripts/`, `/tests/`
and a dozen other top-level directories but not `/data/`. PyPI's per-file limit is
100 MB, so the upload would have failed at the last step of a tag-triggered release
— or worse, squeaked under and permanently shipped an embedding cache to everyone
installing the router.

Nothing in the repo would have caught it. `git status` shows untracked files without
judgement, the test suite does not build artifacts, and the version bump touched
none of it. Only building the sdist and looking inside revealed it, which is why
that is now a test rather than a habit.

Enumerating today's scratch files in `pyproject.toml` fixes today. This fixes
tomorrow: a size ceiling and a shape assertion, so the next stray directory fails a
test instead of a release.
"""
from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# PyPI rejects any file over 100 MB. 25 MB leaves room to grow while staying far
# enough below that a surprise addition trips this and not the upload.
MAX_SDIST_MB = 25

# Shapes that are never publishable, whatever else the sdist grows. An exhaustive
# allowlist was tried first and was the wrong instrument: the sdist legitimately
# ships hooks/, guide/, docs/, commands/, config/, npm/ and packaging/, so a
# whitelist bit-rots into a chore and gets widened without thought. These patterns
# describe things that are working-tree state by nature.
FORBIDDEN_PATTERNS = (
    ".hypothesis",      # Hypothesis' per-machine example database
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "experiments",
    "test_uploads",
    ".venv",
)

FORBIDDEN_SUFFIXES = (".zip", ".bak", ".pyc", ".sqlite", ".db", ".jpg", ".png.bak")

# Nothing inside a published sdist should be this big; the cache that started this
# was 224 MB.
MAX_MEMBER_MB = 5


def _build_sdist(tmp_path: Path) -> Path:
    if shutil.which("uv") is None:
        pytest.skip("uv not available to build the sdist")
    out = tmp_path / "dist"
    proc = subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(out)],
        cwd=REPO, capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        pytest.skip(f"sdist build unavailable here: {proc.stderr[-300:]}")
    tarballs = list(out.glob("*.tar.gz"))
    assert tarballs, "build produced no sdist"
    return tarballs[0]


@pytest.mark.slow
def test_sdist_is_small_enough_to_publish(tmp_path):
    sdist = _build_sdist(tmp_path)
    size_mb = sdist.stat().st_size / (1024 * 1024)
    assert size_mb < MAX_SDIST_MB, (
        f"sdist is {size_mb:.1f} MB (limit {MAX_SDIST_MB} MB, PyPI rejects >100 MB). "
        "Something large was added at the repo root and is not excluded in "
        "[tool.hatch.build.targets.sdist]."
    )


@pytest.mark.slow
def test_sdist_contains_no_working_tree_state(tmp_path):
    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist) as tf:
        names = tf.getnames()
    offenders = [
        n for n in names
        if any(pat in n for pat in FORBIDDEN_PATTERNS)
        or n.endswith(FORBIDDEN_SUFFIXES)
    ]
    assert not offenders, (
        f"sdist would publish working-tree state: {sorted(offenders)[:8]}. "
        "Exclude it in [tool.hatch.build.targets.sdist]."
    )


@pytest.mark.slow
def test_no_single_member_is_enormous(tmp_path):
    """The size ceiling catches the total; this catches the cause.

    A 224 MB embedding cache compresses well enough that a total-size check alone
    can pass while the artifact is still absurd.
    """
    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist) as tf:
        big = [
            (m.name, m.size / (1024 * 1024))
            for m in tf.getmembers() if m.size > MAX_MEMBER_MB * 1024 * 1024
        ]
    assert not big, (
        "sdist members over "
        f"{MAX_MEMBER_MB} MB: {[(n, round(mb, 1)) for n, mb in big]}"
    )


@pytest.mark.slow
def test_the_package_itself_is_still_present(tmp_path):
    """The counterpart. Over-excluding shipped a wheel with no package in 13.0.0,
    because `uv build` builds the wheel FROM the sdist — so a guard that only
    checks for absence would happily pass an empty release."""
    sdist = _build_sdist(tmp_path)
    with tarfile.open(sdist) as tf:
        names = tf.getnames()
    for required in (
        "src/llm_router/__init__.py",
        "src/llm_router/router.py",
        "src/llm_router/agents/session.py",
        "src/llm_router/data/semantic_centroids.json",
    ):
        assert any(n.endswith(required) for n in names), (
            f"{required} is missing from the sdist — the wheel is built from this"
        )
