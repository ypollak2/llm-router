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
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


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
    """macos-13 queued for hours on every run across a day and completed on none
    (and was later retired; the Intel job now runs on macos-15-intel).

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


# ── postinstall wires the hosts: `<bin> install --auto` (v16 P2.12, P212-2) ──
#
# These run the real install.js under node against a stub binary. Reading the JS
# for a string cannot tell a call that runs from one that sits in dead code, so
# every assertion here is about what node actually did: the argv the stub saw,
# the exit code npm would see, and what was printed.

#: Records one line per invocation (args joined by a tab), answers --version,
#: and for `install` writes a receipt into $HOME the way the real binary does,
#: prints the one `receipt:` line and exits $STUB_EXIT.
_STUB = """#!/bin/sh
( IFS="$(printf '\\t')"; printf '%s\\n' "$*" ) >> "$STUB_ARGV_LOG"
if [ "$1" = "--version" ]; then echo "llm-router 15.4.0"; exit 0; fi
status=wired
[ "${STUB_EXIT:-0}" = "0" ] || status=error
mkdir -p "$HOME/.llm-router"
printf '{"hosts": {"codex": {"status": "%s"}}}\\n' "$status" > "$HOME/.llm-router/install-receipt.json"
echo "LLM Router - One-Command Install"
echo "receipt: deadbeef hosts=2/3 version=15.4.0"
[ "$status" = "wired" ] || echo "Codex CLI: install failed: PermissionError: denied" >&2
exit "${STUB_EXIT:-0}"
"""

#: Preload for runInstaller-only runs: any network call is a test failure.
_NO_NET = """
const blocked = () => { throw new Error('network blocked by test'); };
for (const m of ['http', 'https']) { const mod = require(m); mod.get = mod.request = blocked; }
"""

#: Preload for the whole postinstall: https.get serves a local tarball instead of
#: GitHub, and every URL asked for is logged so the test can count them.
_FAKE_RELEASE = """
const fs = require('fs');
const { EventEmitter } = require('events');
require('http').get = require('http').request = () => { throw new Error('http blocked by test'); };
const https = require('https');
https.request = () => { throw new Error('https.request blocked by test'); };
https.get = (url, opts, cb) => {
  fs.appendFileSync(process.env.TEST_URL_LOG, String(url) + '\\n');
  const res = fs.createReadStream(process.env.TEST_TARBALL);
  res.statusCode = 200;
  res.headers = {};
  process.nextTick(() => cb(res));
  return new EventEmitter();
};
"""


def _node() -> str:
    """node, or a FAILURE in CI: a skipped test passes vacuously, and the
    ubuntu runners ship node, so a missing one there is a broken runner."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run the npm postinstall tests")
        pytest.skip("node not installed")
    return node


@pytest.fixture
def npm_env(tmp_path):
    """A copy of the npm package, a stub binary and a fixture HOME.

    The package is copied so the postinstall's `vendor/` lands in tmp, never in
    the repo's npm/ (which test_binary_is_not_vendored_into_the_package guards).
    """
    if sys.platform == "win32":
        pytest.skip("the stub binary is a POSIX shell script")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for name in ("install.js", "package.json"):
        shutil.copy(NPM / name, pkg / name)
    home = tmp_path / "home"
    home.mkdir()
    stub = tmp_path / "fake-llm-router"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    (tmp_path / "no_net.js").write_text(_NO_NET)
    (tmp_path / "fake_release.js").write_text(_FAKE_RELEASE)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "STUB_ARGV_LOG": str(tmp_path / "argv.log"),
    }
    return {"tmp": tmp_path, "pkg": pkg, "home": home, "stub": stub, "env": env}


def _invocations(e) -> list[list[str]]:
    log = Path(e["env"]["STUB_ARGV_LOG"])
    if not log.exists():
        return []
    return [line.split("\t") for line in log.read_text().splitlines()]


def _run_installer(e, bin_path=None, **extra_env):
    """`require('install.js').runInstaller(bin, process.env)` under real node."""
    code = "process.exitCode = require(process.argv[1]).runInstaller(process.argv[2], process.env);"
    return subprocess.run(
        [_node(), "-r", str(e["tmp"] / "no_net.js"), "-e", code,
         str(e["pkg"] / "install.js"), str(bin_path or e["stub"])],
        cwd=e["tmp"], env={**e["env"], **extra_env},
        capture_output=True, text=True, timeout=60,
    )


