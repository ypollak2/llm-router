#!/usr/bin/env python3
"""GitHub Actions entrypoint: fetch benchmark data and update benchmarks.json.

Usage:
    uv run python scripts/update_benchmarks.py

Requires the 'scripts' optional dependency group:
    uv sync --extra scripts
"""

import logging
import sys
from pathlib import Path

# Add src/ to path so we can import llm_router without installing.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from llm_router.benchmark_fetcher import generate_benchmarks_json  # noqa: E402

if __name__ == "__main__":
    try:
        generate_benchmarks_json()
        sys.exit(0)
    except Exception as e:
        logging.getLogger("update_benchmarks").error("Fatal error: %s", e)
        sys.exit(1)
