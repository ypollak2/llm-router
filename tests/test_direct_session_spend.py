"""DIRECT-path routings must populate the session-scoped ledger.

Regression guard for the bug where a session that routed exclusively through
the DIRECT hook path (never the MCP ``llm_*`` tools) reported $0 spent / $0
saved from ``llm_session_spend`` / ``llm_session_savings`` — because those read
``session_spend.json``, which the DIRECT path never updated. The fix records
each DIRECT routing into ``SessionSpend`` from ``savings_logger.log_direct_savings``.

Every assertion is grounded in the token counts fed in, so the reported savings
are traceable, not a free-floating counterfactual.
"""
import json

import pytest

import llm_router.session_spend as SS
from llm_router.hooks import savings_logger as SL
from llm_router.hooks.chain_builder import ModelSpec
from llm_router.hooks.direct_executor import DirectResult


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """Point both ledgers at a tmp dir and reset the in-process singleton."""
    monkeypatch.setattr(SS, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")
    monkeypatch.setattr(SL, "_savings_log_path", lambda: tmp_path / "savings_log.jsonl")
    SS.reset_session_spend()
    yield SS.SESSION_SPEND_FILE


def _route(complexity, itok, otok, monkeypatch):
    """Simulate one DIRECT-routed prompt arriving in a fresh hook process."""
    monkeypatch.setattr(SS, "_spend", None)  # new process → reload from disk
    res = DirectResult(
        text="x",
        model=ModelSpec(provider="ollama", model="hermes3:8b"),
        latency_ms=5000,
        input_tokens=itok,
        output_tokens=otok,
    )
    SL.log_direct_savings(res, task_type="query", complexity=complexity,
                          session_id="test-session")


def test_direct_routing_accumulates_real_session_savings(isolated_ledger, monkeypatch):
    turns = [("moderate", 128, 168), ("deep_reasoning", 134, 333),
             ("simple", 167, 54), ("moderate", 142, 101)]

    expected_net = 0.0
    expected_tokens = 0
    for complexity, itok, otok in turns:
        _route(complexity, itok, otok, monkeypatch)
        expected_net += (SL._baseline_cost(complexity, itok, otok)
                         - SL._cost_for("ollama", "hermes3:8b", itok, otok))
        expected_tokens += itok + otok

    snap = json.loads(isolated_ledger.read_text())

    assert snap["call_count"] == len(turns)
    assert snap["tokens_reclaimed"] == expected_tokens
    assert snap["total_usd"] == pytest.approx(0.0, abs=1e-9)        # free local model
    assert snap["net_savings_usd"] == pytest.approx(round(expected_net, 6), abs=1e-6)
    assert snap["net_savings_usd"] > 0                              # was $0 before the fix
    assert "ollama/hermes3:8b" in snap["per_model"]
    assert snap["per_model"]["ollama/hermes3:8b"]["tokens"] == expected_tokens


def test_direct_recording_preserves_prompt_sequence(isolated_ledger, monkeypatch):
    """The hook hand-writes prompt_sequence; recording must not drop it."""
    data = json.loads(isolated_ledger.read_text())
    data["prompt_sequence"] = 7
    isolated_ledger.write_text(json.dumps(data))

    _route("simple", 10, 10, monkeypatch)

    assert json.loads(isolated_ledger.read_text())["prompt_sequence"] == 7


def test_overridden_turns_reduce_realized_savings(isolated_ledger, monkeypatch):
    """When the main model overrides routed turns, realized < potential."""
    for c, i, o in [("moderate", 128, 168), ("moderate", 142, 101)]:
        _route(c, i, o, monkeypatch)

    spend = SS.get_session_spend()
    potential = spend.potential_savings_usd
    assert potential > 0
    # Before any override: realized == potential.
    assert spend.realized_savings_usd == pytest.approx(potential, abs=1e-9)

    # Override one of the two routed turns → realized halves.
    spend.mark_overridden(prompt_sequence=1)
    assert spend.overridden_turns == 1
    assert spend.realized_savings_usd == pytest.approx(potential * 1 / 2, abs=1e-6)


def test_mark_overridden_is_deduped_per_turn(isolated_ledger, monkeypatch):
    """Several blocked tool-calls in one turn count as a single override."""
    _route("moderate", 128, 168, monkeypatch)
    spend = SS.get_session_spend()

    spend.mark_overridden(prompt_sequence=3)
    spend.mark_overridden(prompt_sequence=3)  # same turn — ignored
    spend.mark_overridden(prompt_sequence=3)
    assert spend.overridden_turns == 1

    spend.mark_overridden(prompt_sequence=4)  # next turn — counts
    assert spend.overridden_turns == 2
