"""StorageService: Single entry point for all llm_router file I/O.

Delegates to specialized adapters (JSON, SQLite, YAML) for different data types.
Routes decision logic (validation, classification) to cheap models via llm_query.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from llm_router.storage.adapters.json_adapter import JsonAdapter
from llm_router.storage.adapters.sqlite_adapter import SqliteAdapter
from llm_router.storage.adapters.yaml_adapter import YamlAdapter
from llm_router.storage.models import AuditEvent, Budget, ConfigSnapshot
from llm_router.storage.routing.validators import (
    classify_event_severity,
    validate_budget_before_write,
    validate_config_migration_path,
)


class StorageService:
    """Single entry point for all llm_router file operations.

    Manages three data types via specialized adapters:
    - Budgets (JSON) → budget caps per provider
    - Audit events (SQLite) → tamper-evident hash chain
    - Configuration (YAML) → user-level config with versions
    """

    def __init__(self, router_dir: Path | None = None):
        """Initialize StorageService with adapters.

        Args:
            router_dir: Override ~/.llm-router directory (for testing)
        """
        self._router_dir = router_dir or (Path.home() / ".llm-router")
        self._router_dir.mkdir(parents=True, exist_ok=True)

        # Initialize adapters
        self._budgets_adapter = JsonAdapter(self._router_dir / "budgets.json")
        self._audit_adapter = SqliteAdapter(self._router_dir / "audit.db")
        self._config_adapter = YamlAdapter(self._router_dir / "config.yaml")

    # ── Budget operations ─────────────────────────────────────────────────

    def write_budget(
        self, provider: str, amount: float, source: str = "system"
    ) -> Budget:
        """Persist a budget cap.

        Integrates Routing Point 3.1: Semantic validation via llm_query.

        Args:
            provider: Provider name (e.g., "openai")
            amount:   Monthly cap in USD
            source:   Where the change came from (e.g., "cli", "api")

        Returns:
            Budget object that was persisted

        Raises:
            ValueError: If validation fails
        """
        # Range check (always enforced)
        if amount <= 0:
            raise ValueError(f"Budget cap must be > 0, got {amount}")

        # Routing Point 3.1: Semantic validation
        try:
            is_valid, reasoning = asyncio.run(
                validate_budget_before_write(provider, amount)
            )
            if not is_valid:
                raise ValueError(f"Budget cap validation failed: {reasoning}")
        except Exception as e:
            if "validation failed" in str(e):
                raise
            # Graceful degradation: allow write if routing unavailable

        # Read existing budgets
        existing_data = self._budgets_adapter.read() or {}

        # Update
        existing_data[provider] = amount

        # Persist atomically
        self._budgets_adapter.write(existing_data, atomic=True)

        return Budget(provider=provider, amount_usd=amount, set_at=time.time(), set_by=source)

    def read_budgets(self) -> list[Budget]:
        """Get all persisted budget caps.

        Returns:
            List of Budget objects (empty if no budgets set)
        """
        data = self._budgets_adapter.read() or {}
        return [
            Budget(
                provider=k,
                amount_usd=float(v),
                set_at=time.time(),  # TODO: store actual timestamps in Phase 2
                set_by="unknown",
            )
            for k, v in data.items()
            if isinstance(v, (int, float)) and float(v) > 0
        ]

    def delete_budget(self, provider: str) -> bool:
        """Remove a budget cap.

        Args:
            provider: Provider to remove

        Returns:
            True if removed, False if not found
        """
        data = self._budgets_adapter.read() or {}

        if provider not in data:
            return False

        del data[provider]
        self._budgets_adapter.write(data, atomic=True)
        return True

    # ── Audit operations ─────────────────────────────────────────────────

    def append_audit_event(
        self, event: AuditEvent, use_routing: bool = True
    ) -> AuditEvent:
        """Append an audit event with optional auto-classification.

        Integrates Routing Point 3.2: Severity classification via llm_query.

        Args:
            event:        Audit event to append
            use_routing:  If True, auto-classify severity

        Returns:
            Event with computed hash and auto-classified severity
        """
        severity = event.severity

        if use_routing:
            try:
                severity, reasoning = asyncio.run(
                    classify_event_severity(
                        event_type=event.type,
                        resource=event.resource,
                        actor_id=event.actor_id,
                        detail=event.detail,
                    )
                )
            except Exception:
                # Graceful degradation: use event.severity
                pass

        # Create event with final severity
        final_event = AuditEvent(
            type=event.type,
            actor_id=event.actor_id,
            actor_email=event.actor_email,
            org_id=event.org_id,
            resource=event.resource,
            action=event.action,
            detail=event.detail,
            severity=severity,
            id=event.id,
            timestamp=event.timestamp,
        )

        # Compute hash chain
        prev_hash = self._get_latest_audit_hash()
        payload = self._canonical_audit_payload(final_event)
        hash_hex = self._compute_hash(prev_hash, payload)

        # Create event with hashes
        persisted_event = AuditEvent(
            type=final_event.type,
            actor_id=final_event.actor_id,
            actor_email=final_event.actor_email,
            org_id=final_event.org_id,
            resource=final_event.resource,
            action=final_event.action,
            detail=final_event.detail,
            severity=final_event.severity,
            id=final_event.id,
            timestamp=final_event.timestamp,
            prev_hash=prev_hash,
            hash_hex=hash_hex,
        )

        # Persist
        event_dict = self._event_to_dict(persisted_event)
        self._audit_adapter.append(event_dict)

        return persisted_event

    def verify_audit_chain(self) -> tuple[bool, list[str]]:
        """Verify audit log integrity (no tampering).

        Returns:
            (is_valid, issues) tuple. issues is empty if valid.
        """
        is_valid, explanation = self._audit_adapter.verify_integrity()
        issues = [] if is_valid else [explanation]
        return is_valid, issues

    def export_audit(self, format: str) -> str:
        """Export audit log in specified format.

        Args:
            format: "json", "csv", or "cef" (Common Event Format for SIEMs)

        Returns:
            Formatted string
        """
        return self._audit_adapter.export(format)

    # ── Config operations ────────────────────────────────────────────────

    def read_config(self) -> ConfigSnapshot:
        """Read current configuration.

        Returns:
            ConfigSnapshot with version and data
        """
        data = self._config_adapter.read() or {"version": 1}
        return ConfigSnapshot(
            version=data.get("version", 1),
            data={k: v for k, v in data.items() if k != "version"},
            updated_at=time.time(),
        )

    def migrate_config(self, target_version: int) -> ConfigSnapshot:
        """Migrate config to new version.

        Integrates Routing Point 3.3: Compatibility validation via llm_query.

        Args:
            target_version: Target version number

        Returns:
            ConfigSnapshot after migration

        Raises:
            ValueError: If migration is unsafe
        """
        current = self.read_config()

        if current.version == target_version:
            return current  # Already at target

        # Routing Point 3.3: Validate migration
        try:
            old_keys = set(current.data.keys())
            # TODO: Define target schema (mocked here)
            new_keys = set(current.data.keys()) | {"new_field_v3"}

            can_migrate, reasoning = asyncio.run(
                validate_config_migration_path(
                    old_version=current.version,
                    new_version=target_version,
                    old_keys=old_keys,
                    new_keys=new_keys,
                )
            )
            if not can_migrate:
                raise ValueError(f"Migration validation failed: {reasoning}")
        except Exception as e:
            if "validation failed" in str(e):
                raise
            # Graceful degradation: allow migration if routing unavailable

        # Perform migration (add new fields, preserve old)
        migrated_data = {**current.data, "version": target_version}
        self._config_adapter.write(migrated_data, atomic=True)

        return ConfigSnapshot(
            version=target_version,
            data={k: v for k, v in migrated_data.items() if k != "version"},
            updated_at=time.time(),
        )

    # ── Internal helpers ─────────────────────────────────────────────────

    def _get_latest_audit_hash(self) -> str:
        """Get hash of most recent audit event (for hash chain)."""
        data = self._audit_adapter.read()
        if data and len(data) > 0:
            return data[-1]["hash_hex"]
        return ""

    @staticmethod
    def _canonical_audit_payload(event: AuditEvent) -> str:
        """Generate canonical payload string for hash computation."""
        import json
        payload = {
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
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _compute_hash(prev_hash: str, payload: str) -> str:
        """Compute SHA-256 hash of (prev_hash + payload)."""
        import hashlib
        return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _event_to_dict(event: AuditEvent) -> dict:
        """Convert AuditEvent to dict for storage."""
        return {
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
            "prev_hash": event.prev_hash,
            "hash_hex": event.hash_hex,
        }


# Global singleton
storage_service = StorageService()
