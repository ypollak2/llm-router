"""Regression: GH#43 — the DIRECT_EXECUTION disclosure must reach PyPI users.

#36 was closed by documenting `LLM_ROUTER_DIRECT_EXECUTION` in SECURITY.md.
But pyproject's sdist `exclude` lists SECURITY.md under "Private / internal
docs — never publish", and README.md contained zero occurrences of the name —
it referenced the file only by a relative link that does not resolve from an
installed package.

Net effect for `pip install llm-routing`: a default-on feature that hands a
local model write_file/edit_file/run_command unsupervised, and no shipped text
naming it or its off switch.

These tests assert the disclosure is present in a file that actually ships, so
a future exclude-list edit fails CI rather than silently un-shipping it again.
"""
from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_MARKER = "LLM_ROUTER_DIRECT_EXECUTION"
_OFF_SWITCH = "LLM_ROUTER_DIRECT_EXECUTION=false"


def test_readme_documents_direct_execution():
    """README ships in every artifact, so the disclosure has to live there."""
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    assert _MARKER in readme, (
        "README.md must name LLM_ROUTER_DIRECT_EXECUTION — SECURITY.md is "
        "excluded from the sdist, so a relative link to it is invisible to "
        "anyone who installed from PyPI."
    )


def test_readme_states_default_on_and_names_off_switch():
    """Naming the variable is not enough — a user needs 'it is on' and 'turn it off'."""
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index(_MARKER):]
    assert _OFF_SWITCH in section, "README must give the exact off switch"
    lowered = section[:4000].lower()
    assert "default" in lowered and " on" in lowered, (
        "README must state that the feature is default-ON"
    )


def test_readme_discloses_the_granted_tools():
    """The material fact is unsupervised shell, not just 'a feature exists'."""
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index(_MARKER):][:4000]
    for tool in ("write_file", "edit_file", "run_command"):
        assert tool in section, f"README must disclose that {tool} is granted"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import subprocess

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
def test_sdist_ships_the_disclosure(built):
    _, sdist = built
    with tarfile.open(sdist) as tar:
        # README.md specifically, NOT "any shipped .md": CHANGELOG.md already
        # mentions the variable, and a changelog line is not a disclosure —
        # nobody reads it to learn what their install is doing right now.
        # Top-level README only — the sdist also carries nested READMEs
        # (e.g. _quarantined_tests/README.md) that would match a bare suffix
        # test and make this assertion pass against the wrong file.
        readmes = [
            n for n in tar.getnames()
            if n.endswith("/README.md") and n.count("/") == 1
        ]
        assert readmes, "sdist ships no top-level README.md"
        body = tar.extractfile(readmes[0]).read()
    assert _MARKER.encode() in body, (
        f"the SDIST's README.md does not mention {_MARKER}. Either un-exclude "
        f"the file that carries it, or mirror the section into README.md."
    )
    assert _OFF_SWITCH.encode() in body, "sdist README omits the off switch"


@pytest.mark.slow
def test_wheel_ships_the_disclosure(built):
    """The wheel is what `pip install` actually unpacks."""
    wheel, _ = built
    zf = zipfile.ZipFile(wheel)
    # The wheel carries README.md as the long_description inside METADATA.
    meta = [n for n in zf.namelist() if n.endswith("METADATA")]
    assert meta, "wheel has no METADATA"
    body = zf.read(meta[0])
    assert _MARKER.encode() in body, (
        f"the WHEEL's METADATA (the rendered README) does not mention {_MARKER} "
        f"— this is the text `pip show` and the PyPI project page display."
    )
    assert _OFF_SWITCH.encode() in body, "wheel METADATA omits the off switch"
