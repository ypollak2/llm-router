"""Behaviour of the llm_text_job MCP tool with route_and_call mocked: flag off,
accept-first-try, retry-with-feedback, reject-after-3, unknown job, truncation,
and flag-gated registration. No real model is ever called."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_router import paths, text_job_ledger
from llm_router.tools import text as text_tools

DIFF = "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n+x = 1\n"
GOOD = "app: set x to 1 in app.py"
BAD = "WIP"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TEXT_JOBS", raising=False)


def _ctx():
    return SimpleNamespace(info=AsyncMock(), report_progress=AsyncMock())


def _resp(content: str):
    return SimpleNamespace(content=content, model="ollama/test", header=lambda: "HEADER")


def _mock_route(monkeypatch, *contents: str) -> AsyncMock:
    mock = AsyncMock(side_effect=[_resp(c) for c in contents])
    monkeypatch.setattr(text_tools, "route_and_call", mock)
    return mock


def _rows() -> list[dict]:
    path = paths.state_path(text_job_ledger.LEDGER_FILENAME)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_flag_off_makes_no_model_call_and_logs_fallback(monkeypatch):
    mock = _mock_route(monkeypatch, GOOD)
    out = await text_tools.llm_text_job("commit_message", DIFF, _ctx())
    assert mock.await_count == 0
    assert "status=fallback" in out and "yourself" in out
    (row,) = _rows()
    assert (row["status"], row["attempts"], row["model"]) == ("fallback", 0, "none")


@pytest.mark.parametrize("value", ["0", "", "no", "off", "false"])
async def test_falsy_flag_values_stay_off(monkeypatch, value):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", value)
    mock = _mock_route(monkeypatch, GOOD)
    out = await text_tools.llm_text_job("commit_message", DIFF, _ctx())
    assert mock.await_count == 0 and "status=fallback" in out


async def test_unknown_job_is_a_fallback_without_a_call(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    mock = _mock_route(monkeypatch, GOOD)
    out = await text_tools.llm_text_job("write_poem", "x", _ctx())
    assert mock.await_count == 0 and "Unknown job" in out and "status=fallback" in out


async def test_accepted_on_first_attempt(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "true")
    mock = _mock_route(monkeypatch, GOOD)
    out = await text_tools.llm_text_job("commit_message", DIFF, _ctx())
    assert mock.await_count == 1
    assert "status=accepted, attempts=1" in out and GOOD in out
    (row,) = _rows()
    assert (row["status"], row["attempts"], row["model"], row["job"]) == (
        "accepted", 1, "ollama/test", "commit_message")


async def test_rejection_reason_is_fed_back_then_accepted(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    mock = _mock_route(monkeypatch, BAD, GOOD)
    out = await text_tools.llm_text_job("commit_message", DIFF, _ctx())
    assert mock.await_count == 2
    first_prompt = mock.await_args_list[0].args[1]
    second_prompt = mock.await_args_list[1].args[1]
    assert "rejected" not in first_prompt
    assert "rejected" in second_prompt and "placeholder" in second_prompt
    assert "status=accepted, attempts=2" in out
    assert [r["status"] for r in _rows()] == ["accepted"]


async def test_rejected_after_three_attempts_tells_caller_to_do_it(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    mock = _mock_route(monkeypatch, BAD, BAD, BAD, GOOD)
    out = await text_tools.llm_text_job("commit_message", DIFF, _ctx())
    assert mock.await_count == 3  # the 4th canned answer is never used
    assert "status=rejected, attempts=3" in out
    assert "do this job yourself" in out.lower()
    assert "not for use as-is" in out
    (row,) = _rows()
    assert (row["status"], row["attempts"]) == ("rejected", 3)


async def test_summary_job_accepts_only_when_signals_kept(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    raw = "tests/test_a.py:7: AssertionError\nexit code 2\n"
    lossy = "Something went wrong in the test run, please investigate."
    kept = "FAILED: tests/test_a.py:7 AssertionError; exit code 2."
    mock = _mock_route(monkeypatch, lossy, kept)
    out = await text_tools.llm_text_job("summarize_output", raw, _ctx())
    assert mock.await_count == 2
    assert "dropped file:line" in mock.await_args_list[1].args[1]
    assert "status=accepted, attempts=2" in out and kept in out


async def test_long_input_is_truncated_with_a_note(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    big = DIFF + ("+line\n" * 20_000)
    mock = _mock_route(monkeypatch, GOOD)
    out = await text_tools.llm_text_job("commit_message", big, _ctx())
    sent = mock.await_args_list[0].args[1]
    assert len(sent) < len(big)
    assert "input_text truncated" in out


def test_tool_is_registered_only_when_flag_is_set(monkeypatch):
    class _Mcp:
        def __init__(self):
            self.names: list[str] = []

        def tool(self):
            return lambda fn: self.names.append(fn.__name__) or fn

    off = _Mcp()
    text_tools.register(off, lambda _: True)
    assert "llm_text_job" not in off.names
    assert "llm_edit" in off.names  # the registration path itself ran

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    on = _Mcp()
    text_tools.register(on, lambda _: True)
    assert "llm_text_job" in on.names
