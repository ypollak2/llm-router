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
    # Assets come from two places now: the gating matrix, and the best-effort
    # macOS Intel job that sits outside the dependency chain. Reading only the
    # matrix would report a false mismatch for a platform that IS built.
    assets = {m["asset"] for m in wf["jobs"]["build"]["strategy"]["matrix"]["include"]}
    assets |= set(
        re.findall(r"(llm-router-[a-z0-9_-]+)\.(?:tar\.gz|zip)",
                   yaml.dump(wf["jobs"]["build-macos-x86"]))
    )

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


def test_release_upload_does_not_assume_a_release_exists():
    """A tag is not a release.

    `gh release upload` requires a release object, and pushing a tag does not
    create one. The first real tag build hit exactly this: every binary built
    correctly and the upload step would have failed with nothing to attach to.
    """
    wf = WORKFLOW.read_text()
    assert "gh release create" in wf, (
        "the workflow uploads to a release it never creates; a plain tag push "
        "leaves nothing to upload to"
    )
    assert "gh release view" in wf, (
        "no existence check before creating — a re-run would fail on the "
        "already-created release"
    )


def test_the_release_job_can_actually_write():
    """Creating a release and uploading assets need contents: write.

    The workflow declared `contents: read` and nothing overrode it, so
    `gh release create` and `gh release upload` failed on every tag build —
    after all four binaries had been built, at the very last step. Two tags,
    v13.1.0 and v13.1.1, produced correct binaries and attached none of them.

    A permission that is wrong fails at the end of the job, not the start,
    which is the most expensive place for it to be wrong.
    """
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text())
    job_perms = wf["jobs"]["build"].get("permissions", {})
    assert job_perms.get("contents") == "write", (
        "the build job cannot write releases, so no asset can ever attach; "
        f"got {job_perms}"
    )
    # Still least-privilege at the top: only the job that needs it escalates.
    assert wf["permissions"]["contents"] == "read"


def test_upload_does_not_assume_every_platform_produces_a_tarball():
    """Windows packages a .zip and no .tar.gz.

    The upload step named both files with a `.tar.gz`-only fallback, so on
    Windows the primary AND the fallback referenced a file that does not exist.
    It failed with the other three platforms already uploaded — a partial
    release, which is worse than none, because the assets that did land make it
    look finished.
    """
    wf = WORKFLOW.read_text()
    step = wf.split("Attach to the release")[1].split("upload-artifact")[0]

    assert "artifacts[@]" in step, "artifacts are not passed as a discovered list"
    # Comments may still discuss nullglob; what matters is that no code uses it.
    code = "\n".join(
        line for line in step.splitlines() if not line.lstrip().startswith("#")
    )
    assert "nullglob" not in code, (
        "nullglob only drops patterns containing wildcards, and `<asset>.tar.gz` "
        "has none — a missing file stays in the array as a literal. It broke "
        "Linux, which had been working. Test existence instead."
    )
    assert "[ -f " in step, "the upload does not test whether each artifact exists"


def test_upload_selection_is_correct_for_each_platform():
    """Run the workflow's own selection logic against each platform's outputs.

    Reading the shell and reasoning about it is what produced three wrong
    fixes in a row. This executes it.
    """
    import subprocess
    import tempfile

    cases = {
        "linux": ([".tar.gz"], 1),
        "macos": ([".tar.gz", ".zip"], 2),
        "windows": ([".zip"], 1),
    }
    for platform, (built, expected) in cases.items():
        with tempfile.TemporaryDirectory() as d:
            asset = f"llm-router-{platform}"
            for ext in built:
                Path(d, asset + ext).touch()
            script = (
                'artifacts=(); '
                f'for f in "{asset}.tar.gz" "{asset}.zip"; do '
                '[ -f "$f" ] && artifacts+=( "$f" ); done; '
                'echo ${#artifacts[@]}'
            )
            out = subprocess.run(
                ["bash", "-c", script], cwd=d, capture_output=True, text=True
            ).stdout.strip()
            assert out == str(expected), (
                f"{platform} builds {built} but the upload selected {out} "
                f"artifacts, expected {expected}"
            )


# ── npm publishing belongs to the release, not to a terminal ──────────────────


def test_npm_publish_is_part_of_the_release_workflow():
    """13.1.4 was published by hand before any asset existed.

    Every install 404'd on every platform, and npm can never republish a
    version — only deprecate it. The recovery was to rush the tag through and
    hope nobody installed in the window.
    """
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text())
    assert "publish-npm" in wf["jobs"], "npm publishing is still a manual step"


def test_npm_publish_cannot_run_before_the_binaries():
    """Ordering enforced by the graph, not by remembering it."""
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text())
    job = wf["jobs"]["publish-npm"]
    assert job["needs"] == "build" or "build" in job["needs"], (
        "publish-npm does not depend on the binary build, so it could publish "
        "before a single asset exists — exactly what happened with 13.1.4"
    )
    assert "refs/tags/v" in job["if"], "npm would publish on a non-tag run"


def test_npm_publish_verifies_every_asset_it_could_request():
    """A platform with no asset is a 404 for that user, and they cannot be told
    to wait for a later publish of the same version."""
    wf = WORKFLOW.read_text()
    step = wf.split("Verify every asset")[1].split("- name: Publish")[0]

    assert "gh release view" in step, "the assets on the release are never read"
    assert "install.js" in step, (
        "the check does not derive its list from install.js, so a target added "
        "there could be published without a binary"
    )
    assert "exit 1" in step, "a missing asset does not stop the publish"


