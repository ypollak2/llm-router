#!/usr/bin/env python3
"""Write the sha256 pin for the benchmark catalog of a release tag (D-22).

Usage (release step, after the tag exists):
    python scripts/update_catalog_pin.py v15.4.0

Hashes exactly the bytes raw.githubusercontent.com serves for that tag
(``git show <tag>:src/llm_router/data/benchmarks.json``) and rewrites
``src/llm_router/data/catalog_pins.json``. Review and commit the diff.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS = ROOT / "src/llm_router/data/catalog_pins.json"

if __name__ == "__main__":
    tag = sys.argv[1]
    data = subprocess.run(
        ["git", "show", f"{tag}:src/llm_router/data/benchmarks.json"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout
    pins = json.loads(PINS.read_text())
    pins["benchmarks"] = {"tag": tag, "sha256": hashlib.sha256(data).hexdigest()}
    PINS.write_text(json.dumps(pins, indent=2) + "\n")
    print(f"pinned {tag}: {pins['benchmarks']['sha256']}")
