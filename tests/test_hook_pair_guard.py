"""CI guard (F53): a stamped hook cannot ship unmirrored or without a version bump.

INCIDENT: PR #568 changed src/llm_router/hooks/session-end.py but not its mirror
hooks/session-end.py, and left `# llm_router-hook-version: 35` alone. The older tests
all passed: test_hook_version_tracks_content.py only watches auto-route.py against a
gitignored baseline (its tests skip as an "upstream zero-tolerance-audit artifact"),
and test_red2_6_01_hook_content_drift.py / test_install_hooks.py exercise the
installer on tmp files, never the repo's own pair of copies.

This file depends on neither. Rule (a): the two copies are byte-identical. Rule (b):
changed content needs a strictly higher stamp than on the merge base with origin/main.

Set LLM_ROUTER_REQUIRE_HOOK_BASE=1 (CI does) to turn "no git history" from a skip
into a failure, so the check cannot silently not run.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DIRS = ("hooks", "src/llm_router/hooks")
_STAMP = re.compile(rb"llm_router-hook-version:\s*(\d+)")


def _stamped() -> list[str]:
    """Repo-relative paths of every stamped file in either hook directory."""
    out = []
    for d in _DIRS:
        for p in sorted((_ROOT / d).glob("*")):
            if p.is_file() and _STAMP.search(p.read_bytes()):
                out.append(f"{d}/{p.name}")
    return out


def _stamp(data: bytes) -> int:
    return int(_STAMP.search(data).group(1))


def _git(*args: str) -> bytes | None:
    try:
        r = subprocess.run(["git", *args], cwd=_ROOT, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def test_stamped_hooks_were_found():
    """An empty set would pass every check below."""
    assert len(_stamped()) >= 10, _stamped()


def test_mirrored_hooks_are_byte_identical():
    """Rule (a). Only a stamped file present in BOTH directories is a pair; a file kept
    in one place (e.g. codex-post-tool.py, src only) is not a pair, so nothing is listed
    by hand."""
    stamped = _stamped()
    pairs = [
        n for n in {Path(s).name for s in stamped} if all((_ROOT / d / n).is_file() for d in _DIRS)
    ]
    assert pairs, "no mirrored pairs found; the guard would check nothing"
    bad = [
        n
        for n in sorted(pairs)
        if (_ROOT / _DIRS[0] / n).read_bytes() != (_ROOT / _DIRS[1] / n).read_bytes()
    ]
    assert not bad, (
        f"hooks/ and src/llm_router/hooks/ differ for {bad}. Edit one copy, then "
        f"`cp` it over the other (and bump llm_router-hook-version)."
    )


def test_changed_hooks_bumped_their_version():
    """Rule (b), against the merge base with origin/main."""
    strict = os.environ.get("LLM_ROUTER_REQUIRE_HOOK_BASE") == "1"

    def unavailable(why: str):
        if strict:
            pytest.fail(f"LLM_ROUTER_REQUIRE_HOOK_BASE=1 but cannot compare: {why}")
        pytest.skip(f"cannot compare to merge base: {why}")

    base = _git("merge-base", "HEAD", "origin/main")
    if base is None:
        unavailable("git or origin/main missing, or shallow checkout with no merge base")
    base_sha = base.decode().strip()

    checked, bad = 0, []
    for path in _stamped():
        old = _git("show", f"{base_sha}:{path}")
        if old is None or not _STAMP.search(old):
            continue  # new file (or newly stamped) on this branch: nothing to bump from
        checked += 1
        new = (_ROOT / path).read_bytes()
        if new != old and _stamp(new) <= _stamp(old):
            bad.append(f"{path}: content changed but stamp is {_stamp(new)} (base {_stamp(old)})")
    if checked == 0:
        unavailable(f"no stamped file readable at merge base {base_sha[:10]}")
    assert not bad, "bump `# llm_router-hook-version:` for:\n" + "\n".join(bad)
