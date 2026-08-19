"""Shared fixtures for storage layer tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from llm_router.storage.models import AuditEvent
from llm_router.storage.service import StorageService


# ── Directory Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def temp_llm_router_dir():
    """Isolated ~/.llm-router equivalent for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / ".llm-router"


@pytest.fixture
def llm_router_paths(temp_llm_router_dir):
    """Paths for budgets.json, audit.db, config.yaml."""
    temp_llm_router_dir.mkdir(parents=True, exist_ok=True)
    return {
        "budgets": temp_llm_router_dir / "budgets.json",
        "audit_db": temp_llm_router_dir / "audit.db",
        "config": temp_llm_router_dir / "config.yaml",
    }


@pytest.fixture
def storage_service(temp_llm_router_dir):
    """StorageService instance with isolated directory."""
    return StorageService(router_dir=temp_llm_router_dir)


# ── Data Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sample_budgets():
    """Pre-built budget data for testing."""
    return {
        "openai": 50.0,
        "gemini": 100.0,
        "anthropic": 0.0,
    }


@pytest.fixture
def sample_audit_events():
    """Pre-built audit events for testing."""
    import time
    now = time.time()
    return [
        AuditEvent(
            type="routing.decision",
            actor_id="system",
            actor_email="system@llm_router.local",
            org_id="org-test",
            resource="lineage:abc123",
            action="routed",
            detail={"model": "gpt-4o", "cost": 0.15},
            severity="info",
            timestamp=now,
        ),
        AuditEvent(
            type="quota.breach",
            actor_id="user-xyz",
            actor_email="alice@company.com",
            org_id="org-test",
            resource="team:engineering",
            action="blocked",
            detail={"reason": "monthly cap exceeded", "overage": 50.0},
            severity="critical",
            timestamp=now + 1,
        ),
    ]


@pytest.fixture
def sample_config_v2():
    """Config in version 2 format."""
    return {
        "version": 2,
        "openai_api_key": "sk-test-...",
        "ollama_base_url": "http://localhost:11434",
        "routing_profile": "balanced",
    }


@pytest.fixture
def sample_config_v3():
    """Config in version 3 format (with breaking changes)."""
    return {
        "version": 3,
        "openai_api_key": "sk-test-...",
        "ollama_base_url": "http://localhost:11434",
        "routing_profile": "balanced",
        "new_field_required": "value",
    }


# ── Mock Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm_query():
    """Mock llm_query routing to control responses in tests."""
    async def mock_query(prompt, complexity="simple", **kwargs):
        # Simulate different routing behaviors
        if "budget" in prompt.lower():
            return {"decision": "yes", "reasoning": "reasonable amount"}
        elif "severity" in prompt.lower():
            return "warn"
        elif "migrate" in prompt.lower():
            return True
        elif "sensitive" in prompt.lower():
            return False
        return {"result": "success"}

    return mock_query


@pytest.fixture
def mock_llm_query_unavailable():
    """Simulate llm_query unavailable (routing fallback)."""
    async def mock_unavailable(prompt, **kwargs):
        raise ConnectionError("llm_query unavailable")
    return mock_unavailable


# ── Concurrency Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def concurrent_writers():
    """Simulate concurrent writers accessing storage."""
    from concurrent.futures import ProcessPoolExecutor
    executor = ProcessPoolExecutor(max_workers=4)
    yield executor
    executor.shutdown(wait=True)


# ── Corruption Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def corrupted_sqlite_db(llm_router_paths):
    """SQLite database with tampered hash chain."""
    db_path = llm_router_paths["audit_db"]
    conn = sqlite3.connect(str(db_path))

    # Create schema
    conn.execute("""
        CREATE TABLE audit_events (
            id TEXT PRIMARY KEY,
            timestamp REAL,
            type TEXT,
            severity TEXT,
            actor_id TEXT,
            actor_email TEXT,
            org_id TEXT,
            resource TEXT,
            action TEXT,
            detail TEXT,
            prev_hash TEXT,
            hash_hex TEXT UNIQUE
        )
    """)

    # Insert event with valid hash
    conn.execute(
        "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "id1", 1000.0, "routing.decision", "info", "system", "system@local",
            "org-1", "lineage:x", "routed", json.dumps({}), "",
            "abc123def456"
        ),
    )

    # Tamper with hash (break chain)
    conn.execute(
        "UPDATE audit_events SET hash_hex = ? WHERE id = ?",
        ("tampered_hash_xyz", "id1"),
    )
    conn.commit()
    conn.close()

    return db_path


# ── Pytest Configuration ──────────────────────────────────────────────────


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "concurrent: mark test as concurrency test")
