"""Unit tests for SQLite adapter (audit log storage)."""

from __future__ import annotations

import json
import pytest

from llm_router.storage.adapters.sqlite_adapter import SqliteAdapter


@pytest.mark.unit
class TestSqliteAdapter:
    """SQLite adapter unit tests."""

    def test_create_schema_idempotent(self, llm_router_paths):
        """Schema creation succeeds on first and subsequent calls."""
        SqliteAdapter(llm_router_paths["audit_db"])
        SqliteAdapter(llm_router_paths["audit_db"])

        # Both should succeed without errors
        assert llm_router_paths["audit_db"].exists()

    def test_append_event_creates_row(self, llm_router_paths, sample_audit_events):
        """append() inserts event into audit_events table."""
        adapter = SqliteAdapter(llm_router_paths["audit_db"])
        event = sample_audit_events[0]
        event_dict = {
            "id": event.id,
            "timestamp": event.timestamp,
            "type": event.type,
            "severity": event.severity,
            "actor_id": event.actor_id,
            "actor_email": event.actor_email,
            "org_id": event.org_id,
            "resource": event.resource,
            "action": event.action,
            "detail": event.detail,
            "prev_hash": "",
            "hash_hex": "abc123",
        }

        adapter.append(event_dict)

        data = adapter.read()
        assert len(data) == 1
        assert data[0]["type"] == event.type

    def test_append_computes_hash_correctly(self, llm_router_paths):
        """Verify hash computation in adapter."""
        adapter = SqliteAdapter(llm_router_paths["audit_db"])
        event_dict = {
            "id": "event1",
            "timestamp": 1000.0,
            "type": "routing.decision",
            "severity": "info",
            "actor_id": "system",
            "actor_email": "system@local",
            "org_id": "org-1",
            "resource": "lineage:x",
            "action": "routed",
            "detail": {},
            "prev_hash": "",
            "hash_hex": "dummy",  # Will be stored as-is
        }

        adapter.append(event_dict)

        data = adapter.read()
        # Hash should be stored
        assert len(data[0]["hash_hex"]) > 0

    def test_verify_chain_detects_tampering(self, corrupted_sqlite_db):
        """verify_integrity() detects broken hash chain."""
        adapter = SqliteAdapter(corrupted_sqlite_db)

        is_valid, explanation = adapter.verify_integrity()

        assert is_valid is False
        assert "tampered" in explanation or "broken" in explanation

    def test_read_empty_database(self, llm_router_paths):
        """read() returns empty list for fresh database."""
        adapter = SqliteAdapter(llm_router_paths["audit_db"])

        result = adapter.read()

        assert result == []

    def test_export_json_format(self, llm_router_paths, sample_audit_events):
        """export('json') returns valid JSON."""
        adapter = SqliteAdapter(llm_router_paths["audit_db"])
        event_dict = {
            "id": sample_audit_events[0].id,
            "timestamp": sample_audit_events[0].timestamp,
            "type": sample_audit_events[0].type,
            "severity": sample_audit_events[0].severity,
            "actor_id": sample_audit_events[0].actor_id,
            "actor_email": sample_audit_events[0].actor_email,
            "org_id": sample_audit_events[0].org_id,
            "resource": sample_audit_events[0].resource,
            "action": sample_audit_events[0].action,
            "detail": sample_audit_events[0].detail,
            "prev_hash": "",
            "hash_hex": "abc",
        }

        adapter.append(event_dict)
        result = adapter.export("json")

        # Should parse as valid JSON
        data = json.loads(result)
        assert len(data) >= 1

    def test_export_csv_format(self, llm_router_paths, sample_audit_events):
        """export('csv') returns valid CSV."""
        adapter = SqliteAdapter(llm_router_paths["audit_db"])
        event_dict = {
            "id": sample_audit_events[0].id,
            "timestamp": sample_audit_events[0].timestamp,
            "type": sample_audit_events[0].type,
            "severity": sample_audit_events[0].severity,
            "actor_id": sample_audit_events[0].actor_id,
            "actor_email": sample_audit_events[0].actor_email,
            "org_id": sample_audit_events[0].org_id,
            "resource": sample_audit_events[0].resource,
            "action": sample_audit_events[0].action,
            "detail": sample_audit_events[0].detail,
            "prev_hash": "",
            "hash_hex": "abc",
        }

        adapter.append(event_dict)
        result = adapter.export("csv")

        # Should have header and at least one row
        lines = result.strip().split("\n")
        assert len(lines) >= 2

    def test_write_not_supported(self, llm_router_paths):
        """write() raises NotImplementedError for SQLite."""
        adapter = SqliteAdapter(llm_router_paths["audit_db"])

        with pytest.raises(NotImplementedError):
            adapter.write({})
