"""JSON adapter for budget storage (~/.llm-router/budgets.json).

Implements atomic writes via temp file + os.replace().
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class JsonAdapter:
    """Persist dictionaries as JSON files with atomic writes."""

    def __init__(self, filepath: Path):
        """Initialize adapter for a JSON file.

        Args:
            filepath: Path to .json file (e.g., ~/.llm-router/budgets.json)
        """
        self.filepath = filepath
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict | None:
        """Read JSON file.

        Returns:
            Deserialized dict, or None on error.
        """
        try:
            return json.loads(self.filepath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def write(self, data: dict, atomic: bool = True) -> None:
        """Write dict to JSON file.

        Args:
            data:   Dict to persist
            atomic: If True, use temp file + rename (atomic)

        Raises:
            OSError: On write failure
        """
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

        if atomic:
            # Atomic write: temp file + rename
            tmp = self.filepath.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.filepath)
        else:
            # Direct write
            self.filepath.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def append(self, data: dict) -> None:
        """Not supported for JSON files."""
        raise NotImplementedError("JSON adapter does not support append operations")

    def verify_integrity(self) -> tuple[bool, str]:
        """JSON files don't have integrity checks (no hash chain)."""
        return True, "n/a"
