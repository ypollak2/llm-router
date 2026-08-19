"""Routing decision lineage tracking — audit every model selection and token usage.

Provides observability into which model handled each operation, why it was selected,
how many tokens were used, and whether that selection was optimal.

Key modules:
- decision_logger: Capture routing decisions with full trace
- lineage_store: JSONL + SQLite dual-write backend
- lineage_query: Query and analyze lineage data
- report_generator: Human-readable reports
"""

from __future__ import annotations

from llm_router.lineage.decision_logger import log_routing_decision
from llm_router.lineage.lineage_query import LineageQuery
from llm_router.lineage.lineage_store import LineageStore
from llm_router.lineage.report_generator import generate_routing_report
from llm_router.lineage.types import (
    Inversion,
    LineageRecord,
    Tier,
    detect_inversion,
    make_record,
    tier_for_model,
)

__all__ = [
    "Inversion",
    "LineageQuery",
    "LineageRecord",
    "LineageStore",
    "Tier",
    "detect_inversion",
    "generate_routing_report",
    "log_routing_decision",
    "make_record",
    "tier_for_model",
]
