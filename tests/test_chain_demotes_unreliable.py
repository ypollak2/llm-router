"""Chain order answers to measured timeouts, not only to static rules.

A3 of docs/ACTIONS_REMEDIATION_RUN.md, and it depends on A1 — before the attempt
log existed there was no failure data to order by. `build_chain` ordered purely by
complexity x zone x task_type with zero references to latency, timeout or history.

Measured 2026-09-14: the first model in the chain timed out on 72 of 166 attempts
and 50 of a 99-minute run produced nothing, while the fallback behind it answered
in ~12s. Demote, never drop: a slow model that sometimes answers beats no model,
and dropping one on thin evidence is a verdict it could never recover from.
"""
from __future__ import annotations

import pytest

from llm_router import attempt_log
from llm_router.hooks import chain_builder as cb
from llm_router.hooks.chain_builder import ModelSpec, _demote_unreliable


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return tmp_path


def _chain():
    return [ModelSpec("ollama", "slow:latest"), ModelSpec("ollama", "fast:latest")]


def _record(model, timeouts, oks):
    for _ in range(timeouts):
        attempt_log.record(model, attempt_log.TIMEOUT, 37000, reason="timeout_37s")
    for _ in range(oks):
        attempt_log.record(model, attempt_log.OK, 9000)


def test_a_model_that_mostly_times_out_stops_leading():
    _record("slow:latest", timeouts=8, oks=1)
    _record("fast:latest", timeouts=0, oks=6)
    assert [m.model for m in _demote_unreliable(_chain())] == ["fast:latest", "slow:latest"]


def test_it_is_demoted_not_dropped():
    _record("slow:latest", timeouts=9, oks=0)
    _record("fast:latest", timeouts=0, oks=6)
    out = _demote_unreliable(_chain())
    assert len(out) == 2, "a model was removed; a slow model still beats no model"
    assert out[-1].model == "slow:latest"


def test_thin_evidence_changes_nothing():
    _record("slow:latest", timeouts=2, oks=0)          # under _MIN_EVIDENCE
    assert [m.model for m in _demote_unreliable(_chain())] == ["slow:latest", "fast:latest"]


def test_a_model_with_no_record_keeps_its_place():
    _record("fast:latest", timeouts=0, oks=6)
    assert [m.model for m in _demote_unreliable(_chain())] == ["slow:latest", "fast:latest"], (
        "an unrecorded model must read as 'unknown', not as 'bad' — otherwise a "
        "newly pulled model can never earn its place"
    )


def test_everything_unreliable_leaves_the_order_alone():
    _record("slow:latest", timeouts=9, oks=0)
    _record("fast:latest", timeouts=9, oks=0)
    assert [m.model for m in _demote_unreliable(_chain())] == ["slow:latest", "fast:latest"], (
        "demoting every candidate produces an empty chain and no draft at all"
    )


def test_a_recovered_model_is_promoted_back():
    _record("slow:latest", timeouts=8, oks=1)
    assert _demote_unreliable(_chain())[-1].model == "slow:latest"
    _record("slow:latest", timeouts=0, oks=40)         # window moves on
    assert _demote_unreliable(_chain())[0].model == "slow:latest"


def test_a_broken_attempt_log_does_not_break_routing(monkeypatch):
    monkeypatch.setattr(attempt_log, "summary",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert [m.model for m in _demote_unreliable(_chain())] == ["slow:latest", "fast:latest"]


def test_build_chain_applies_it(monkeypatch):
    _record("slow:latest", timeouts=8, oks=1)
    _record("fast:latest", timeouts=0, oks=6)
    monkeypatch.setattr("llm_router.model_discovery.available_ollama_models",
                        lambda: ["slow:latest", "fast:latest"])
    got = [m.model for m in cb.build_chain("simple", "green", "query")]
    assert got[0] == "fast:latest", f"build_chain ignored the measured order: {got}"
