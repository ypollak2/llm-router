"""T-CODEX-3: surface real Codex / Gemini-CLI stderr in router chain errors.

Background
----------
Before this fix, the dispatch loop raised
``RuntimeError("Codex exited 1: (response omitted)")`` regardless of WHY
Codex failed. That opaque marker is what made PR #39 expensive to
diagnose — the real failure (``"The 'gpt-4o-mini' model is not
supported when using Codex with a ChatGPT account"`` or ``"Not inside
a trusted directory…"``) was thrown away before reaching the router's
``chain_errors`` summary.

Contract
--------
``_format_subprocess_chain_error(agent, exit_code, content)`` returns a
single-line message of shape:

    ``"<agent> exited <code>: <first-informative-line>"``

* Strips leading blank lines.
* Returns the first non-empty line of ``content``.
* Truncates content to a small cap (default 200 chars) with a trailing
  marker so a multi-kilobyte traceback doesn't blow up the chain
  summary.
* Falls back to ``"<no stderr captured>"`` when content is empty / None
  — preserving the diagnostic shape even on a silent failure.
* Strips ANSI escape codes defensively (Codex runs with
  ``--color never`` but Gemini CLI may not).
"""
from __future__ import annotations

import pytest

from llm_router.router import _format_subprocess_chain_error


# ── 1. Happy path: first informative line ────────────────────────────────


def test_emits_agent_name_and_exit_code() -> None:
    msg = _format_subprocess_chain_error("Codex", 1, "boom")
    assert msg.startswith("Codex exited 1:")
    assert "boom" in msg


def test_extracts_first_non_empty_line() -> None:
    content = (
        "\n"
        "\n"
        "ERROR 400: model not supported on ChatGPT account\n"
        "stack: foo\n"
        "stack: bar\n"
    )
    msg = _format_subprocess_chain_error("Codex", 1, content)
    assert "ERROR 400: model not supported on ChatGPT account" in msg
    # Subsequent lines suppressed
    assert "stack: foo" not in msg


def test_preserves_pr39_class_message() -> None:
    """The exact message shape from PR #39's diagnosis must round-trip
    through the helper — this pins the regression."""
    content = (
        "ERROR 400: \"The 'gpt-4o-mini' model is not supported "
        "when using Codex with a ChatGPT account.\""
    )
    msg = _format_subprocess_chain_error("Codex", 1, content)
    assert "gpt-4o-mini" in msg
    assert "ChatGPT account" in msg


def test_preserves_trusted_directory_message() -> None:
    """The other PR #39 failure class — non-trusted-directory refusal."""
    content = (
        "Not inside a trusted directory and --skip-git-repo-check "
        "was not specified."
    )
    msg = _format_subprocess_chain_error("Codex", 1, content)
    assert "Not inside a trusted directory" in msg


# ── 2. Truncation ────────────────────────────────────────────────────────


def test_truncates_long_content() -> None:
    long_line = "A" * 1000
    msg = _format_subprocess_chain_error("Codex", 1, long_line)
    # Whole message should be well under 1000 chars
    assert len(msg) < 400
    # Truncation marker present
    assert "…" in msg or "..." in msg


def test_keeps_short_content_intact() -> None:
    short = "permission denied"
    msg = _format_subprocess_chain_error("Codex", 1, short)
    assert short in msg
    # No truncation marker when content fits
    assert "…" not in msg
    assert "..." not in msg


# ── 3. Empty / None ──────────────────────────────────────────────────────


def test_handles_empty_content() -> None:
    msg = _format_subprocess_chain_error("Codex", 1, "")
    assert "Codex exited 1" in msg
    # Some marker indicating absence of stderr — never an empty trailing colon
    assert "<no stderr captured>" in msg


def test_handles_none_content() -> None:
    msg = _format_subprocess_chain_error("Codex", 1, None)
    assert "Codex exited 1" in msg
    assert "<no stderr captured>" in msg


def test_handles_whitespace_only_content() -> None:
    msg = _format_subprocess_chain_error("Codex", 1, "   \n  \n\n")
    assert "<no stderr captured>" in msg


# ── 4. ANSI stripping ───────────────────────────────────────────────────


def test_strips_ansi_color_codes() -> None:
    """Gemini CLI may include ANSI codes; chain summary should stay clean."""
    coloured = "\x1b[31mError:\x1b[0m connection refused"
    msg = _format_subprocess_chain_error("Gemini CLI", 1, coloured)
    assert "\x1b" not in msg
    assert "Error: connection refused" in msg


# ── 5. Different agents reuse the helper ────────────────────────────────


@pytest.mark.parametrize("agent", ["Codex", "Gemini CLI", "Ollama"])
def test_agent_name_appears_verbatim(agent: str) -> None:
    msg = _format_subprocess_chain_error(agent, 2, "x")
    assert msg.startswith(f"{agent} exited 2:")


@pytest.mark.parametrize("exit_code", [1, 2, 124, 127])
def test_exit_code_appears_verbatim(exit_code: int) -> None:
    msg = _format_subprocess_chain_error("Codex", exit_code, "x")
    assert f"exited {exit_code}" in msg
