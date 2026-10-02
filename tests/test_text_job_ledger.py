"""Ledger for llm_text_job: same contract as edit_ledger (resolved session id,
null never "", fail-silent)."""
from __future__ import annotations

import json

import pytest

from llm_router import paths, session_store, text_job_ledger


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    yield tmp_path


def _rows() -> list[dict]:
    path = paths.state_path(text_job_ledger.LEDGER_FILENAME)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_row_shape_and_resolved_session_id():
    session_store.write_pointer("sess-xyz")
    text_job_ledger.record_text_job_outcome(
        job="commit_message", model="ollama/qwen3.5:latest",
        status=text_job_ledger.STATUS_ACCEPTED, attempts=2,
    )
    (row,) = _rows()
    assert set(row) == {"ts", "session_id", "job", "model", "status", "attempts"}
    assert row["session_id"] == "sess-xyz"
    assert (row["job"], row["model"], row["status"], row["attempts"]) == (
        "commit_message", "ollama/qwen3.5:latest", "accepted", 2)
    assert isinstance(row["ts"], float)


def test_unresolvable_session_is_null_not_empty_string():
    text_job_ledger.record_text_job_outcome(
        job="summarize_output", model="none", status=text_job_ledger.STATUS_FALLBACK, attempts=0)
    (row,) = _rows()
    assert row["session_id"] is None


def test_appends_one_row_per_call():
    for status in (text_job_ledger.STATUS_ACCEPTED, text_job_ledger.STATUS_REJECTED,
                   text_job_ledger.STATUS_FALLBACK):
        text_job_ledger.record_text_job_outcome(job="pr_description", model="m", status=status, attempts=1)
    assert [r["status"] for r in _rows()] == ["accepted", "rejected", "fallback"]


def test_write_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(paths, "state_path", boom)
    # Must not raise: a broken ledger cannot break the call being waited on.
    text_job_ledger.record_text_job_outcome(job="commit_message", model="m", status="accepted", attempts=1)
