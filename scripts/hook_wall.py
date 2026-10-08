#!/usr/bin/env python3
"""PLAN v16 P0.9-g: external wall-clock latency of the five sync hooks, load on every row.

Entry point for ``llm_router.hook_wall`` from a checkout (the module docstring has
the method). Rows go to ``<LLM_ROUTER_HOME>/hook_wall.jsonl`` unless ``--out``;
``llm-router kpi`` reads them for its P0.9-g line.

    python scripts/hook_wall.py run --runs 200
    python scripts/hook_wall.py run --hook enforce-route --fixture tests/fixtures/hook_payloads/enforce-route.json --runs 200 --cold
    python scripts/hook_wall.py report --since 2026-10-09 --until 2026-10-23
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from llm_router import hook_wall  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(hook_wall.main(sys.argv[1:]))
