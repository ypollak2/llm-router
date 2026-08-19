"""Test the ``llm_router welcome`` CLI subcommand.

This command exists because Claude Code SessionStart hooks cannot surface
output to the user's terminal — both stdout and stderr from a hook go to
Claude (as additionalContext / system reminders) but never reach the
terminal scrollback in the user's UI, and ``/dev/tty`` is detached in
the hook subprocess (``OSError: Errno 6 Device not configured``).

The path forward for a user-visible startup banner is a shell wrapper
that runs ``llm_router welcome`` BEFORE ``claude`` itself — the banner lands
in the terminal directly because the wrapper runs in the user's shell,
not in Claude Code's hook sandbox.

These tests pin the CLI contract so future refactors don't break the
shell-wrapper integration.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest


def _import_cmd_welcome():
    from llm_router.commands.welcome import cmd_welcome
    return cmd_welcome


def test_welcome_default_prints_painterly_banner() -> None:
    """``llm_router welcome`` with no args prints the full painterly banner.

    Verifies that the output contains the wordmark line and has the
    expected order of magnitude of ANSI escape sequences — i.e. the
    full painting rendered, not a fallback string.
    """
    cmd_welcome = _import_cmd_welcome()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_welcome([])
    out = buf.getvalue()

    assert rc == 0
    # The painterly wordmark spaces letters apart (`C  H  U  Z  O  M`).
    # Strip ANSI and whitespace to check the brand letters appear in order.
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    plain_squashed = re.sub(r"\s+", "", plain).upper()
    assert "LLM_ROUTER" in plain_squashed, (
        "wordmark missing from full painterly banner"
    )
    # The painterly art uses 24-bit truecolor — expect many ESC sequences.
    esc = "\x1b["
    assert out.count(esc) > 40, (
        f"expected the full painterly banner with many ANSI escapes; "
        f"got {out.count(esc)} (looks like a fallback rendered)"
    )


def test_welcome_compact_prints_single_line() -> None:
    """``llm_router welcome --compact`` prints a short statusline-style variant.

    Designed for shell-wrapper integration in ~/.zshrc so the banner
    appears as a single discreet line above the claude TUI rather than
    dozens of lines of painting.
    """
    cmd_welcome = _import_cmd_welcome()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_welcome(["--compact"])
    out = buf.getvalue()

    assert rc == 0
    assert "LLM_ROUTER" in out
    # Compact must be small — strict bound to catch refactors that
    # accidentally route --compact to the full banner.
    assert len(out) < 1000, (
        f"--compact output too large ({len(out)} bytes); should be a "
        "one-line statusline variant"
    )
    # Strip ANSI to count visible newlines (the wordmark line may
    # itself wrap into 1-2 logical lines depending on styling).
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert plain.count("\n") <= 2, (
        "compact variant should be effectively single-line"
    )


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_welcome_help_returns_zero_without_printing_banner(flag: str) -> None:
    """``-h`` / ``--help`` prints the docstring, not the painting."""
    cmd_welcome = _import_cmd_welcome()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cmd_welcome([flag])
    out = buf.getvalue()

    assert rc == 0
    # Help text mentions the command name & SessionStart context.
    assert "welcome" in out.lower()
    # ANSI escape count should be small (no painting).
    assert out.count("\x1b[") < 5


def test_welcome_subcommand_is_registered_in_cli() -> None:
    """The dispatch path in ``cli.py`` must route ``welcome`` to
    ``cmd_welcome`` — without this, the shell-wrapper invocation
    ``llm_router welcome --compact`` silently does nothing and the
    user-visible banner vanishes from the launch path.
    """
    from pathlib import Path
    cli_src = (Path(__file__).resolve().parent.parent / "src" / "llm_router" / "cli.py").read_text()
    assert 'args[0] == "welcome"' in cli_src, (
        "welcome subcommand dispatch missing from cli.py — "
        "`llm_router welcome` will be treated as unknown"
    )
    assert "from llm_router.commands.welcome import cmd_welcome" in cli_src, (
        "cli.py does not import cmd_welcome — dispatch will fail at runtime"
    )
