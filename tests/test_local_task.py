"""The local task service must report what happened, not what it hoped.

Every assertion here exists because of a measured failure on 2026-09-12:
the loop returns "Agent reached maximum iterations" on exhaustion, and
`direct_executor.quality_ok` — which checks length and refusal phrases — scored
that string as a pass. A task service whose success signal is the worker's own
prose reproduces that bug at a larger blast radius.
"""
from __future__ import annotations

import json


from llm_router.tools import local_task as lt


def _call(**kw):
    import asyncio
    return json.loads(asyncio.run(lt.llm_local_task(**kw)))


def test_missing_workdir_is_blocked(tmp_path):
    out = _call(objective="x", workdir=str(tmp_path / "nope"))
    assert out["status"] == lt.BLOCKED
    assert out["check_passed"] is None


def test_exhaustion_is_never_success(tmp_path, monkeypatch):
    """The exact string that was scored as a pass must map to `incomplete`."""
    monkeypatch.setattr(lt, "run_agent_loop", None, raising=False)
    monkeypatch.setattr(
        "llm_router.hooks.agent_loop.run_agent_loop",
        lambda **kw: "Agent reached maximum iterations. Partial work may have been done.",
    )
    out = _call(objective="x", workdir=str(tmp_path))
    assert out["status"] == lt.INCOMPLETE, out


def test_no_check_can_never_be_verified(tmp_path, monkeypatch):
    """Without an acceptance check nothing established the work is correct."""
    monkeypatch.setattr(
        "llm_router.hooks.agent_loop.run_agent_loop",
        lambda **kw: "I have completed the task successfully and everything works.",
    )
    out = _call(objective="x", workdir=str(tmp_path))
    assert out["status"] == lt.PROPOSED, out
    assert out["status"] != lt.VERIFIED_COMPLETE
    assert out["check_passed"] is None


def test_confident_report_with_a_failing_check_is_not_success(tmp_path, monkeypatch):
    """The worker's self-report loses to the supervisor's check."""
    monkeypatch.setattr(
        "llm_router.hooks.agent_loop.run_agent_loop",
        lambda **kw: "Done — I fixed it and verified it works.",
    )
    out = _call(objective="x", workdir=str(tmp_path), acceptance_check=["python3", "-c", "raise SystemExit(1)"])
    assert out["status"] == lt.FAILED_CHECK, out
    assert out["check_passed"] is False


def test_passing_check_is_verified_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_router.hooks.agent_loop.run_agent_loop",
        lambda **kw: "did the thing",
    )
    out = _call(objective="x", workdir=str(tmp_path), acceptance_check=["python3", "-c", "raise SystemExit(0)"])
    assert out["status"] == lt.VERIFIED_COMPLETE
    assert out["check_passed"] is True


def test_a_passing_check_beats_exhaustion(tmp_path, monkeypatch):
    """If the objective is demonstrably met, the turn count is uninteresting."""
    monkeypatch.setattr(
        "llm_router.hooks.agent_loop.run_agent_loop",
        lambda **kw: "Agent reached maximum iterations.",
    )
    out = _call(objective="x", workdir=str(tmp_path), acceptance_check=["python3", "-c", "raise SystemExit(0)"])
    assert out["status"] == lt.VERIFIED_COMPLETE


def test_loop_crash_is_reported_as_failed(tmp_path, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("ollama unreachable")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _boom)
    out = _call(objective="x", workdir=str(tmp_path))
    assert out["status"] == lt.FAILED
    assert "ollama unreachable" in (out["error"] or "")


def test_changed_files_are_observed_not_reported(tmp_path, monkeypatch):
    """File changes come from hashing the tree, never from the model's claims."""
    (tmp_path / "a.txt").write_text("before")

    def _edit(**kw):
        (tmp_path / "a.txt").write_text("after")
        (tmp_path / "new.txt").write_text("new")
        return "I changed nothing at all."      # a lie, deliberately

    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", _edit)
    out = _call(objective="x", workdir=str(tmp_path))
    assert out["changed_files"] == ["a.txt", "new.txt"], out


def test_write_mode_is_restored_after_the_call(tmp_path, monkeypatch):
    """The apply-mode override is scoped to the call, not leaked to the process."""
    import os
    monkeypatch.setenv("LLM_ROUTER_AGENT_WRITES", "propose")
    monkeypatch.setattr("llm_router.hooks.agent_loop.run_agent_loop", lambda **kw: "ok")
    _call(objective="x", workdir=str(tmp_path), apply_writes=True)
    assert os.environ["LLM_ROUTER_AGENT_WRITES"] == "propose"
