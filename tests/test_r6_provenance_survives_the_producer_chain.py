"""The producer never stamped provenance, so fail-closed filtering zeroed real money.

R6 made the money surfaces filter fail-closed: `COALESCE(is_simulated, 1) = 0`.
That rule is right — a row written before the column existed had its provenance
NEVER MEASURED, and counting it asserts production origin on no evidence.

It is also only safe if the producers stamp it. They did not.

Found by reading the rows this very session's hook had just written into the
operator's live ledger:

    58 rows in 20 minutes, every one is_simulated = NULL, $0.0081 total

All four `INSERT INTO savings_stats` sites stamp provenance correctly, and
`cost.import_savings_log` correctly copies an entry's own provenance rather
than inventing one (`_detect_synthetic()` there would describe the IMPORTING
process, not the call). But `hooks/savings_logger.py` writes the JSONL those
rows are imported from, and its record carried no provenance field at all — so
there was nothing to copy.

The chain was: correct writer, correct importer, correct filter, and a real
figure of $0.00, because the one link nobody looked at was the producer.

This test walks the whole chain — record -> JSONL -> import -> filtered query —
because every individual link passed its own tests while the chain was broken.
"""

from __future__ import annotations

import ast
import json
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
LOGGER = SRC / "llm_router" / "hooks" / "savings_logger.py"


def _record_dicts() -> list[ast.Dict]:
    """Every `record = {...}` literal in the producer."""
    tree = ast.parse(LOGGER.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "record" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            out.append(node.value)
    return out


def test_the_producer_writes_more_than_one_record_shape():
    """Anti-vacuity: the assertions below are empty if the scan finds nothing."""
    records = _record_dicts()
    assert len(records) >= 2, (
        f"found {len(records)} `record = {{...}}` literals in savings_logger.py; "
        "there are two producer paths (DIRECT and the receipt bridge) and both "
        "must stamp provenance. If they were refactored, update this scan — do "
        "not leave it matching nothing."
    )


def test_every_producer_record_stamps_provenance():
    """AST on the dict literal, so the key cannot be satisfied by a comment."""
    missing = []
    for i, d in enumerate(_record_dicts()):
        keys = {k.value for k in d.keys if isinstance(k, ast.Constant)}
        if "is_simulated" not in keys:
            missing.append(f"record #{i + 1} at line {d.lineno}: keys={sorted(keys)}")
    assert not missing, (
        "savings_logger record(s) with no `is_simulated` key:\n  "
        + "\n  ".join(missing)
        + "\n\nEvery row they produce lands NULL, and the money surfaces filter "
        "fail-closed, so genuinely real routing reports $0.00. The importer "
        "cannot fix this — it deliberately refuses to invent a provenance the "
        "producer did not record."
    )


def test_the_provenance_detector_fails_closed():
    """Unreachable detector means synthetic, never production.

    Read from the AST rather than by importing the module. `llm_router.hooks`
    is a package that importing `savings_logger` leaves a fileless stub on, and
    the suite's own T-01 guard rejects that — a stub answers for the real
    module in every test that follows. Which is the class of defect fixed two
    commits ago, caught here by the guard that exists because of it.
    """
    tree = ast.parse(LOGGER.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_detect_synthetic"
    )
    # The except branch must return True (synthetic), not False.
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers, "_detect_synthetic has no failure path"
    returns = [
        r.value.value
        for h in handlers
        for r in ast.walk(h)
        if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)
    ]
    assert returns and all(v is True for v in returns), (
        f"_detect_synthetic's failure path returns {returns}; it must return "
        "True. A detector we cannot reach cannot certify a row as production, "
        "and defaulting to production counts test data as real money."
    )


def test_the_importer_carries_provenance_through(tmp_path, monkeypatch):
    """Producer -> JSONL -> import -> a filtered read. The whole chain.

    Every link passed its own tests while the chain was broken, which is the
    entire reason this is one test rather than three.
    """
    import asyncio
    import sqlite3

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))

    log = tmp_path / "savings_log.jsonl"
    rows = [
        {"timestamp": "2026-09-22T10:00:00+00:00", "session_id": "s", "task_type": "code",
         "estimated_saved": 2.50, "external_cost": 0.0, "model": "ollama/x",
         "is_simulated": 0},                                        # production
        {"timestamp": "2026-09-22T10:00:01+00:00", "session_id": "s", "task_type": "code",
         "estimated_saved": 999.0, "external_cost": 0.0, "model": "ollama/x",
         "is_simulated": 1},                                        # a benchmark
        {"timestamp": "2026-09-22T10:00:02+00:00", "session_id": "s", "task_type": "code",
         "estimated_saved": 7.0, "external_cost": 0.0, "model": "ollama/x"},
        # ^ no field at all: a row from before the producer stamped provenance
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    from llm_router import cost

    monkeypatch.setattr(cost, "savings_log_path", lambda: log)
    n = asyncio.run(cost.import_savings_log())
    assert n == 3, f"imported {n} of 3"

    con = sqlite3.connect(db)
    try:
        got = dict(con.execute(
            "SELECT COALESCE(is_simulated, -1), ROUND(SUM(estimated_claude_cost_saved), 2) "
            "FROM savings_stats GROUP BY 1"
        ).fetchall())
    finally:
        con.close()

    assert got.get(0) == 2.50, f"the production row did not survive: {got}"
    assert got.get(1) == 999.0, f"the benchmark row lost its marking: {got}"
    assert got.get(-1) == 7.00, (
        f"the unstamped row should be NULL (unknown), not coerced: {got}"
    )

    # And the filter the money surfaces use keeps exactly the production row.
    con = sqlite3.connect(db)
    try:
        counted = con.execute(
            "SELECT ROUND(COALESCE(SUM(estimated_claude_cost_saved), 0), 2) "
            "FROM savings_stats WHERE COALESCE(is_simulated, 1) = 0"
        ).fetchone()[0]
    finally:
        con.close()
    assert counted == 2.50, (
        f"the fail-closed filter counted {counted}; it must count only the row "
        "whose provenance was actually measured — not the benchmark's $999, "
        "and not the unmeasured $7."
    )
