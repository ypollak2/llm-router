"""Scenario-based tests — full narratives, not binary pass/fail.

Each scenario records every step the router takes (signals fired,
decisions made, models called, fallbacks taken, lineage written) and
asserts on the final outcome. The accumulated traces are rendered by
the reporter into a single markdown story at session end.

Run:
    pytest tests/scenarios/ -v
    # Report lands at Docs/SCENARIO_REPORT.md
"""
