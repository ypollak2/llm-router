"""``cost.SAVINGS_LOG_PATH`` must follow the state dir, not freeze at import.

Same defect class as receipt_store: a module-level

    SAVINGS_LOG_PATH = Path.home() / ".llm-router" / "savings_log.jsonl"

captured the real user's home when the module was first imported, so
``LLM_ROUTER_HOME`` could not move it. The consequence is worse here than for
receipts, because ``import_savings_log`` does not merely *read* that file — it
atomically CLAIMS it with os.replace and deletes it after importing. A test run
that thought it was isolated could consume a developer's real, un-imported
savings history.

Found by an e2e test that made three real calls into an isolated home and then
watched the importer report zero rows: it was looking somewhere else entirely.
"""

from __future__ import annotations

import pytest


def test_savings_log_path_follows_the_state_dir(tmp_path, monkeypatch):
    from llm_router import cost

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    assert cost.savings_log_path() == tmp_path / "state" / "savings_log.jsonl"


def test_it_is_resolved_per_call(tmp_path, monkeypatch):
    from llm_router import cost

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "one"))
    first = cost.savings_log_path()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "two"))
    assert cost.savings_log_path() != first, "frozen at import"


@pytest.mark.asyncio
async def test_importer_reads_the_isolated_log(tmp_path, monkeypatch):
    import json

    from llm_router import cost

    state = tmp_path / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(state))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))

    (state / "savings_log.jsonl").write_text(json.dumps({
        "timestamp": "2026-09-04T12:00:00+00:00", "session_id": "s",
        "task_type": "query", "complexity": "simple", "estimated_saved": 1.0,
        "external_cost": 0.0, "cost_state": "known", "model": "ollama/m",
        "input_tokens": 1, "output_tokens": 1, "host": "test",
    }) + "\n")

    assert await cost.import_savings_log() == 1, (
        "the importer looked somewhere other than the configured state dir"
    )
    assert not (state / "savings_log.jsonl").exists(), "log not drained"