def test_npm_publish_checks_version_against_the_tag():
    """install.js turns package.json's version into the download URL.

    Publishing 13.1.4 from a v13.1.3 tag points every install at the wrong
    release.
    """
    wf = WORKFLOW.read_text()
    step = wf.split("Verify every asset")[1].split("- name: Publish")[0]
    assert "GITHUB_REF_NAME" in step and "package.json" in step


def test_the_package_ships_a_readme():
    """package.json listed README.md in `files` and the file did not exist.

    13.1.4 published with nothing for the npm page to render: a visitor saw
    three tiny files, no explanation, and no reason to believe a 3 kB package
    does anything. The package was correct and looked like a mistake, which for
    a first impression is the same thing.
    """
    readme = NPM / "README.md"
    assert readme.is_file(), "npm/README.md is missing; the npm page renders nothing"
    assert "README.md" in _package_json()["files"], (
        "README.md is not in `files`, so it will not be included in the tarball"
    )

    text = readme.read_text()
    # The single question a visitor asks about a 3 kB package.
    assert "3 kB" in text or "300 MB" in text, (
        "the README does not explain why the package is tiny, which is the "
        "first thing anyone looking at the code tab wonders"
    )


def test_a_scarce_runner_cannot_block_the_publish():
    """macos-13 queued for hours on every run across a day and completed on none.

    As a leg of the `build` matrix it was a hard block: `needs: build` waits for
    every leg to FINISH, and `always()` does not change that — it only decides
    whether the dependent runs once the wait is over. A permanently queued job
    therefore blocked the npm publish forever.

    It now builds outside the dependency chain and attaches when it lands.
    """
    import yaml

    wf = yaml.safe_load(WORKFLOW.read_text())

    matrix = wf["jobs"]["build"]["strategy"]["matrix"]["include"]
    gating = {m["os"] for m in matrix}
    assert "macos-13" not in gating, (
        "macos-13 is back in the gating matrix, so a queued Intel runner blocks "
        "the npm publish again"
    )

    assert "build-macos-x86" in wf["jobs"], "macOS Intel is no longer built at all"
    assert wf["jobs"]["build-macos-x86"].get("continue-on-error") is True

    needs = wf["jobs"]["publish-npm"]["needs"]
    assert "build-macos-x86" not in str(needs), (
        "publish-npm depends on the best-effort job, which reintroduces the block"
    )


def test_only_the_scarce_platform_is_optional():
    """Relaxing the wait must not relax what the release promises.

    Every platform except macOS Intel stays mandatory: publishing without a
    Linux or Windows binary would 404 for those users, and npm cannot
    republish a version to fix it.
    """
    wf = WORKFLOW.read_text()
    step = wf.split("Verify every asset")[1].split("- name: Publish")[0]

    assert "macos-x86_64" in step, "no platform is treated as optional"
    assert "exit 1" in step, "a missing mandatory asset no longer stops the publish"
    # The tolerated case must be narrow — a wildcard would excuse everything.
    assert "*macos-x86_64*" in step, (
        "the optional case is not scoped to macOS Intel specifically"
    )


def test_provenance_has_the_permission_it_requires():
    """`npm publish --provenance` mints a signed attestation via OIDC.

    Without id-token: write it fails at the very last step, after the binaries
    are already attached — the same shape as the contents: read bug two
    releases earlier. Both are permissions that fail at the END of a job, which
    is the most expensive place for them to be wrong.
    """
    import yaml

    wf_text = WORKFLOW.read_text()
    wf = yaml.safe_load(wf_text)
    perms = wf["jobs"]["publish-npm"]["permissions"]

    if "--provenance" in wf_text:
        assert perms.get("id-token") == "write", (
            "publish uses --provenance but the job cannot mint an OIDC token; "
            f"got {perms}"
        )


def test_npm_publish_uses_trusted_publishing_not_a_token():
    """v13.1.5-v13.1.7 all died with EOTP.

    The account's 2FA is "authorization and writes" and a classic token cannot
    bypass it, so `npm publish` asked CI for a one-time code -- after the
    provenance statement had already been written to a public transparency
    log. No pre-flight can catch that: `npm publish --dry-run` never contacts
    the registry, and `npm whoami` proves reads, not writes.

    Trusted publishing removes the token altogether: npm mints a short-lived
    credential from the GitHub OIDC token. That needs id-token: write and
    npm >= 11.5.1, which Node 20 does not bundle.
    """
    wf = WORKFLOW.read_text()
    step = wf.split("- name: Publish")[1]

    assert "NPM_TOKEN" not in step and "NODE_AUTH_TOKEN" not in step, (
        "the publish still uses a stored token, which the account's 2FA "
        "refuses at write time with EOTP"
    )
    assert "npm install -g npm@" in step, (
        "trusted publishing needs npm >= 11.5.1; Node 20 bundles npm 10"
    )
    upgrade = step.index("npm install -g npm@")
    real = step.index("npm publish")
    assert upgrade < real, "npm must be upgraded before the publish runs"

    import yaml

    job = yaml.safe_load(wf)["jobs"]["publish-npm"]
    assert job.get("permissions", {}).get("id-token") == "write", (
        "trusted publishing exchanges a GitHub OIDC token for npm credentials; "
        "without id-token: write there is nothing to exchange"
    )
