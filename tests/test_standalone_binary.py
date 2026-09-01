"""The standalone binary must be buildable, honest, and self-sufficient (task 35).

Python 3.11+ is the prerequisite that keeps llm-router out of the hands of the
audiences it most needs — Cursor users, Claude Desktop users, and the entire
JS ecosystem, none of whom will install a Python toolchain to save money on
tokens. The binary removes it.

These tests do not build a binary; that takes ~40 s per platform and belongs in
CI. They guard the decisions the build depends on, each of which was learned the
expensive way during the spike.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "packaging" / "llm-router.spec"
WORKFLOW = REPO / ".github" / "workflows" / "binary.yml"


def test_the_spec_exists_and_is_onedir():
    """Onefile unpacks its archive on EVERY invocation.

    The hooks run on every prompt and every tool call against a ~313 MB
    collected tree. Measured: venv python 0.04 s, onedir binary 0.08 s. Onefile
    pays an unpack per invocation and no tuning changes that, because the cost
    IS the unpack.
    """
    assert SPEC.is_file(), "no PyInstaller spec"
    src = SPEC.read_text()
    assert "COLLECT(" in src, "not a onedir build — COLLECT is what makes it one"
    assert "exclude_binaries=True" in src, (
        "EXE does not exclude binaries, which makes this a onefile build"
    )


def test_litellm_is_explicitly_collected():
    """The single most expensive lesson of the spike.

    cli.py imports litellm lazily — good design, and it defeats PyInstaller's
    static analysis completely. The first build produced a WORKING binary with
    litellm entirely absent; it ran fine until something needed pricing data.
    collect_all is required because the runtime also reads a 1.3 MB JSON.
    """
    src = SPEC.read_text()
    assert "collect_all" in src
    for pkg in ("litellm", "tiktoken"):
        assert pkg in src, f"{pkg} is not collected; it will be silently missing"


def test_hook_scripts_travel_with_the_binary():
    """Hooks live inside the package and are copied out at install time.

    Without them bundled, a binary can install its MCP server and no hooks at
    all — the manual-tools-only degradation, silently.
    """
    src = SPEC.read_text()
    assert "llm_router/hooks" in src, "hook scripts are not bundled"
    assert "llm_router/rules" in src, "rules are not bundled"


def test_upx_is_disabled():
    """UPX corrupts signed macOS binaries and trips Gatekeeper.

    A compression win that makes the artifact refuse to launch is not a win,
    and task 36 (notarisation) depends on this staying off.
    """
    src = SPEC.read_text()
    assert re.search(r"upx\s*=\s*False", src), "UPX is enabled somewhere in the spec"
    assert not re.search(r"upx\s*=\s*True", src)


# ── the frozen hook contract ──────────────────────────────────────────────────


def test_installer_knows_it_is_frozen():
    from llm_router import install_hooks as ih

    assert hasattr(ih, "is_frozen")
    # Not frozen under pytest, so the normal interpreter path must be taken.
    assert ih.is_frozen() is False
    assert "run-hook" not in ih._python_exe()


def test_frozen_installs_route_hooks_through_run_hook(monkeypatch):
    """sys.executable IS the binary under PyInstaller.

    Writing `<binary> /path/to/hook.py` into settings.json would hand the CLI an
    unknown argument on every prompt. The frozen build must register
    `<binary> run-hook <path>` instead — which is the whole reason a machine
    with no Python still gets auto-routing rather than only the MCP tools.
    """
    from llm_router import install_hooks as ih

    monkeypatch.setattr(ih, "is_frozen", lambda: True)
    monkeypatch.setattr(ih.sys, "executable", "/opt/llm-router/llm-router")

    prefix = ih._python_exe()
    assert prefix == "/opt/llm-router/llm-router run-hook", prefix


def test_cli_exposes_run_hook(tmp_path):
    """The entry point the frozen installer writes must actually exist."""
    src = (REPO / "src" / "llm_router" / "cli.py").read_text()
    assert '"run-hook"' in src, "cli has no run-hook entry point"
    assert "runpy" in src, "run-hook does not execute the script"


def test_run_hook_executes_a_script(tmp_path, capsys):
    """End to end, in-process: the hook's own __main__ must run."""
    import sys

    from llm_router.cli import main

    hook = tmp_path / "probe-hook.py"
    hook.write_text("import sys\nprint('hook ran with', sys.argv[0])\n")

    argv = sys.argv
    try:
        sys.argv = ["llm-router", "run-hook", str(hook)]
        main()
    finally:
        sys.argv = argv

    assert "hook ran with" in capsys.readouterr().out


def test_run_hook_fails_open(tmp_path, capsys):
    """A hook that raises must not take the host's turn down.

    Same fail-open contract the hooks apply internally — a reporting or routing
    hook is never worth breaking the session over.
    """
    import sys

    from llm_router.cli import main

    hook = tmp_path / "bad-hook.py"
    hook.write_text("raise RuntimeError('boom')\n")

    argv = sys.argv
    try:
        sys.argv = ["llm-router", "run-hook", str(hook)]
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0, "a failing hook must not fail the turn"
    finally:
        sys.argv = argv


# ── CI ────────────────────────────────────────────────────────────────────────


def test_workflow_covers_every_platform_we_claim():
    wf = WORKFLOW.read_text()
    for target in ("macos-14", "macos-13", "ubuntu-latest", "windows-latest"):
        assert target in wf, f"no binary is built for {target}"


def test_workflow_smoke_tests_what_the_spike_got_wrong():
    """A binary that builds and cannot run is worse than no binary: it ships.

    The smoke test must check the two things that were silently absent from the
    first working build.
    """
    wf = WORKFLOW.read_text()
    assert "model_prices_and_context_window_backup.json" in wf, (
        "CI does not verify litellm's data file, which was missing from a build "
        "that otherwise passed --version and --help"
    )
    assert "run-hook" in wf, "CI never exercises the frozen hook path"
    assert "--version" in wf


def test_binary_build_is_not_on_every_push():
    """~40 s per platform over a 565 MB tree, and nothing consumes an untagged
    artifact."""
    wf = WORKFLOW.read_text()
    on_block = wf.split("on:")[1].split("permissions:")[0]
    assert "tags:" in on_block
    assert "pull_request" not in on_block
