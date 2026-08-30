"""Regression test for GH#61.

`llm-router` with no arguments is documented to start the MCP stdio server —
that's correct and expected. But `cli.py::main()`'s dispatch was a flat
`if/elif` chain over ~45 literal subcommand names with no branch for an
*unrecognized* one — the final `else` was the same MCP-server startup. So
typing any subcommand that wasn't in that list (a typo, `help`, `health`, ...)
silently launched the full MCP server on stdio and hung forever waiting for
JSON-RPC input, since there's no MCP client attached in an interactive
terminal.

WHY THE SUBPROCESS TEST HAS A HARD TIMEOUT

Before the fix, `llm-router nosuchcmd` never exits — it blocks on stdio
forever. A naive `subprocess.run(...)` with no timeout would therefore hang
this *test* forever too, which is exactly the bug the issue reports, just
relocated into CI. `timeout=` turns that hang into a prompt `TimeoutExpired`
so the bug registers as a fast, loud test failure instead of a stuck runner.

WHY "NO MCP STARTUP OUTPUT" IS THE LOAD-BEARING ASSERTION

An exit-code-only test (`returncode == 2`) would falsely pass against a
"fixed" version that still starts the MCP server and then merely dies with
exit code 2 for some unrelated reason — it would not actually prove the
server never started. Asserting the known MCP-startup log lines are absent
from stdout/stderr is what actually pins "unrecognized command never reaches
the server code path."
"""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

import pytest

from llm_router import cli

# Generous but bounded: a passing run returns almost instantly; a regression
# to the old behavior would otherwise hang forever, so this timeout is what
# converts that hang into a fast, deterministic test failure.
_TIMEOUT_S = 10

# Substrings that only appear once the MCP stdio server has actually started
# (see llm_router/server.py's startup banner / ensemble warm-up logging, and
# the reporter's own repro transcript in GH#61).
_MCP_STARTUP_MARKERS = (
    "Local LLM platforms detected",
    "anyio.run",
    "run_stdio_async",
    "mcpserver",
)


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "llm_router.cli", *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    )


def test_unknown_subcommand_errors_fast_with_no_server_startup():
    try:
        result = _run_cli(["nosuchcmd"])
    except subprocess.TimeoutExpired:
        pytest.fail(
            "`llm-router nosuchcmd` did not exit within "
            f"{_TIMEOUT_S}s — it hung, almost certainly because the "
            "unrecognized subcommand fell through to the MCP stdio server "
            "startup (GH#61)."
        )

    assert result.returncode == 2, (
        "unknown subcommand must exit 2, got "
        f"{result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "unknown command" in result.stderr.lower(), (
        f"expected an 'unknown command' message on stderr, got: {result.stderr!r}"
    )

    combined = result.stdout + result.stderr
    for marker in _MCP_STARTUP_MARKERS:
        assert marker not in combined, (
            f"found MCP-server startup marker {marker!r} in output — the "
            "unrecognized subcommand still reached the server startup path "
            f"instead of erroring out.\nstdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}"
        )


def test_no_args_still_reaches_the_mcp_server_start_path(monkeypatch):
    """The documented no-args contract (`llm-router` alone starts the MCP
    stdio server) must survive the GH#61 fix. This is a unit test, not a
    subprocess test — it mocks `llm_router.server.main` so it never actually
    blocks on stdio; it only proves the dispatch reaches that call.
    """
    monkeypatch.setattr(sys, "argv", ["llm-router"])
    with mock.patch("llm_router.server.main") as mock_server_main:
        cli.main()
    mock_server_main.assert_called_once_with()


def test_help_and_version_flags_still_work():
    help_result = _run_cli(["--help"])
    assert help_result.returncode == 0
    assert "llm_router" in help_result.stdout or "llm-router" in help_result.stdout

    version_result = _run_cli(["--version"])
    assert version_result.returncode == 0
    assert "v" in version_result.stdout.lower()
