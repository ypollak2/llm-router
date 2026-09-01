"""The npm wrapper must stay in step with the binary workflow (task 37).

npm is how this reaches Cursor users, Claude Desktop users, and the JS
ecosystem — the audiences who will not install a Python toolchain to save money
on tokens. The wrapper downloads the standalone binary built by
.github/workflows/binary.yml, which means two files in different languages have
to agree about asset names, versions and platforms.

Nothing enforces that agreement at runtime: a mismatch surfaces as a 404 during
someone else's `npm install`, which is the worst possible place to find out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
NPM = REPO / "npm"
INSTALL_JS = NPM / "install.js"
WORKFLOW = REPO / ".github" / "workflows" / "binary.yml"


def _package_json() -> dict:
    return json.loads((NPM / "package.json").read_text())


def test_npm_name_matches_the_adr():
    """ADR 0001: one published name, `llm-routing`.

    `llm-router` on npm is squatted, and npm rejects names too similar after
    punctuation is stripped — `llm_router` and `llmrouter` both normalise to
    `llmrouter` and would be refused at publish despite looking free.
    """
    assert _package_json()["name"] == "llm-routing"


def test_npm_version_matches_pyproject():
    """The wrapper builds its download URL from its own version.

    A drift here points npm users at a release tag that does not exist.
    """
    import tomllib

    py = tomllib.load((REPO / "pyproject.toml").open("rb"))["project"]["version"]
    assert _package_json()["version"] == py, (
        f"npm says {_package_json()['version']}, pyproject says {py} — the "
        "postinstall would request a release tag that does not exist"
    )


def test_command_name_is_the_hyphenated_cli():
    """ADR 0001 keeps the command hyphenated; the package name differs
    deliberately, as with ripgrep/rg."""
    assert list(_package_json()["bin"]) == ["llm-router"]


def test_every_workflow_asset_is_downloadable():
    """The two files must name the same artifacts.

    This is the check that a mismatch would otherwise defer to a stranger's
    `npm install` failing with a 404.
    """
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text())
    assets = {m["asset"] for m in wf["jobs"]["build"]["strategy"]["matrix"]["include"]}

    js = INSTALL_JS.read_text()
    referenced = set(re.findall(r"(llm-router-[a-z0-9_-]+)\.(?:tar\.gz|zip)", js))

    assert assets == referenced, (
        f"asset names drifted.\n  built by CI: {sorted(assets)}\n"
        f"  fetched by npm: {sorted(referenced)}"
    )


def test_platform_map_covers_what_package_json_claims():
    """`os`/`cpu` in package.json tell npm which platforms may install.

    Claiming a platform the postinstall cannot serve turns a clean
    "unsupported" refusal into a failed install.
    """
    pkg = _package_json()
    js = INSTALL_JS.read_text()
    keys = set(re.findall(r"'(darwin|linux|win32)-(x64|arm64)'", js))
    mapped_os = {k[0] for k in keys}

    assert set(pkg["os"]) == mapped_os, (
        f"package.json claims os={sorted(pkg['os'])} but install.js maps "
        f"{sorted(mapped_os)}"
    )


def test_postinstall_verifies_the_binary_runs():
    """Extracting is not installing.

    A wrong-arch or Gatekeeper-quarantined binary extracts perfectly and then
    fails on first use, mid-session. The postinstall must execute it.
    """
    js = INSTALL_JS.read_text()
    assert "--version" in js, "postinstall never runs the binary it just installed"
    assert "quarantine" in js, (
        "no macOS Gatekeeper guidance; until the build is notarised (task 36) "
        "this is the most likely failure a mac user hits"
    )


def test_failures_are_fatal_and_offer_a_way_out():
    """A postinstall that swallows an error leaves a broken command on PATH."""
    js = INSTALL_JS.read_text()
    assert "process.exit(1)" in js
    assert "pip install llm-routing" in js, (
        "no fallback offered; a user whose platform has no binary is left stuck"
    )


def test_launcher_preserves_the_stdio_contract():
    """llm-router is an MCP stdio server.

    The host speaks to it over stdin/stdout, so the launcher must inherit those
    streams rather than pipe them — any buffering or re-encoding corrupts the
    protocol — and must propagate the child's exit code.
    """
    launcher = (NPM / "bin" / "llm-router.js").read_text()
    assert "stdio: 'inherit'" in launcher, (
        "the launcher pipes stdio, which corrupts the MCP stream"
    )
    assert "result.status" in launcher, "exit code is not propagated"


def test_binary_is_not_vendored_into_the_package():
    """Four platforms in one tarball would be >1 GB, three of them unusable."""
    pkg = _package_json()
    assert "vendor/" not in pkg.get("files", []), (
        "the binary is vendored; the package should fetch only the matching one"
    )
    assert not (NPM / "vendor").exists() or not any((NPM / "vendor").iterdir()), (
        "a built binary is sitting in npm/vendor and would be published"
    )


def test_binaries_reach_the_url_npm_downloads_from():
    """Building the artifact is not publishing it.

    upload-artifact attaches to the workflow RUN and expires after 14 days.
    install.js fetches from releases/download/v<version>/<asset>, which is a
    RELEASE asset — a different destination entirely. A workflow that only does
    the first builds perfect binaries that no `npm install` can ever reach, and
    the 404 lands on a stranger rather than in CI.
    """
    wf = WORKFLOW.read_text()
    js = INSTALL_JS.read_text()

    assert "releases/download" in js, "install.js no longer uses release assets"
    assert "gh release upload" in wf, (
        "the workflow never attaches binaries to the release, so every "
        "npm install would 404 on the URL install.js builds"
    )
    assert "startsWith(github.ref, 'refs/tags/v')" in wf, (
        "release upload is not gated on a tag; a dispatch build would try to "
        "upload to a release that does not exist"
    )
