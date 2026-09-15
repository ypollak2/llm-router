"""Every chain attempt is recorded, not only the one that won.

A1 of docs/ACTIONS_REMEDIATION_RUN.md. `RoutingDecision` is written before the
model is called and carries no outcome; the only latency trace fires for the
winner. So a model that times out on every call left no trace anything could read,
and the 72-of-166 timeout rate measured on 2026-09-14 had to come from an ad-hoc
harness rather than from production telemetry.

This is the precondition for A2 (a success signal that means something) and A3
(demoting a chronically slow model) — neither can adapt on data nobody keeps.
"""
from __future__ import annotations

import json

import pytest

from llm_router import attempt_log
from llm_router.hooks import direct_executor as de
from llm_router.hooks.direct_executor import ModelSpec, execute_chain


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(de, "available_ollama_models",
                        lambda timeout=0.5: {"slow:latest", "fast:latest"})
    return tmp_path


def _rows():
    p = attempt_log._path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _patch_call(monkeypatch, fn):
    monkeypatch.setitem(de._PROVIDER_CALLS, "ollama", fn)


def test_the_loser_is_recorded_not_just_the_winner(monkeypatch):
    def call(prompt, model, timeout, history, system_prompt):
        if model == "slow:latest":
            de._call_failure("ollama", model, "timeout_37s")
            return None, {}
        return "x" * 400, {}

    _patch_call(monkeypatch, call)
    result = execute_chain("q", [ModelSpec("ollama", "slow:latest"),
                                 ModelSpec("ollama", "fast:latest")], "query", timeout=40)
    assert result is not None
    rows = _rows()
    assert len(rows) == 2, f"only {len(rows)} attempt(s) recorded; the failure was lost"
    assert rows[0]["model"] == "slow:latest" and rows[0]["outcome"] == attempt_log.TIMEOUT
    assert rows[1]["model"] == "fast:latest" and rows[1]["outcome"] == attempt_log.OK


@pytest.mark.parametrize("reason,expected", [
    ("timeout_37s", attempt_log.TIMEOUT),
    ("empty response", attempt_log.EMPTY),
    ("returned_empty_content", attempt_log.EMPTY),
])
def test_failure_reasons_map_to_outcomes(monkeypatch, reason, expected):
    def call(prompt, model, timeout, history, system_prompt):
        de._call_failure("ollama", model, reason)
        return None, {}

    _patch_call(monkeypatch, call)
    execute_chain("q", [ModelSpec("ollama", "slow:latest")], "query", timeout=40)
    assert _rows()[0]["outcome"] == expected


def test_a_rejected_answer_is_not_counted_as_a_timeout(monkeypatch):
    _patch_call(monkeypatch, lambda p, m, t, h, s: ("no", {}))
    execute_chain("q", [ModelSpec("ollama", "slow:latest")], "query", timeout=40)
    rows = _rows()
    assert rows and rows[0]["outcome"] == attempt_log.REJECTED, (
        "a short answer is a quality problem, not a latency one; conflating them "
        "would demote a fast model for the wrong reason"
    )


def test_a_failed_attempt_carries_its_elapsed_time(monkeypatch):
    def call(prompt, model, timeout, history, system_prompt):
        de._call_failure("ollama", model, "timeout_37s")
        return None, {}

    _patch_call(monkeypatch, call)
    execute_chain("q", [ModelSpec("ollama", "slow:latest")], "query", timeout=40)
    assert "latency_ms" in _rows()[0], "a timeout with no duration cannot inform ordering"


def test_summary_reports_the_timeout_rate():
    for _ in range(3):
        attempt_log.record("slow:latest", attempt_log.TIMEOUT, 37000, reason="timeout_37s")
    attempt_log.record("slow:latest", attempt_log.OK, 9000)
    attempt_log.record("fast:latest", attempt_log.OK, 4000)
    s = attempt_log.summary()
    assert s["slow:latest"]["timeout_rate"] == pytest.approx(0.75)
    assert s["fast:latest"]["timeout_rate"] == 0.0
    assert s["fast:latest"]["p50_ms"] == 4000


def test_an_unseen_model_is_absent_not_zero():
    attempt_log.record("seen:latest", attempt_log.OK, 1000)
    assert "never-run:latest" not in attempt_log.summary(), (
        "an absent model must read as 'no evidence', never as 'perfect' or 'bad'"
    )


def test_recording_never_breaks_routing(monkeypatch):
    monkeypatch.setattr(attempt_log, "_path", lambda: (_ for _ in ()).throw(OSError("nope")))
    attempt_log.record("m", attempt_log.OK, 1)          # must not raise
    assert attempt_log.summary() == {}
