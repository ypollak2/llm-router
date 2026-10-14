"""P1.10 task 2: verify records carry task_id and trace_id.

The verify record is ``northstar.record_verify`` (a ``north_star_units.jsonl`` row with a
``verify`` key). It is written by the detached verify worker, another process, so the
dispatching call's ids travel in the queue marker (``verify_queue.enqueue_from_run``). The
toolkit / local_agent ``verify`` modules only compute verdicts and write no ledger row.
"""
from __future__ import annotations

import json
import re

import pytest

from llm_router import call_identity as ci
from llm_router import northstar as ns
from llm_router import verify_queue as Q
from llm_router import verify_worker as W
from llm_router.toolkit.verify_unit import UnitResult

SID = "5a1c0000-0000-4000-8000-00000000b001"
UID = "u_" + "ab" * 8
HEX16 = re.compile(r"[0-9a-f]{16}")
HEX32 = re.compile(r"[0-9a-f]{32}")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.setenv("LLM_ROUTER_VERIFY", "on")


def _result() -> UnitResult:
    return UnitResult("pass_f2p", "v1_f2p", n_candidates=1, n_f2p=1, ms=5, sandboxed=True)


def _rows(tmp_path) -> list[dict]:
    p = tmp_path / "router_home" / "north_star_units.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_verify_row_is_pure_and_adds_ids_only_when_given():
    assert "task_id" not in ns.verify_row(UID, _result())
    row = ns.verify_row(UID, _result(), task_id="t" * 16, trace_id="a" * 32)
    assert row["task_id"] == "t" * 16 and row["trace_id"] == "a" * 32
    assert ns.verify_row(UID, _result(), task_id="has space")["unit_id"] == UID
    assert "task_id" not in ns.verify_row(UID, _result(), task_id="has space")  # free text never rides in


def test_record_verify_stamps_from_the_scope_like_other_writers(tmp_path):
    with ci.scope(SID, task_id="caller-task-9") as (task, trace):
        ns.record_verify(UID, _result())
    row = _rows(tmp_path)[0]
    assert row["task_id"] == task == "caller-task-9" and row["trace_id"] == trace


def test_record_verify_outside_a_scope_gets_a_generated_task_never_none(tmp_path):
    ns.record_verify(UID, _result())
    row = _rows(tmp_path)[0]
    assert ci.is_generated_task_id(row["task_id"]) and HEX32.fullmatch(row["trace_id"])
    assert ns.load_verify_records(_rows(tmp_path))[UID]["verify_status"] == "pass_f2p"  # extra keys harmless


def test_ids_travel_hook_to_marker_to_worker_to_record(tmp_path, monkeypatch):
    ci.begin_turn(SID)
    turn = ci.turn_task_id(SID)
    monkeypatch.setattr(Q, "capture", lambda cwd, budget_s=None: (("/repo", "0" * 40, b"diff --git a/x b/x\n"), ""))
    assert Q.enqueue_from_run("/repo", session_id=SID, ts=1_800_000_000.0) == "queued"
    marker = json.loads(next((Q.queue_dir() / "pending").glob("*.json")).read_text())
    assert marker["task_id"] == turn and HEX32.fullmatch(marker["trace_id"])

    W.drain(verify=lambda checkout, patch, budget_s: _result(),
            record=ns.record_verify)                     # the worker's own call shape, no scope
    # the checkout step may be unavailable on a fake repo; the record is written either way
    row = _rows(tmp_path)[0]
    assert row["task_id"] == turn == marker["task_id"] and row["trace_id"] == marker["trace_id"]
    assert not ci.is_generated_task_id(row["task_id"]) and HEX16.fullmatch(row["task_id"])


def test_old_marker_without_ids_still_settles_with_a_two_arg_recorder(tmp_path):
    Q.enqueue(UID, "/nonexistent", "0" * 40, b"diff --git a/x b/x\n")
    got = []
    W.drain(verify=lambda *a: _result(), record=lambda uid, r: got.append(uid))
    assert got == [UID]
