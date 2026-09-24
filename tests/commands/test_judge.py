"""Tests for `llm-router judge drain` — CLI wiring for the grading queue."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.commands.judge import main as judge_main


def test_judge_drain_dispatches_to_drain_queue(capsys):
    fake_result = {"graded": 2, "ungraded": 1, "failed": 0, "requeued": 0}
    with patch(
        "llm_router.judge.drain_queue", AsyncMock(return_value=fake_result)
    ) as mock_drain:
        rc = judge_main(["drain"])

    assert rc == 0
    mock_drain.assert_called_once_with(batch_size=50, time_budget_s=20.0)
    out = capsys.readouterr().out
    assert "graded=2" in out
    assert "ungraded=1" in out


def test_judge_drain_passes_through_batch_and_time_flags():
    fake_result = {"graded": 0, "ungraded": 0, "failed": 0, "requeued": 0}
    with patch(
        "llm_router.judge.drain_queue", AsyncMock(return_value=fake_result)
    ) as mock_drain:
        rc = judge_main(["drain", "--batch-size", "10", "--time-budget", "5.5"])

    assert rc == 0
    mock_drain.assert_called_once_with(batch_size=10, time_budget_s=5.5)


def test_judge_no_action_prints_help_and_returns_nonzero(capsys):
    rc = judge_main([])
    assert rc == 2


def test_cli_dispatches_judge_subcommand(monkeypatch):
    """cli.main must route `llm-router judge ...` to commands/judge.py — the
    same wiring `_KNOWN_SUBCOMMANDS` and test_f38 check for every command."""
    import sys

    from llm_router import cli

    monkeypatch.setattr(sys, "argv", ["llm-router", "judge", "drain"])
    with patch("llm_router.commands.judge.main", return_value=0) as mock_main:
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    mock_main.assert_called_once_with(["drain"])
    assert exc_info.value.code == 0
