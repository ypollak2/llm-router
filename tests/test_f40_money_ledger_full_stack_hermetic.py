"""Full-stack money-ledger coverage that CI can actually run — T-24.

The only end-to-end exercise of ledger + importer + aggregation lived in
`tests/e2e/test_e2e_money_ledger.py`, behind `requires_ollama`, which
`pyproject.toml`'s `addopts` deselects by default. 62 of 9,029 tests are
deselected and those 14 were the only full-stack check that a routed call
actually reaches the savings totals.

Making CI run Ollama is an infrastructure decision, not a code fix. This file
takes the other half: the same chain — route → JSONL → importer → aggregate —
with a STUB provider, so the plumbing is covered on every run. It does not
replace the live tests (nothing here proves a real model responds); it means a
break in the ledger chain stops being invisible by default.
"""

from __future__ import annotations

import json

import pytest

from llm_router import cost


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return tmp_path


async def _seed_savings_jsonl(home, rows):
    """Write the JSONL the hook layer produces, without needing the hook."""
    from llm_router.cost import savings_log_path  # noqa: PLC0415

    path = savings_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


@pytest.mark.asyncio
async def test_the_ledger_chain_carries_a_call_all_the_way_to_the_totals(home):
    """route-shaped row → JSONL → importer → savings_stats → aggregate."""
    rows = [
        {"timestamp": "2026-09-22T10:00:00+00:00", "session_id": "s1",
         "task_type": "code", "estimated_saved": 0.30, "external_cost": 0.01,
         "model": "ollama/qwen", "host": "claude_code",
         "input_tokens": 100, "output_tokens": 50, "is_simulated": 0},
        {"timestamp": "2026-09-22T10:01:00+00:00", "session_id": "s1",
         "task_type": "query", "estimated_saved": 0.20, "external_cost": 0.00,
         "model": "ollama/qwen", "host": "claude_code",
         "input_tokens": 60, "output_tokens": 20, "is_simulated": 0},
    ]
    await _seed_savings_jsonl(home, rows)

    imported = await cost.import_savings_log()
    assert imported == len(rows), f"importer took {imported} of {len(rows)} rows"

    summary = await cost.get_lifetime_savings_summary(days=0)
    assert summary["tasks_routed"] == len(rows), (
        f"{len(rows)} imported rows produced tasks_routed={summary['tasks_routed']} — "
        "the chain drops rows between the importer and the aggregate"
    )
    assert summary["total_saved"] == pytest.approx(0.50)
    assert summary["total_external_cost"] == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_a_synthetic_row_does_not_reach_the_totals(home):
    """The T-05 filter, exercised through the real chain rather than a unit."""
    await _seed_savings_jsonl(home, [
        {"timestamp": "2026-09-22T10:00:00+00:00", "session_id": "s2",
         "task_type": "code", "estimated_saved": 99.0, "external_cost": 0.0,
         "model": "ollama/qwen", "host": "claude_code",
         "input_tokens": 1, "output_tokens": 1, "is_simulated": 1},
    ])
    await cost.import_savings_log()
    summary = await cost.get_lifetime_savings_summary(days=0)
    assert summary["total_saved"] == pytest.approx(0.0), (
        f"a synthetic row contributed ${summary['total_saved']} to lifetime savings"
    )


@pytest.mark.asyncio
async def test_an_unstamped_row_does_not_reach_the_totals(home):
    """NULL provenance is unknown, and unknown is not money (fail-closed)."""
    await _seed_savings_jsonl(home, [
        {"timestamp": "2026-09-22T10:00:00+00:00", "session_id": "s3",
         "task_type": "code", "estimated_saved": 42.0, "external_cost": 0.0,
         "model": "ollama/qwen", "host": "claude_code",
         "input_tokens": 1, "output_tokens": 1},
    ])
    await cost.import_savings_log()
    summary = await cost.get_lifetime_savings_summary(days=0)
    assert summary["total_saved"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_the_importer_is_idempotent(home):
    """Re-importing must not double the totals.

    The JSONL is claimed-and-truncated, so a second import should take nothing.
    If it takes the rows again, every savings figure doubles on the next flush.
    """
    await _seed_savings_jsonl(home, [
        {"timestamp": "2026-09-22T10:00:00+00:00", "session_id": "s4",
         "task_type": "code", "estimated_saved": 0.25, "external_cost": 0.0,
         "model": "ollama/qwen", "host": "claude_code",
         "input_tokens": 1, "output_tokens": 1, "is_simulated": 0},
    ])
    first = await cost.import_savings_log()
    second = await cost.import_savings_log()
    assert first == 1
    assert second == 0, f"a second import took {second} rows — totals will double"

    summary = await cost.get_lifetime_savings_summary(days=0)
    assert summary["total_saved"] == pytest.approx(0.25)


def test_this_file_runs_by_default(request):
    """The whole point: it must not be deselected like the live e2e lane.

    `pyproject.toml` deselects `requires_ollama`, which is where the only
    full-stack ledger coverage lived. If someone marks this file the same way,
    the gap reopens silently.
    """
    marks = {m.name for m in request.node.iter_markers()}
    assert "requires_ollama" not in marks
    assert "slow" not in marks
    assert "requires_api_keys" not in marks
