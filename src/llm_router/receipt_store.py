"""Silent receipt logging — records every verified model delegation.

Receipts are append-only records proving that:
1. A contract was generated for a delegation
2. The delegated model returned a response
3. Verification gates passed (or the response was retried)

Receipts are persisted to SQLite for audit and session summaries.
They are never shown to the user during normal operation.

v8.8.0: Contract-as-Infrastructure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from llm_router.contract import RoutingContract
from llm_router.gates import GateResult

_DB_PATH = Path.home() / ".llm-router" / "receipts.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    contract_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    complexity TEXT NOT NULL,
    model TEXT NOT NULL,
    gates_passed TEXT NOT NULL,
    gate_count INTEGER NOT NULL,
    all_passed INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    opus_equivalent_cost REAL NOT NULL,
    tokens_reclaimed INTEGER NOT NULL,
    savings_usd REAL NOT NULL
)
"""


@dataclass(frozen=True)
class Receipt:
    """A verified delegation receipt."""

    contract_id: str
    model: str
    task_type: str
    complexity: str
    gates_passed: list[str]
    gate_count: int
    all_passed: bool
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    opus_equivalent_cost: float
    tokens_reclaimed: int
    savings_usd: float
    timestamp: float = field(default_factory=time.time)


def compute_receipt(
    contract: RoutingContract,
    gate_results: list[GateResult],
    latency_ms: float,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> Receipt:
    """Compute a receipt from contract + gate results + response metadata.

    Calculates tokens_reclaimed and savings_usd by comparing actual cost
    against what Opus would have charged for the same token volume.
    """
    # Opus pricing: $15/1M input, $75/1M output
    opus_input_cost = (input_tokens / 1_000_000) * 15.0
    opus_output_cost = (output_tokens / 1_000_000) * 75.0
    opus_equivalent = opus_input_cost + opus_output_cost

    savings = opus_equivalent - cost_usd
    # Tokens reclaimed = tokens that Opus would have consumed (input+output)
    # but didn't because a cheaper model handled it
    tokens_reclaimed = input_tokens + output_tokens if savings > 0 else 0

    return Receipt(
        contract_id=contract.contract_id,
        model=contract.model,
        task_type=contract.task_type.value,
        complexity=contract.complexity.value,
        gates_passed=[r.gate.value for r in gate_results if r.passed],
        gate_count=len(gate_results),
        all_passed=all(r.passed for r in gate_results) if gate_results else True,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        opus_equivalent_cost=opus_equivalent,
        tokens_reclaimed=tokens_reclaimed,
        savings_usd=savings,
    )


async def store_receipt(receipt: Receipt) -> None:
    """Persist a receipt to SQLite. Silent on failure."""
    try:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(_DB_PATH)) as db:
            await db.execute(_CREATE_TABLE)
            await db.execute(
                """INSERT INTO receipts (
                    contract_id, task_type, complexity, model,
                    gates_passed, gate_count, all_passed,
                    latency_ms, input_tokens, output_tokens,
                    cost_usd, opus_equivalent_cost, tokens_reclaimed, savings_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.contract_id,
                    receipt.task_type,
                    receipt.complexity,
                    receipt.model,
                    ",".join(receipt.gates_passed),
                    receipt.gate_count,
                    int(receipt.all_passed),
                    receipt.latency_ms,
                    receipt.input_tokens,
                    receipt.output_tokens,
                    receipt.cost_usd,
                    receipt.opus_equivalent_cost,
                    receipt.tokens_reclaimed,
                    receipt.savings_usd,
                ),
            )
            await db.commit()
    except Exception:
        pass  # Never crash routing for receipt storage


async def get_session_receipts(since_timestamp: float) -> list[dict]:
    """Retrieve receipts since a given timestamp for session summary."""
    try:
        if not _DB_PATH.exists():
            return []
        async with aiosqlite.connect(str(_DB_PATH)) as db:
            await db.execute(_CREATE_TABLE)
            cursor = await db.execute(
                """SELECT contract_id, task_type, complexity, model,
                          gates_passed, gate_count, all_passed,
                          latency_ms, input_tokens, output_tokens,
                          cost_usd, opus_equivalent_cost, tokens_reclaimed, savings_usd
                   FROM receipts
                   WHERE timestamp >= datetime(?, 'unixepoch')
                   ORDER BY id ASC""",
                (since_timestamp,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "contract_id": r[0],
                    "task_type": r[1],
                    "complexity": r[2],
                    "model": r[3],
                    "gates_passed": r[4].split(",") if r[4] else [],
                    "gate_count": r[5],
                    "all_passed": bool(r[6]),
                    "latency_ms": r[7],
                    "input_tokens": r[8],
                    "output_tokens": r[9],
                    "cost_usd": r[10],
                    "opus_equivalent_cost": r[11],
                    "tokens_reclaimed": r[12],
                    "savings_usd": r[13],
                }
                for r in rows
            ]
    except Exception:
        return []
