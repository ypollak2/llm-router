"""Integration tests for StorageService."""

from __future__ import annotations

import pytest

from llm_router.storage.models import AuditEvent


@pytest.mark.integration
class TestStorageService:
    """StorageService integration tests."""

    def test_write_and_read_budgets(self, storage_service):
        """Verify write → read cycle for budgets."""
        storage_service.write_budget(provider="openai", amount=50.0, source="test")

        budgets = storage_service.read_budgets()

        assert len(budgets) == 1
        assert budgets[0].provider == "openai"
        assert budgets[0].amount_usd == 50.0

    def test_read_budgets_empty_initially(self, storage_service):
        """Fresh StorageService returns empty budget list."""
        budgets = storage_service.read_budgets()
        assert budgets == []

    def test_delete_budget_removes_cap(self, storage_service):
        """delete_budget() removes a cap."""
        storage_service.write_budget(provider="openai", amount=50.0)
        assert len(storage_service.read_budgets()) == 1

        removed = storage_service.delete_budget("openai")

        assert removed is True
        assert len(storage_service.read_budgets()) == 0

    def test_delete_nonexistent_budget_returns_false(self, storage_service):
        """delete_budget() returns False for nonexistent provider."""
        result = storage_service.delete_budget("nonexistent")
        assert result is False

    def test_append_audit_event(self, storage_service):
        """append_audit_event() persists event."""
        event = AuditEvent(
            type="routing.decision",
            actor_id="system",
            actor_email="system@local",
            org_id="org-1",
            resource="lineage:x",
            action="routed",
            detail={"model": "gpt-4o"},
        )

        persisted = storage_service.append_audit_event(event, use_routing=False)

        assert persisted.id == event.id
        assert persisted.prev_hash == ""
        assert len(persisted.hash_hex) > 0

    def test_immutable_budget_model(self, storage_service):
        """Budget model is frozen; mutations raise FrozenInstanceError."""
        budget = storage_service.read_budgets()[0] if storage_service.read_budgets() else None
        if budget is None:
            storage_service.write_budget(provider="openai", amount=50.0)
            budget = storage_service.read_budgets()[0]

        # Try to mutate frozen dataclass
        with pytest.raises(AttributeError):
            budget.amount_usd = 100.0

    def test_immutable_audit_event_model(self):
        """AuditEvent model is frozen."""
        event = AuditEvent(
            type="routing.decision",
            actor_id="system",
            actor_email="system@local",
            org_id="org-1",
            resource="lineage:x",
            action="routed",
        )

        with pytest.raises(AttributeError):
            event.severity = "critical"

    def test_read_config_empty_initially(self, storage_service):
        """Fresh StorageService returns default config."""
        config = storage_service.read_config()

        assert config.version == 1
        assert isinstance(config.data, dict)

    def test_multiple_budgets_persisted(self, storage_service):
        """Multiple budget caps can be persisted."""
        storage_service.write_budget("openai", 50.0)
        storage_service.write_budget("gemini", 100.0)

        budgets = storage_service.read_budgets()

        assert len(budgets) == 2
        providers = {b.provider for b in budgets}
        assert providers == {"openai", "gemini"}