def test_postinstall_runs_install_auto_and_echoes_the_receipt(npm_env):
    r = _run_installer(npm_env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _invocations(npm_env) == [["install", "--auto"]]
    assert "llm-routing: receipt: deadbeef hosts=2/3 version=15.4.0" in r.stdout
    # Only the receipt line is echoed, not the installer's banner.
    assert "One-Command Install" not in r.stdout
    # Hosts are wired now; pointing at the plugin would register its hooks twice.
    assert "/plugin install" not in r.stdout


def test_opt_out_never_calls_the_installer_and_says_why(npm_env):
    r = _run_installer(npm_env, LLM_ROUTER_NO_AUTO_INSTALL="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _invocations(npm_env) == [], "the opt-out still ran the installer"
    assert "LLM_ROUTER_NO_AUTO_INSTALL" in r.stdout
    assert "llm-router install" in r.stdout, "no manual command offered after the opt-out"
    # Nothing is wired, so the plugin is still a valid alternative.
    assert "/plugin install llm-router@ypollak2" in r.stdout
    assert not (npm_env["home"] / ".llm-router").exists()


def test_opt_out_is_not_triggered_by_an_empty_or_zero_value(npm_env):
    for value in ("", "0"):
        npm_env["env"]["STUB_ARGV_LOG"] = str(npm_env["tmp"] / f"argv-{value or 'empty'}.log")
        r = _run_installer(npm_env, LLM_ROUTER_NO_AUTO_INSTALL=value)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _invocations(npm_env) == [["install", "--auto"]], value


def test_installer_failure_warns_but_npm_install_still_succeeds(npm_env):
    r = _run_installer(npm_env, STUB_EXIT="1")
    assert r.returncode == 0, "a host-wiring failure failed the whole npm install"
    assert _invocations(npm_env) == [["install", "--auto"]]
    assert "WARNING" in r.stderr and "exit code 1" in r.stderr, r.stderr
    assert "llm-router install" in r.stderr, "no manual command offered"
    # The installer's own reason reaches the user, and its receipt stays put.
    assert "PermissionError: denied" in r.stderr
    assert "receipt: deadbeef" in r.stdout
    receipt = json.loads((npm_env["home"] / ".llm-router" / "install-receipt.json").read_text())
    assert receipt["hosts"]["codex"]["status"] == "error"


def test_an_installer_that_cannot_start_still_exits_zero(npm_env):
    r = _run_installer(npm_env, bin_path=npm_env["tmp"] / "no-such-binary")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARNING" in r.stderr and "ENOENT" in r.stderr, r.stderr
    assert "llm-router install" in r.stderr


def test_a_spawn_that_throws_still_exits_zero(npm_env):
    """The injected spawner raising (not returning an error) must not escape
    to main()'s catch, which would exit 1 and fail the npm install."""
    code = ("process.exitCode = require(process.argv[1]).runInstaller("
            "'llm-router', process.env, () => { throw new Error('spawn exploded'); });")
    r = subprocess.run(
        [_node(), "-r", str(npm_env["tmp"] / "no_net.js"), "-e", code, str(npm_env["pkg"] / "install.js")],
        cwd=npm_env["tmp"], env=npm_env["env"], capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "spawn exploded" in r.stderr and "llm-router install" in r.stderr


def test_the_whole_postinstall_downloads_once_then_wires_hosts(npm_env):
    """`node install.js` exactly as npm runs it, with GitHub replaced by a
    local tarball: one download, then `--version`, then `install --auto`."""
    targets = {
        ("darwin", "arm64"): "llm-router-macos-arm64.tar.gz",
        ("darwin", "x86_64"): "llm-router-macos-x86_64.tar.gz",
        ("linux", "x86_64"): "llm-router-linux-x86_64.tar.gz",
    }
    import platform as _platform

    asset = targets.get((sys.platform, _platform.machine()))
    if asset is None:
        pytest.skip(f"no published binary for {sys.platform}/{_platform.machine()}")
    e = npm_env
    tarball = e["tmp"] / asset
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(e["stub"], arcname="llm-router/llm-router")
    url_log = e["tmp"] / "urls.log"

    r = subprocess.run(
        [_node(), "-r", str(e["tmp"] / "fake_release.js"), "install.js"],
        cwd=e["pkg"], env={**e["env"], "TEST_TARBALL": str(tarball), "TEST_URL_LOG": str(url_log)},
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    version = _package_json()["version"]
    assert url_log.read_text().splitlines() == [
        f"https://github.com/ypollak2/llm-router/releases/download/v{version}/{asset}"
    ]
    assert _invocations(e) == [["--version"], ["install", "--auto"]]
    assert "llm-routing: receipt: deadbeef" in r.stdout
    assert "next step ->" not in r.stdout, "the manual next-step hint is still printed"
