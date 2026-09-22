"""C-01 — the quality ledger must be able to record a failed route.

The 2026-09-21 audit's top finding, and the one that gated everything downstream:

    routing_quality.jsonl:  0 of 16,869 real rows with route_succeeded=False
    record_route():         exactly ONE call site, router.py:2004,
                            inside _finalize_successful_route

Not a low failure rate — zero, across the whole history, because failure had no
way to be written down. The failure path emitted to the *execution* ledger
(SQLite) and the cache-hit path was skipped by a gate, so two of the three
terminal outcomes were absent from the file that every quality metric and all of
Ground Truth sampling reads.

The consequence is not a wrong number. It is an instrument with one of its
states physically missing: a reader cannot distinguish "nothing failed" from
"failure is unrepresentable", and every success rate is 100% by construction.

These tests assert the property directly — that all three terminal outcomes can
appear in the file — rather than asserting a rate, because a rate computed over
a population that cannot contain failures is exactly the thing being fixed.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from llm_router.routing_quality import (
    CURRENT_SCHEMA_VERSION,
    ROUTE_OUTCOMES,
    RouteLedgerRecord,
    record_route,
)


@pytest.fixture
def ledger(tmp_path, monkeypatch) -> pathlib.Path:
    path = tmp_path / "routing_quality.jsonl"
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(path))
    return path


def _rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_record_can_represent_all_three_terminal_states():
    """Before: one representable outcome. A field with one value is not a measurement."""
    assert set(ROUTE_OUTCOMES) == {"success", "failed", "cache_hit"}


@pytest.mark.parametrize("outcome", ["success", "failed", "cache_hit"])
def test_every_terminal_outcome_can_be_written_and_read_back(ledger, outcome):
    """The file must be able to hold each state, not just the happy one."""
    assert record_route(RouteLedgerRecord(route_outcome=outcome, task_type="code"))

    rows = _rows(ledger)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["route_outcome"] == outcome


def test_a_failed_route_writes_exactly_one_quality_row(ledger, monkeypatch):
    """The production path: every model fails, and the quality ledger says so.

    This is the assertion that was impossible before — `_emit_quality_terminal`
    did not exist and the failure branch never touched this file.
    """
    from llm_router import router
    from llm_router.classify import TaskType
    from llm_router.profiles import RoutingProfile

    router._emit_quality_terminal(
        outcome="failed",
        correlation_id="route-under-test",
        task_type=TaskType.CODE,
        profile=RoutingProfile.BALANCED,
        chain_attempts=["ollama/qwen", "openai/gpt-4o"],
        chain_errors=[("ollama/qwen", "timeout"), ("openai/gpt-4o", "rate_limit")],
        prompt="anything",
    )

    rows = _rows(ledger)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    row = rows[0]
    assert row["route_outcome"] == "failed"
    assert row["route_succeeded"] is False
    assert row["route_id"] == "route-under-test"
    assert row["chain_attempts"] == ["ollama/qwen", "openai/gpt-4o"]
    assert len(row["chain_errors"]) == 2, "the chain that failed must be recorded"


def test_a_cache_hit_is_recorded_as_neither_success_nor_failure(ledger):
    """A cache hit answered the turn but ran no model. It is its own state."""
    from llm_router import router
    from llm_router.classify import TaskType
    from llm_router.profiles import RoutingProfile

    router._emit_quality_terminal(
        outcome="cache_hit",
        correlation_id="cached-route",
        task_type=TaskType.QUERY,
        profile=RoutingProfile.BALANCED,
        chain_attempts=[],
        chain_errors=[],
        final_model="cache/ollama-qwen",
    )

    rows = _rows(ledger)
    assert len(rows) == 1
    assert rows[0]["route_outcome"] == "cache_hit"
    # the legacy boolean stays truthful for existing readers: the turn WAS answered
    assert rows[0]["route_succeeded"] is True


def test_success_rate_is_no_longer_one_by_construction(ledger):
    """The point of the whole finding, expressed as a computation.

    With a mixed population the rate must be able to be something other than
    1.0. Before this change no input could produce that.
    """
    from llm_router import router
    from llm_router.classify import TaskType
    from llm_router.profiles import RoutingProfile

    record_route(RouteLedgerRecord(route_outcome="success", route_succeeded=True))
    record_route(RouteLedgerRecord(route_outcome="success", route_succeeded=True))
    router._emit_quality_terminal(
        outcome="failed", correlation_id="f1", task_type=TaskType.CODE,
        profile=RoutingProfile.BALANCED, chain_attempts=["m"], chain_errors=[("m", "boom")],
    )

    rows = _rows(ledger)
    assert len(rows) == 3, "denominator guard: not all rows were written"
    succeeded = sum(1 for r in rows if r["route_outcome"] == "success")
    rate = succeeded / len(rows)
    assert rate == pytest.approx(2 / 3), f"success rate {rate}, expected 2/3"
    assert rate != 1.0, "the rate is still 1.0 — failure is still unrepresentable"


def test_schema_version_was_bumped(ledger):
    """A new field changes the row shape; readers must be able to tell."""
    assert CURRENT_SCHEMA_VERSION >= 4
    record_route(RouteLedgerRecord(route_outcome="failed"))
    assert _rows(ledger)[0]["schema_version"] == CURRENT_SCHEMA_VERSION


def test_failure_rows_carry_provenance_like_every_other_row(ledger):
    """A failure row written under pytest must be marked synthetic.

    Otherwise the fix for C-01 would immediately reintroduce the contamination
    that `is_evaluable` exists to prevent — this suite would start writing
    "production failures" into any ledger it touched.
    """
    from llm_router.routing_quality import is_evaluable

    record_route(RouteLedgerRecord(route_outcome="failed"))
    row = _rows(ledger)[0]
    assert row["synthetic"] is True, "a failure row written by a test is not marked synthetic"
    assert is_evaluable(row) is False


def test_emit_is_fail_open_and_counts_the_loss(ledger, monkeypatch):
    """Telemetry must never fail a turn — but the loss must be COUNTED (H-09).

    The sibling emitter on the execution ledger gained `failopen.record` after
    "66 dropped events across 2400 writes produced no error, no log and no
    counter". That fix was never applied to the measurement ledger.
    """
    from llm_router import failopen, router
    from llm_router.classify import TaskType
    from llm_router.profiles import RoutingProfile
    import llm_router.routing_quality as rq

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rq, "record_route", _boom)

    seen = []
    monkeypatch.setattr(failopen, "record", lambda tag, exc: seen.append(tag))

    # must not raise
    router._emit_quality_terminal(
        outcome="failed", correlation_id="x", task_type=TaskType.CODE,
        profile=RoutingProfile.BALANCED, chain_attempts=[], chain_errors=[],
    )
    assert seen, "the ledger write was lost silently — no failopen counter fired"
    assert any("QUALITY" in t for t in seen), seen


def test_this_suite_writes_where_it_thinks_it_does(ledger):
    """Anti-vacuity + isolation. A test asserting on an empty file passes anything.

    Also pins M-04 for this specific store: `routing_quality` reads
    LLM_ROUTER_ROUTING_LEDGER, and seven synthetic rows once reached the
    operator's real ledger because a test believed it was isolated and was not.
    """
    from llm_router.routing_quality import _default_ledger

    record_route(RouteLedgerRecord(route_outcome="success"))
    assert ledger.exists() and _rows(ledger), "nothing was written to the sandbox"
    real = pathlib.Path.home() / ".llm-router" / "routing_quality.jsonl"
    assert _default_ledger() != real, "the test is pointed at the real ledger"
