"""Phase 2 — historical rows are UNKNOWN, not production.

`is_simulated` was declared `DEFAULT 0` and never written, so all ~23,000
historical `usage` rows read as "not simulated". That zero is not a recorded
fact about those rows; it is a column default standing in for a measurement
nobody took. 1,813 of them are known fixtures and they are **not separable after
the fact**, because they carry real model names.

So the contaminated history cannot be cleaned. It can only be superseded:

    stamp provenance at write time  (C-02, done)
    -> mark the pre-provenance rows UNKNOWN   (here)
    -> start a clean window
    -> only then recompute and republish

NULL is the honest value. Writing it replaces a default that lies with an
absence that is true, and it destroys no information because the column never
held any. It does not touch the append-only routing ledger, which is a different
store with a different guarantee.

The reader change matters as much: `AND is_simulated IS NOT 1` **admits NULL**,
so an UNKNOWN row still counted as production — the same defect as `is_evaluable`
treating a missing field as real. The filter is now `= 0`: explicitly stamped, or
it does not count.
"""

from __future__ import annotations


import aiosqlite
import pytest

_LEGACY_INSERT = (
    "INSERT INTO usage (model, provider, task_type, profile, input_tokens, "
    "output_tokens, cost_usd, latency_ms, success, saved_usd) VALUES "
    "('claude-opus-4-6','anthropic','code','balanced',100,50,0.01,10,1,0.5)"
)


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    path = str(tmp_path / "usage.db")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", path)
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    return path


async def _provenance_counts(path: str) -> dict:
    db = await aiosqlite.connect(path)
    try:
        rows = await db.execute_fetchall(
            "SELECT is_simulated, COUNT(*) FROM usage GROUP BY is_simulated"
        )
        return {("NULL" if k is None else k): v for k, v in rows}
    finally:
        await db.close()


async def _seed_pre_upgrade(path: str, n: int) -> None:
    """A database as it existed before provenance was stamped."""
    from llm_router import cost

    db = await cost._get_db()
    await db.close()
    db = await aiosqlite.connect(path)
    try:
        await db.execute("DELETE FROM provenance_meta")  # pretend the upgrade has not run
        for _ in range(n):
            await db.execute(_LEGACY_INSERT)
        await db.commit()
    finally:
        await db.close()


async def test_pre_provenance_rows_become_unknown(db_path):
    from llm_router import cost

    await _seed_pre_upgrade(db_path, 7)
    assert await _provenance_counts(db_path) == {0: 7}, "fixture did not seed legacy rows"

    db = await cost._get_db()   # opening runs migrations, including the cutover
    await db.close()

    assert await _provenance_counts(db_path) == {"NULL": 7}, (
        "historical rows still claim to be production"
    )


async def test_unknown_rows_are_excluded_from_savings(db_path):
    from llm_router import cost

    await _seed_pre_upgrade(db_path, 7)
    db = await cost._get_db()
    await db.close()

    out = await cost.get_savings_by_period()
    assert out["all_time"]["calls"] == 0, (
        "UNKNOWN rows counted toward savings — the filter still admits NULL"
    )


async def test_a_row_stamped_real_after_the_cutover_does_count(db_path):
    """Anti-over-correction: the clean window has to actually begin."""
    from llm_router import cost

    await _seed_pre_upgrade(db_path, 3)
    db = await cost._get_db()
    await db.close()

    db = await aiosqlite.connect(db_path)
    await db.execute(_LEGACY_INSERT.replace(
        "saved_usd) VALUES", "saved_usd, is_simulated) VALUES").replace("0.5)", "0.5,0)"))
    await db.commit()
    await db.close()

    out = await cost.get_savings_by_period()
    assert out["all_time"]["calls"] == 1, (
        "a row explicitly stamped as production is not being counted — the "
        "filter is now excluding everything, which is not a fix"
    )


async def test_the_cutover_is_idempotent(db_path):
    """Run twice and the second run must not blank correctly-stamped rows.

    Without the sentinel this turns the fix into the bug it was fixing.
    """
    from llm_router import cost

    await _seed_pre_upgrade(db_path, 4)
    db = await cost._get_db()
    await db.close()

    db = await aiosqlite.connect(db_path)
    await db.execute(_LEGACY_INSERT.replace(
        "saved_usd) VALUES", "saved_usd, is_simulated) VALUES").replace("0.5)", "0.5,0)"))
    await db.commit()
    await db.close()

    for _ in range(3):          # several more opens
        db = await cost._get_db()
        await db.close()

    counts = await _provenance_counts(db_path)
    assert counts == {"NULL": 4, 0: 1}, (
        f"the cutover re-ran and blanked a real row: {counts}"
    )


async def test_the_sentinel_records_what_it_did(db_path):
    """A migration that changes 23,000 rows must say how many."""
    from llm_router import cost

    await _seed_pre_upgrade(db_path, 5)
    db = await cost._get_db()
    await db.close()

    db = await aiosqlite.connect(db_path)
    try:
        rows = await db.execute_fetchall(
            "SELECT key, value FROM provenance_meta WHERE key = ?",
            (cost.PROVENANCE_CUTOVER_KEY,),
        )
    finally:
        await db.close()
    assert rows, "no cutover sentinel was written"
    assert int(rows[0][1]) == 5, f"sentinel records {rows[0][1]} rows, expected 5"


async def test_this_suite_is_not_vacuous(db_path):
    """The seed must genuinely produce DEFAULT-0 rows.

    If `_seed_pre_upgrade` wrote nothing, or wrote NULL already, every assertion
    above would pass without the cutover doing anything.
    """
    await _seed_pre_upgrade(db_path, 2)
    counts = await _provenance_counts(db_path)
    assert counts == {0: 2}, (
        f"seeded rows are {counts}, not the DEFAULT-0 shape this tests against"
    )
