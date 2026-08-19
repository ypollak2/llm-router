"""Immutable data models for storage layer.

All models use frozen=True to prevent mutation and enable safe concurrent access.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Budget:
    """Immutable budget cap record."""
    provider: str
    amount_usd: float
    set_at: float
    set_by: str = "system"


@dataclass(frozen=True)
class AuditEvent:
    """Immutable audit log entry with tamper-evident hash chain."""
    type: str  # routing.decision, quota.breach, policy.change, etc.
    actor_id: str  # User ID or "system"
    actor_email: str  # Denormalized for SIEM readability
    org_id: str  # Organization ID (required)
    resource: str  # Thing acted on: "lineage:abc", "team:eng", etc.
    action: str  # Verb: "created", "viewed", "denied", etc.
    detail: dict = field(default_factory=dict)  # JSON-serializable payload
    severity: str = "info"  # info | warn | critical
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    prev_hash: str = ""  # Hash of previous event (chain link)
    hash_hex: str = ""  # SHA-256(prev_hash + canonical_payload)


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable configuration snapshot with version tracking."""
    version: int  # Config version for migration tracking
    data: dict = field(default_factory=dict)  # Config key-value pairs
    updated_at: float = field(default_factory=time.time)  # Timestamp


@dataclass(frozen=True)
class StorageResult:
    """Generic result wrapper for storage operations."""
    success: bool
    data: dict | list | str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)
