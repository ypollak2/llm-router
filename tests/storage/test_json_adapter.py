"""Unit tests for JSON adapter (budget storage)."""

from __future__ import annotations

import json
import pytest

from llm_router.storage.adapters.json_adapter import JsonAdapter


@pytest.mark.unit
class TestJsonAdapter:
    """JSON adapter unit tests."""

    def test_write_creates_file(self, llm_router_paths):
        """Verify write() creates file at correct path."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        data = {"openai": 50.0}

        adapter.write(data)

        assert llm_router_paths["budgets"].exists()
        assert json.loads(llm_router_paths["budgets"].read_text()) == data

    def test_write_atomic_uses_tmp_rename(self, llm_router_paths):
        """Verify atomic write uses temp file + rename."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        data = {"openai": 50.0}

        # Monitor tmp file creation
        tmp_path = llm_router_paths["budgets"].with_suffix(".json.tmp")
        adapter.write(data, atomic=True)

        # Tmp file should not exist after atomic write
        assert not tmp_path.exists()
        # Final file should exist
        assert llm_router_paths["budgets"].exists()

    def test_read_missing_file_returns_none(self, llm_router_paths):
        """Verify read() returns None when file doesn't exist."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        result = adapter.read()
        assert result is None

    def test_read_parses_json_correctly(self, llm_router_paths, sample_budgets):
        """Verify read() deserializes JSON correctly."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        llm_router_paths["budgets"].write_text(json.dumps(sample_budgets))

        result = adapter.read()

        assert result == sample_budgets

    def test_read_invalid_json_returns_none(self, llm_router_paths):
        """Graceful degradation: corrupted JSON returns None."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        llm_router_paths["budgets"].write_text("{ invalid json }")

        result = adapter.read()

        assert result is None

    def test_write_preserves_sort_order(self, llm_router_paths):
        """Verify JSON written with sorted keys (deterministic)."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        data = {"zebra": 1.0, "apple": 2.0, "mango": 3.0}

        adapter.write(data)

        # Read raw text to check key order
        content = llm_router_paths["budgets"].read_text()
        assert content.index("apple") < content.index("mango") < content.index("zebra")

    def test_append_not_supported(self, llm_router_paths):
        """Verify append() raises NotImplementedError."""
        adapter = JsonAdapter(llm_router_paths["budgets"])

        with pytest.raises(NotImplementedError):
            adapter.append({})

    def test_verify_integrity_returns_no_checks(self, llm_router_paths):
        """JSON files don't have integrity checks."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        is_valid, reason = adapter.verify_integrity()

        assert is_valid is True
        assert reason == "n/a"

    def test_write_non_atomic(self, llm_router_paths):
        """Verify non-atomic write (direct overwrite)."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        data = {"openai": 50.0}

        adapter.write(data, atomic=False)

        assert json.loads(llm_router_paths["budgets"].read_text()) == data

    def test_write_overwrites_existing(self, llm_router_paths):
        """Verify write() overwrites existing data."""
        adapter = JsonAdapter(llm_router_paths["budgets"])
        old_data = {"openai": 100.0}
        new_data = {"openai": 50.0, "gemini": 200.0}

        adapter.write(old_data)
        adapter.write(new_data)

        result = adapter.read()
        assert result == new_data
        assert "100.0" not in llm_router_paths["budgets"].read_text()
